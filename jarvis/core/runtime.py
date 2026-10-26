"""Composition root.

:func:`build_runtime` is the single place where every subsystem is created and
wired together.  The UI, the CLI and the tests all use it, which is why nothing
else in the codebase constructs a :class:`PolicyEngine` or a
:class:`VisionService` directly.

Start-up order matters for responsiveness: the window can appear before Ollama
has been probed, so everything that can block (platform probing, model listing,
speech engine loading) happens on background threads and reports in through the
event bus.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .. import config as config_mod
from .. import paths
from ..events import AgentState, EventBus, EventType, bus as global_bus
from ..integrations.browser import BrowserIntegration
from ..integrations.explorer import ExplorerIntegration
from ..integrations.smtc import create_smtc
from ..integrations.spotify import SpotifyIntegration
from ..llm.ollama_client import OllamaClient, is_ollama_installed
from ..llm.router import ModelRouter
from ..logging_setup import dump_diagnostics, setup as configure_logging
from ..memory import MemoryStore
from ..platform import Platform, create_platform
from ..security.audit import AuditLog
from ..security.guard import Pressure, ResourceGuard
from ..security.policy import PolicyEngine, SessionGrants
from ..tools import ToolContext, build_default_registry
from ..tools.registry import ToolRegistry
from ..vision import VisionService
from .agent import Agent
from .broker import ApprovalBroker

log = logging.getLogger(__name__)


@dataclass
class RuntimeStatus:
    """Snapshot of everything the UI status bar shows."""

    state: str = AgentState.IDLE.value
    ollama_installed: bool = False
    ollama_reachable: bool = False
    ollama_detail: str = ""
    models: List[Dict[str, Any]] = field(default_factory=list)
    main_model: str = ""
    vision_model: str = ""
    fast_model: str = ""
    tools_total: int = 0
    tools_available: int = 0
    simulated: bool = False
    pressure: str = Pressure.OK.value
    ram_gb: float = 0.0
    vram_gb: float = 0.0
    ram_budget_gb: float = 0.0
    vram_budget_gb: float = 0.0
    uptime_s: float = 0.0
    speech: Dict[str, Any] = field(default_factory=dict)
    notes: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "ollama": {"installed": self.ollama_installed,
                       "reachable": self.ollama_reachable,
                       "detail": self.ollama_detail,
                       "models": self.models,
                       "main_model": self.main_model,
                       "vision_model": self.vision_model,
                       "fast_model": self.fast_model},
            "tools": {"total": self.tools_total, "available": self.tools_available},
            "platform": {"simulated": self.simulated, "notes": dict(self.notes)},
            "resources": {"pressure": self.pressure, "ram_gb": round(self.ram_gb, 2),
                          "vram_gb": round(self.vram_gb, 2),
                          "ram_budget_gb": round(self.ram_budget_gb, 2),
                          "vram_budget_gb": round(self.vram_budget_gb, 2)},
            "uptime_s": round(self.uptime_s, 1),
            "speech": dict(self.speech),
        }


class JarvisRuntime:
    """Owns every long-lived object for one JARVIS process."""

    def __init__(self,
                 settings: Optional[Any] = None,
                 bus: Optional[EventBus] = None,
                 force_simulated: Optional[bool] = None,
                 realtime: bool = True,
                 with_speech: bool = True) -> None:
        self.started_at = time.time()
        self.settings = settings if settings is not None else config_mod.load()
        self.bus = bus if bus is not None else global_bus
        self.state = AgentState.IDLE
        self._state_lock = threading.RLock()
        self._shutdown = threading.Event()
        self.with_speech = bool(with_speech)

        general = self.settings.general
        configure_logging(level=str(general.log_level),
                          to_file=bool(general.log_to_file),
                          files_kept=int(general.log_files_kept))
        for folder in (paths.data_dir(), paths.log_dir(), paths.models_dir(),
                       paths.screenshots_dir(), paths.temp_dir()):
            folder.mkdir(parents=True, exist_ok=True)

        # --- security first: nothing else may exist before the policy engine --
        self.grants = SessionGrants()
        self.policy = PolicyEngine(self.settings_provider, self.grants)
        self.audit = AuditLog(enabled=bool(self.settings.security.audit_log_enabled),
                              retention_days=int(self.settings.security.audit_log_days))
        self.guard = ResourceGuard(self.settings_provider,
                                   on_pressure=self._on_pressure)
        self.broker = ApprovalBroker(self.bus, self.grants, audit=self.audit)

        # --- desktop --------------------------------------------------------
        self.platform: Platform = create_platform(self.settings_provider,
                                                  force_simulated=force_simulated,
                                                  realtime=realtime)
        self.vision = VisionService(self.settings_provider, self.platform,
                                    self.guard, self.bus)
        self.memory = MemoryStore(settings_provider=self.settings_provider)

        # --- model ----------------------------------------------------------
        # NOTE: the client is configured against the *Ollama* section (it reads
        # base_url, timeouts and sampling options straight off it) while the
        # router takes the whole settings tree because it also honours the
        # resource ceilings.
        self.ollama = OllamaClient(self.ollama_settings_provider)
        self.router = ModelRouter(self.ollama, self.settings_provider)

        # --- tools ----------------------------------------------------------
        self.ctx = ToolContext(
            settings_provider=self.settings_provider,
            platform=self.platform, bus=self.bus, policy=self.policy,
            audit=self.audit, guard=self.guard, approvals=self.broker,
            memory=self.memory, vision=self.vision, ollama=self.ollama,
            router=self.router, speech=None, integrations={})
        self.registry: ToolRegistry = build_default_registry(self.ctx)
        self._wire_integrations()

        # --- speech ---------------------------------------------------------
        self.speech: Any = None
        if self.with_speech:
            self.speech = self._build_speech()
            self.ctx.speech = self.speech

        self.agent = Agent(self)

        self.ollama_install_info: Dict[str, Any] = {}
        self._models_cache: List[Dict[str, Any]] = []
        self._last_models_refresh = 0.0

        # --- background work -------------------------------------------------
        # Model probes, speech pre-loading and diagnostics all belong off the
        # calling thread.  One small pool (``general.background_workers``) keeps
        # that bounded instead of spawning a thread per request.
        workers = max(1, min(8, int(getattr(self.settings.general,
                                            "background_workers", 2) or 2)))
        self.executor = ThreadPoolExecutor(max_workers=workers,
                                           thread_name_prefix="jarvis-bg")

        # Speech finishes asynchronously, so the "speaking" state is cleared by
        # the event rather than by the agent (which has already moved on).
        self.bus.subscribe(EventType.TTS_FINISHED, self._on_tts_finished)
        self.bus.subscribe(EventType.METRICS, self._on_metrics)

    # ------------------------------------------------------------------
    # wiring
    # ------------------------------------------------------------------
    @property
    def settings_provider(self) -> Callable[[], Any]:
        # A stable callable for every subsystem; re-reads the cached settings
        # object, which config.load() keeps current after each save.
        def _provider() -> Any:
            return config_mod.load()

        return _provider

    @property
    def ollama_settings_provider(self) -> Callable[[], Any]:
        def _provider() -> Any:
            return config_mod.load().ollama

        return _provider

    def reload_settings(self) -> Any:
        self.settings = config_mod.load(force=True)
        self.bus.publish(EventType.SETTINGS_CHANGED, self.settings.to_dict(),
                         source="runtime")
        return self.settings

    def _wire_integrations(self) -> None:
        """Native app integrations, preferred over blind automation."""
        integrations = self.ctx.integrations
        integrations["registry"] = self.registry
        if self.platform.audio is not None:
            integrations["audio"] = self.platform.audio
        try:
            smtc = create_smtc()
            integrations["smtc"] = smtc
            self.platform.notes["media_session"] = getattr(
                smtc, "detail", "") or getattr(smtc, "name", "smtc")
        except Exception as exc:  # noqa: BLE001
            self.platform.notes["media_session"] = f"SMTC unavailable ({exc})"
        try:
            integrations["spotify"] = SpotifyIntegration(
                self.ctx, smtc=integrations.get("smtc"))
            integrations["browser"] = BrowserIntegration(self.ctx)
            integrations["explorer"] = ExplorerIntegration(self.ctx)
        except Exception:  # noqa: BLE001
            log.exception("integration wiring failed")

    def _build_speech(self) -> Any:
        try:
            from ..speech import SpeechService

            service = SpeechService(self.settings_provider, bus=self.bus,
                                    guard=self.guard, on_command=self.on_voice_command)
            return service
        except Exception as exc:  # noqa: BLE001
            log.warning("Speech unavailable: %s", exc)
            self.platform.notes["speech"] = f"Speech subsystem failed to load ({exc})"
            return None

    def on_voice_command(self, text: str, source: str = "voice") -> None:
        """Speech -> agent.  Ignored while a task is already running."""
        if self.agent.busy:
            self.bus.publish(EventType.NOTIFICATION, {
                "title": "Busy",
                "message": "Finishing the current task first."}, source="runtime")
            return
        self.bus.publish(EventType.USER_MESSAGE, {"text": text, "source": source},
                         source="runtime")
        self.agent.submit(text, source=source)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self, background: bool = True) -> Dict[str, Any]:
        """Begin monitoring and (optionally) probe Ollama off the UI thread."""
        report: Dict[str, Any] = {"data_dir": str(paths.data_dir()),
                                  "simulated": self.platform.is_simulated}
        self.guard.start()
        if self.settings.general.auto_check_ollama_on_start:
            if background:
                self.submit_background(self.refresh_models, True)
            else:
                self.refresh_models(force=True)
        if self.speech is not None:
            self.submit_background(self._start_speech)
        self.bus.publish(EventType.NOTIFICATION, {
            "title": self.settings.general.assistant_name,
            "message": ("Demo Mode is active - actions are simulated."
                        if self.platform.is_simulated else "Ready.")},
            source="runtime")
        return report

    def submit_background(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` on the bounded background pool.

        Falls back to a daemon thread if the pool is gone (during shutdown) so a
        caller never has to care which path was taken.  Never raises.
        """
        try:
            return self.executor.submit(fn, *args, **kwargs)
        except Exception:  # noqa: BLE001
            try:
                thread = threading.Thread(target=fn, args=args, kwargs=kwargs,
                                          name="jarvis-bg-fallback", daemon=True)
                thread.start()
            except Exception:  # noqa: BLE001
                log.debug("could not start background work", exc_info=True)
            return None

    def _start_speech(self) -> None:
        try:
            self.speech.preload()
            mode = self.settings.speech.activation_mode
            if self.settings.speech.stt_enabled and mode in ("wakeword", "continuous"):
                self.speech.set_mode(mode)
        except Exception:  # noqa: BLE001
            log.exception("speech start-up failed")

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        log.info("Shutting down JARVIS")
        self.agent.cancel()
        try:
            if self.speech is not None:
                self.speech.shutdown()
        except Exception:  # noqa: BLE001
            log.debug("speech shutdown failed", exc_info=True)
        try:
            self.broker.cancel_all("JARVIS is shutting down")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.guard.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.memory.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.audit.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.settings.ollama.keep_alive not in ("-1", "forever"):
                for model in {self.settings.ollama.main_model,
                              self.settings.ollama.vision_model,
                              self.settings.ollama.fast_model}:
                    if model:
                        self.ollama.unload(model)
        except Exception:  # noqa: BLE001
            log.debug("model unload failed", exc_info=True)

    # ------------------------------------------------------------------
    # state + status
    # ------------------------------------------------------------------
    def set_state(self, state: AgentState) -> None:
        with self._state_lock:
            if state is self.state:
                return
            self.state = state
        self.bus.publish(EventType.STATE_CHANGED, {"state": state.value},
                         source="runtime")

    def refresh_models(self, force: bool = False) -> Dict[str, Any]:
        """Probe Ollama and refresh the model list (cached for a few seconds)."""
        now = time.time()
        if not force and now - self._last_models_refresh < 3.0 and self._models_cache:
            return {"models": self._models_cache, "cached": True}
        info = is_ollama_installed()
        self.ollama_install_info = info
        reachable = self.ollama.ping()
        models: List[Dict[str, Any]] = []
        if reachable:
            try:
                models = [m.as_dict() for m in self.ollama.list_models(force=force)]
            except Exception as exc:  # noqa: BLE001
                log.warning("model listing failed: %s", exc)
        self._models_cache = models
        self._last_models_refresh = now

        o = self.settings.ollama
        if reachable and models and not o.main_model:
            chosen = self.router.auto_select_main()
            if chosen:
                def _select(s: Any, model: str = chosen) -> None:
                    s.ollama.main_model = model

                config_mod.update(_select)
                self.reload_settings()
                log.info("Auto-selected main model: %s", chosen)
        self.bus.publish(EventType.OLLAMA_STATUS, {
            "installed": bool(info.get("installed")),
            "reachable": bool(reachable),
            "detail": info.get("detail", "") or self.ollama.last_error,
            "models": models,
            "main_model": self.settings.ollama.main_model,
            "vision_model": self.settings.ollama.vision_model,
            "fast_model": self.settings.ollama.fast_model,
        }, source="runtime")
        self.bus.publish(EventType.MODELS_CHANGED, {"models": models}, source="runtime")
        if reachable and self.settings.ollama.warm_on_start:
            self.warm_main_model()
        return {"models": models, "reachable": reachable, "installed": info}

    def warm_main_model(self) -> bool:
        """Pre-load the main model so the first request is fast."""
        model = self.settings.ollama.main_model
        if not model:
            return False
        if self.guard.pressure is Pressure.HARD:
            log.info("Skipping model warm-up: memory pressure is high")
            return False
        try:
            return bool(self.ollama.warm(model))
        except Exception as exc:  # noqa: BLE001
            log.debug("warm-up failed: %s", exc)
            return False

    def _on_tts_finished(self, event: Any) -> None:
        with self._state_lock:
            if self.state is not AgentState.SPEAKING:
                return
        self.set_state(AgentState.IDLE)

    def _on_metrics(self, event: Any) -> None:
        """Re-publish nothing; this hook exists so the UI can attach here."""
        return None

    def _on_pressure(self, pressure: Pressure, snapshot: Any) -> None:
        """Load-shedding: give back memory and drop optional work first."""
        report = {"pressure": pressure.value}
        try:
            report.update(self.guard.release_memory())
        except Exception:  # noqa: BLE001
            pass
        if pressure is Pressure.HARD:
            # Free the GPU: unload anything that is not the model in use.
            try:
                used = {self.settings.ollama.main_model}
                for model in self.ollama.running_models():
                    if model not in used:
                        self.ollama.unload(model)
                        report.setdefault("unloaded", []).append(model)
            except Exception:  # noqa: BLE001
                log.debug("load shedding failed", exc_info=True)
            if self.vision is not None:
                self.vision.invalidate()
        self.bus.publish(EventType.RESOURCE_WARNING, report, source="runtime")
        log.warning("RESOURCE PRESSURE %s %s", pressure.value, report)

    def status(self) -> RuntimeStatus:
        snap = self.guard.latest
        availability = self.registry.availability()
        available = sum(1 for ok, _why in availability.values() if ok)
        st = RuntimeStatus(
            state=self.state.value,
            ollama_installed=bool(self.ollama_install_info.get("installed")),
            ollama_reachable=bool(self.ollama.status().get("connected")),
            ollama_detail=str(self.ollama_install_info.get("hint")
                              or self.ollama_install_info.get("version")
                              or self.ollama.status().get("error") or ""),
            models=list(self._models_cache),
            main_model=self.settings.ollama.main_model,
            vision_model=self.settings.ollama.vision_model,
            fast_model=self.settings.ollama.fast_model,
            tools_total=len(self.registry),
            tools_available=available,
            simulated=self.platform.is_simulated,
            pressure=snap.pressure.value,
            ram_gb=snap.process_ram_gb,
            vram_gb=snap.vram_used_gb,
            ram_budget_gb=snap.ram_budget_gb,
            vram_budget_gb=snap.vram_budget_gb,
            uptime_s=time.time() - self.started_at,
            speech=self.speech.status() if self.speech is not None else {},
            notes=dict(self.platform.notes),
        )
        return st

    # ------------------------------------------------------------------
    def platform_summary(self) -> str:
        return self.platform.summary_text()

    def diagnostics(self) -> Dict[str, Any]:
        """Everything a bug report needs, with secrets already redacted."""
        data: Dict[str, Any] = {"diagnostics_file": dump_diagnostics()}
        data["status"] = self.status().as_dict()
        data["platform"] = self.platform.capabilities()
        data["tools"] = {"total": len(self.registry),
                         "unavailable": self.registry.unavailable_report(),
                         "categories": self.registry.by_category()}
        data["speech_engines"] = self.speech.engines() if self.speech is not None else {}
        data["memory"] = self.memory.stats()
        data["grants"] = self.grants.snapshot()
        return data

    def say(self, text: str, interrupt: bool = True) -> bool:
        if self.speech is None:
            return False
        return self.speech.speak(text, interrupt=interrupt)


def build_runtime(**kwargs: Any) -> JarvisRuntime:
    """Convenience factory used by the UI, the CLI and the tests."""
    return JarvisRuntime(**kwargs)
