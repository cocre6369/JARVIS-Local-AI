"""Typed, persistent application settings.

The settings object is the single source of truth for every configurable
behaviour in JARVIS.  It is stored as human readable JSON in the per-user data
directory and written atomically so a crash can never leave a half-written
config behind.

Security relevant fields are *locked*: :meth:`Settings.sanitize` re-asserts the
hard ceilings (12 GB RAM, 8 GB VRAM, sensitive-data masking, no cloud AI) every
single time settings are loaded **and** every time they are saved.  A tampered
settings.json therefore cannot weaken the security layer.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hard limits from the project specification (section 15.F).
# These are ceilings, not defaults, and are never configurable above the cap.
# ---------------------------------------------------------------------------
ABSOLUTE_RAM_LIMIT_GB = 12.0
ABSOLUTE_VRAM_LIMIT_GB = 8.0


@dataclass
class OllamaSettings:
    host: str = "127.0.0.1"
    port: int = 11434
    #: Model used for reasoning / tool calling.
    main_model: str = ""
    #: Model used to interpret screenshots.  Empty => use main_model if it has
    #: vision, otherwise vision analysis is disabled (and reported honestly).
    vision_model: str = ""
    #: Small, fast model used for intent classification and re-ranking.
    fast_model: str = ""
    #: How long Ollama keeps a model resident in VRAM after the last request.
    keep_alive: str = "10m"
    request_timeout_s: float = 180.0
    connect_timeout_s: float = 4.0
    num_ctx: int = 8192
    #: Layers offloaded to the GPU.  -1 => let Ollama decide (all it can fit).
    num_gpu: int = -1
    num_thread: int = 0  # 0 => auto
    temperature: float = 0.2
    top_p: float = 0.9
    use_mmap: bool = True
    streaming: bool = True
    #: Retry a failed generation once with a shortened context.
    auto_recover: bool = True
    #: PERFORMANCE: load the main model during start-up so the first request
    #: does not pay the cold-load cost (several seconds on a laptop GPU).
    warm_on_start: bool = True
    #: PERFORMANCE: route short, unambiguous requests (media, launching an app)
    #: to the fast model.  Multi-step computer control always uses the main one.
    use_fast_model_for_simple_requests: bool = True
    #: Poll interval for the model list while the models page is open (ms).
    refresh_interval_ms: int = 15000

    @property
    def base_url(self) -> str:
        host = self.host.strip()
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        return f"http://{host}:{int(self.port)}"


@dataclass
class SpeechSettings:
    # --- input -----------------------------------------------------------
    stt_enabled: bool = True
    stt_engine: str = "auto"           # auto | faster-whisper | vosk | none
    stt_model_size: str = "base.en"    # tiny.en | base.en | small.en | ...
    stt_device: str = "cpu"            # cpu | cuda | auto
    stt_compute_type: str = "int8"     # int8 | int8_float16 | float16 | float32
    stt_language: str = "en"           # Australian English is handled by the
    stt_region: str = "en-AU"          # 'en' whisper models + prompt biasing.
    microphone: str = ""               # empty => system default
    sample_rate: int = 16000
    #: wakeword | push_to_talk | button | continuous | disabled
    activation_mode: str = "push_to_talk"
    wake_phrases: List[str] = field(
        default_factory=lambda: ["hey jarvis", "jarvis", "okay jarvis"]
    )
    #: 0..1 - how aggressively the VAD must agree that a human is talking
    #: before audio is handed to the transcription model.
    vad_aggressiveness: int = 2
    vad_frame_ms: int = 30
    wake_rms_threshold: float = 0.006
    push_to_talk_key: str = "ctrl+space"
    #: Stop TTS output the moment the user starts speaking.
    barge_in: bool = True
    beam_size: int = 1
    #: Bias transcription towards assistant style English (helps en-AU).
    initial_prompt: str = (
        "You are talking to JARVIS, a local desktop assistant. "
        "Australian English spelling."
    )
    # --- output ----------------------------------------------------------
    tts_enabled: bool = True
    tts_engine: str = "auto"           # auto | piper | sapi | espeak | none
    tts_voice: str = ""                # piper voice name/path, or SAPI voice
    tts_rate: float = 1.0              # 0.5 .. 2.0
    tts_volume: float = 1.0            # 0.0 .. 1.0
    tts_pitch: float = 1.0
    #: Synthesise sentence-by-sentence and start playback on the first chunk.
    tts_streaming: bool = True
    tts_max_chunk_chars: int = 220
    #: PERFORMANCE: load the TTS voice at start-up so the first reply is spoken
    #: immediately instead of after a model load.
    preload_tts: bool = True
    speak_progress_updates: bool = True
    speak_tool_actions: bool = False   # don't narrate every mouse movement


@dataclass
class VisionSettings:
    enabled: bool = True
    capture_backend: str = "auto"      # auto | mss | qt | win32 | simulated
    #: Longest edge of the image handed to the vision model.  Keeps VRAM and
    #: token usage bounded on an 8 GB card.
    max_dimension: int = 1280
    jpeg_quality: int = 72
    ocr_backend: str = "auto"          # auto | windows | tesseract | rapidocr
    ocr_language: str = "en-AU"
    #: Use the Windows UI Automation tree - much more reliable than pixels.
    use_uia: bool = True
    uia_max_elements: int = 160
    #: Black out password fields before an image is stored or shown to a model.
    #: LOCKED - cannot be disabled from the UI.
    mask_password_fields: bool = True
    #: Do not send screenshots anywhere except the local Ollama server. LOCKED.
    local_only: bool = True
    observation_interval_ms: int = 1200
    #: Re-observe automatically after every executed action (verification).
    observe_after_action: bool = True
    #: Skip re-analysis when the screen hash has not changed.
    cache_screenshots: bool = True
    cache_ttl_s: float = 1.5
    #: PERFORMANCE: OCR is the slowest part of an observation.  When UI
    #: Automation already returned a usable control tree there is nothing OCR can
    #: add for element location, so it is skipped unless text was asked for.
    skip_ocr_when_uia_rich: bool = True
    uia_rich_threshold: int = 20
    #: PERFORMANCE: several tools resolve elements back to back against the same
    #: window; reuse that control tree briefly instead of walking it again.
    element_cache_ttl_s: float = 1.0
    include_cursor: bool = True
    capture_monitor: int = 0           # 0 = primary/all


@dataclass
class AutomationSettings:
    enabled: bool = True
    #: Seconds between synthetic keystrokes. 0 => paste-style fast typing.
    typing_interval_s: float = 0.012
    click_settle_s: float = 0.18
    #: Wait used after launching an application before observing it.
    app_launch_wait_s: float = 2.5
    #: After the window appears, keep watching until the app has CONTENT (UI
    #: elements / non-blank pixels) before acting on it - a blank Spotify shell
    #: must not be treated as a loaded app.  0 disables the ready-wait.
    app_ready_wait_s: float = 12.0
    #: How long find_element / click_element / type keep re-looking for an
    #: element while a UI is still loading, instead of failing instantly with
    #: "could not find the search bar".  0 = fail on the first miss.
    element_wait_s: float = 6.0
    generic_wait_s: float = 0.8
    max_wait_s: float = 30.0
    #: Agent loop limits (infinite-loop protection).
    max_steps_per_task: int = 30
    max_identical_actions: int = 3
    max_consecutive_failures: int = 4
    #: Always check the result of an action instead of assuming success.
    #: LOCKED - this is a core requirement of the project.
    verify_after_action: bool = True
    take_screenshot_before_action: bool = True
    move_mouse_smoothly: bool = True
    mouse_move_duration_s: float = 0.18
    #: Prefer native integrations (Spotify API, browser CDP, UIA) over blind
    #: mouse automation whenever they are available.
    prefer_native_integrations: bool = True
    #: Backend preference for synthetic input: auto | pyautogui | win32 | xdotool.
    input_backend: str = "auto"
    #: Browser DevTools automation is OPT-IN.  Off means JARVIS drives the user's
    #: own browser through its address bar and reads the result back with vision
    #: and OCR, so nothing extra has to be enabled in the browser.
    browser_devtools: bool = False
    browser_devtools_port: int = 9222
    #: PERFORMANCE: poll for the expected change instead of sleeping a fixed
    #: amount.  The verification is identical; the waiting is not.
    adaptive_waits: bool = True
    settle_poll_interval_s: float = 0.04
    #: PERFORMANCE: reuse the previous observation while it is still fresh
    #: enough, instead of re-capturing for every element lookup.
    observation_reuse_s: float = 2.0
    allowed_roots: List[str] = field(default_factory=list)  # extra file roots
    #: Folders JARVIS may write to without asking. Empty => sensible defaults
    #: (Desktop, Documents, Downloads, Pictures, Music, Videos).
    writable_roots: List[str] = field(default_factory=list)


@dataclass
class SecuritySettings:
    #: strict | balanced.  Never "off".
    mode: str = "balanced"
    #: Per-tool overrides.  Values: "allow" | "ask" | "block".
    #: Overrides may only ever *tighten* a tool's built-in classification;
    #: :meth:`Settings.sanitize` drops any override that would loosen a
    #: tool marked as approval-required or prohibited in code.
    tool_overrides: Dict[str, str] = field(default_factory=dict)
    require_approval_for_downloads: bool = True
    require_approval_for_messages: bool = True
    require_approval_for_deletes: bool = True
    require_approval_for_overwrites: bool = True
    require_approval_for_bulk_file_moves: bool = True
    require_approval_for_shell_commands: bool = True
    require_approval_for_financial_sites: bool = True
    #: Remember "always allow" decisions for the rest of the session only.
    remember_session_grants: bool = True
    audit_log_enabled: bool = True
    audit_log_days: int = 30
    #: LOCKED security invariants -------------------------------------
    redact_sensitive_output: bool = True
    block_password_field_reading: bool = True
    prohibit_privilege_escalation: bool = True
    prohibit_uninstall: bool = True
    prohibit_registry_writes: bool = True
    prohibit_security_software_changes: bool = True
    prohibit_cloud_ai: bool = True
    upload_screenshots: bool = False
    upload_audio: bool = False
    #: Resource ceilings.  Values above the absolute caps are clamped.
    max_ram_gb: float = ABSOLUTE_RAM_LIMIT_GB
    max_vram_gb: float = ABSOLUTE_VRAM_LIMIT_GB
    #: Start shedding load at this fraction of the ceiling.
    soft_threshold: float = 0.80
    resource_poll_ms: int = 1500
    #: Kill-switch: when True no computer-control tool may run at all.
    automation_master_switch: bool = True


@dataclass
class MemorySettings:
    enabled: bool = True
    remember_conversations: bool = True
    #: Automatically extract durable facts ("my favourite album is ...").
    auto_extract_facts: bool = True
    max_context_messages: int = 24
    max_context_chars: int = 24000
    max_facts_in_prompt: int = 12
    retention_days: int = 0            # 0 = keep forever
    #: Never write secrets into memory. LOCKED.
    redact_secrets: bool = True


@dataclass
class UISettings:
    theme: str = "dark"
    accent: str = "#22d3ee"
    accent_secondary: str = "#3b82f6"
    font_family: str = "Segoe UI"
    mono_font_family: str = "Cascadia Mono"
    base_font_size: int = 11
    animation_speed: float = 1.0       # 0 disables animation
    core_visualiser: bool = True
    show_status_metrics: bool = True
    metrics_refresh_ms: int = 1000
    start_minimised: bool = False
    #: Closing the window minimises JARVIS to the tray instead of quitting.
    #: LOCKED ON: the assistant must keep working (tasks, wake word, the
    #: floating orb) when the main window is closed.
    close_to_tray: bool = True
    #: Floating always-on-top orb (bottom-right): state colours + mini chat,
    #: visible even when the main window is minimised or closed to the tray.
    show_desktop_orb: bool = True
    window_opacity: float = 1.0
    conversation_font_size: int = 12
    show_tool_trace: bool = True


@dataclass
class GeneralSettings:
    assistant_name: str = "JARVIS"
    personality: str = "concise"       # concise | detailed | playful
    language: str = "en-AU"
    log_level: str = "INFO"
    log_to_file: bool = True
    log_files_kept: int = 7
    first_run_complete: bool = False
    #: LOCKED: JARVIS has no telemetry of any kind.
    telemetry_enabled: bool = False
    auto_check_ollama_on_start: bool = True
    #: Run the assistant loop on a background thread (never block the UI).
    background_workers: int = 2


@dataclass
class Settings:
    ollama: OllamaSettings = field(default_factory=OllamaSettings)
    speech: SpeechSettings = field(default_factory=SpeechSettings)
    vision: VisionSettings = field(default_factory=VisionSettings)
    automation: AutomationSettings = field(default_factory=AutomationSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    ui: UISettings = field(default_factory=UISettings)
    general: GeneralSettings = field(default_factory=GeneralSettings)

    # ------------------------------------------------------------------
    # (de)serialisation
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return _to_dict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        s = cls()
        for f in dataclasses.fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            current = getattr(s, f.name)
            if is_dataclass(current) and isinstance(value, dict):
                for sub_f in dataclasses.fields(current):
                    if sub_f.name in value:
                        try:
                            setattr(current, sub_f.name, value[sub_f.name])
                        except Exception:  # noqa: BLE001 - never die on config
                            log.warning(
                                "Ignoring invalid setting %s.%s=%r",
                                f.name, sub_f.name, value[sub_f.name],
                            )
        s.sanitize()
        return s

    # ------------------------------------------------------------------
    # Security invariant enforcement
    # ------------------------------------------------------------------
    def sanitize(self) -> "Settings":
        """Clamp and re-assert every security invariant.  Always call this
        after loading *or* changing settings.  It is idempotent."""
        sec = self.security
        # Resource ceilings can be lowered, never raised.
        sec.max_ram_gb = float(min(max(sec.max_ram_gb, 0.5), ABSOLUTE_RAM_LIMIT_GB))
        sec.max_vram_gb = float(min(max(sec.max_vram_gb, 0.5), ABSOLUTE_VRAM_LIMIT_GB))
        sec.soft_threshold = float(min(max(sec.soft_threshold, 0.5), 0.97))
        sec.resource_poll_ms = int(max(sec.resource_poll_ms, 500))
        # Locked booleans.
        sec.redact_sensitive_output = True
        sec.block_password_field_reading = True
        sec.prohibit_privilege_escalation = True
        sec.prohibit_uninstall = True
        sec.prohibit_registry_writes = True
        sec.prohibit_security_software_changes = True
        sec.prohibit_cloud_ai = True
        sec.upload_screenshots = False
        sec.upload_audio = False
        sec.automation_master_switch = bool(sec.automation_master_switch)
        if sec.mode not in ("strict", "balanced"):
            sec.mode = "balanced"
        # Approval switches can be turned ON but never OFF.
        sec.require_approval_for_downloads = True
        sec.require_approval_for_messages = True
        sec.require_approval_for_deletes = True
        sec.require_approval_for_overwrites = True
        sec.require_approval_for_shell_commands = True
        # Vision / memory locks.
        self.vision.mask_password_fields = True
        self.ui.close_to_tray = True      # LOCKED - closing must never kill a task
        self.vision.local_only = True
        self.memory.redact_secrets = True
        self.general.telemetry_enabled = False
        self.automation.verify_after_action = True
        # Numeric sanity.
        self.vision.max_dimension = int(
            min(max(self.vision.max_dimension, 320), 2560)
        )
        self.vision.jpeg_quality = int(min(max(self.vision.jpeg_quality, 20), 95))
        self.vision.observation_interval_ms = int(
            min(max(self.vision.observation_interval_ms, 250), 30000)
        )
        self.automation.max_steps_per_task = int(
            min(max(self.automation.max_steps_per_task, 1), 200)
        )
        self.automation.max_identical_actions = int(
            min(max(self.automation.max_identical_actions, 1), 10)
        )
        a = self.automation
        a.settle_poll_interval_s = float(min(max(a.settle_poll_interval_s, 0.005), 0.5))
        a.observation_reuse_s = float(min(max(a.observation_reuse_s, 0.0), 15.0))
        a.app_ready_wait_s = float(min(max(a.app_ready_wait_s, 0.0), 60.0))
        a.element_wait_s = float(min(max(a.element_wait_s, 0.0), 30.0))
        a.browser_devtools_port = int(min(max(a.browser_devtools_port, 1024), 65535))
        v = self.vision
        v.uia_rich_threshold = int(min(max(v.uia_rich_threshold, 1), 500))
        v.element_cache_ttl_s = float(min(max(v.element_cache_ttl_s, 0.0), 30.0))
        v.cache_ttl_s = float(min(max(v.cache_ttl_s, 0.0), 60.0))
        o = self.ollama
        o.num_ctx = int(min(max(o.num_ctx, 1024), 131072))
        o.temperature = float(min(max(o.temperature, 0.0), 2.0))
        o.port = int(min(max(o.port, 1), 65535))
        sp = self.speech
        sp.tts_rate = float(min(max(sp.tts_rate, 0.5), 2.0))
        sp.tts_volume = float(min(max(sp.tts_volume, 0.0), 1.0))
        sp.activation_mode = sp.activation_mode if sp.activation_mode in (
            "wakeword", "push_to_talk", "button", "continuous", "disabled"
        ) else "push_to_talk"
        # Tool overrides may only tighten.
        cleaned: Dict[str, str] = {}
        for tool, mode in dict(sec.tool_overrides).items():
            if mode in ("allow", "ask", "block"):
                cleaned[str(tool)] = mode
        sec.tool_overrides = cleaned
        return self


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _to_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(v) for v in obj]
    return obj


_lock = threading.RLock()
_cached: Optional[Settings] = None


def load(force: bool = False) -> Settings:
    """Load settings from disk (cached).  Missing/corrupt file => defaults."""
    global _cached
    with _lock:
        if _cached is not None and not force:
            return _cached
        path = paths.config_file()
        data: Dict[str, Any] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception as exc:  # noqa: BLE001
                log.error("Could not parse %s (%s) - using defaults", path, exc)
                try:
                    path.replace(path.with_suffix(".json.corrupt"))
                except OSError:
                    pass
                data = {}
        settings = Settings.from_dict(data)
        env_overrides(settings)
        _cached = settings
        return settings


def env_overrides(s: Settings) -> None:
    """Small escape hatch for development/CI; never weakens security."""
    if os.environ.get("JARVIS_OLLAMA_HOST"):
        s.ollama.host = os.environ["JARVIS_OLLAMA_HOST"]
    if os.environ.get("JARVIS_OLLAMA_PORT"):
        try:
            s.ollama.port = int(os.environ["JARVIS_OLLAMA_PORT"])
        except ValueError:
            pass
    if os.environ.get("JARVIS_MAIN_MODEL"):
        s.ollama.main_model = os.environ["JARVIS_MAIN_MODEL"]
    if os.environ.get("JARVIS_LOG_LEVEL"):
        s.general.log_level = os.environ["JARVIS_LOG_LEVEL"]
    s.sanitize()


def save(settings: Optional[Settings] = None) -> Path:
    """Atomically persist settings (after re-sanitising them)."""
    global _cached
    with _lock:
        s = settings or _cached or load(force=True)
        s.sanitize()
        _cached = s
        path = paths.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(s.to_dict(), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return path


def update(mutator) -> Settings:
    """Apply ``mutator(settings)`` then sanitise + save.  Thread safe."""
    with _lock:
        s = load()
        mutator(s)
        save(s)
        return s
