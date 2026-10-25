"""Model routing: which Ollama model handles which job.

Three roles exist (all optional, all configurable - nothing is hardcoded to a
particular model name):

``main``    reasoning, planning and tool calling.
``vision``  interpreting screenshots.
``fast``    cheap intent classification / re-ranking.

On an 8 GB card you usually cannot keep two 7B models resident at once, so the
router tracks which role is currently loaded and can evict the others through
:meth:`OllamaClient.unload`.  That keeps VRAM inside budget without the user
having to think about it.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .ollama_client import ModelInfo, OllamaClient

log = logging.getLogger(__name__)

ROLE_MAIN = "main"
ROLE_VISION = "vision"
ROLE_FAST = "fast"


@dataclass
class RoleResolution:
    role: str
    model: str
    reason: str = ""
    capabilities: Dict[str, bool] = field(default_factory=dict)
    ok: bool = True

    @property
    def usable(self) -> bool:
        return self.ok and bool(self.model)


class ModelRouter:
    def __init__(self, client: OllamaClient, settings_provider: Callable[[], Any],
                 single_resident_model: bool = True) -> None:
        self.client = client
        self._settings_provider = settings_provider
        self.single_resident_model = single_resident_model
        self._cap_cache: Dict[str, tuple] = {}
        self._loaded_role: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._last_switch: float = 0.0

    @property
    def settings(self):
        return self._settings_provider()

    # ------------------------------------------------------------------
    def capabilities(self, model: str) -> Dict[str, bool]:
        if not model:
            return {"tools": False, "vision": False, "embedding": False,
                    "completion": True}
        with self._lock:
            hit = self._cap_cache.get(model)
            if hit and time.time() - hit[1] < 300:
                return dict(hit[0])
        caps = self.client.capabilities(model)
        with self._lock:
            self._cap_cache[model] = (dict(caps), time.time())
        return caps

    def invalidate(self, model: Optional[str] = None) -> None:
        with self._lock:
            if model:
                self._cap_cache.pop(model, None)
            else:
                self._cap_cache.clear()

    # ------------------------------------------------------------------
    def resolve(self, role: str, needs: Optional[Dict[str, bool]] = None) -> RoleResolution:
        """Pick the model for ``role``, applying sensible fallbacks."""
        s = self.settings.ollama
        needs = needs or {}
        wanted = {ROLE_MAIN: s.main_model, ROLE_VISION: s.vision_model,
                  ROLE_FAST: s.fast_model or s.main_model}.get(role, "")

        if role == ROLE_VISION:
            if wanted:
                caps = self.capabilities(wanted)
                if caps.get("vision"):
                    return self._finish(RoleResolution(role, wanted, "configured vision model", caps))
                log.warning("Configured vision model %s does not support images; "
                            "falling back.", wanted)
            main = s.main_model
            if main and self.capabilities(main).get("vision"):
                return self._finish(RoleResolution(
                    role, main, "main model supports vision", self.capabilities(main)))
            # Last resort: any installed vision-capable model.
            for m in self.client.list_models():
                if m.supports_vision and not m.is_embedding:
                    return self._finish(RoleResolution(
                        role, m.name, f"auto-selected vision model ({m.name})",
                        self.capabilities(m.name)))
            return RoleResolution(role, "", "no installed model supports vision - "
                                             "visual understanding is disabled",
                                  ok=False)

        if not wanted:
            return RoleResolution(role, "", f"no model configured for role '{role}'",
                                  ok=False)
        caps = self.capabilities(wanted)
        if needs.get("tools") and not caps.get("tools"):
            # Keep the model (the prompted protocol works) but record why.
            return self._finish(RoleResolution(
                role, wanted,
                "model has no native tool calling - using the JSON action protocol",
                caps))
        return self._finish(RoleResolution(role, wanted, "configured model", caps))

    def _finish(self, res: RoleResolution) -> RoleResolution:
        if self.single_resident_model and res.model:
            self._note_load(res.role, res.model)
        return res

    def _note_load(self, role: str, model: str) -> None:
        with self._lock:
            previous = self._loaded_role.get("active")
            self._loaded_role[role] = model
            self._loaded_role["active"] = model
            self._loaded_role["previous"] = previous or ""
            self._last_switch = time.time()

    # ------------------------------------------------------------------
    def evict_other_models(self, keep: str) -> List[str]:
        """Unload every configured model except ``keep`` (VRAM hygiene)."""
        evicted: List[str] = []
        s = self.settings.ollama
        for name in {s.main_model, s.vision_model, s.fast_model}:
            if name and name != keep:
                if self.client.unload(name):
                    evicted.append(name)
        if evicted:
            log.info("Evicted from VRAM to stay inside budget: %s", ", ".join(evicted))
        return evicted

    def auto_select_main(self) -> str:
        """Pick a good default main model from what is installed.

        Preference order: tool-calling instruct models around 3-8B (fast on an
        8 GB card), then any instruct/chat model, then anything at all.
        """
        models = self.client.list_models()
        if not models:
            return ""
        usable = [m for m in models if not m.is_embedding]
        if not usable:
            return models[0].name

        def score(m: ModelInfo) -> tuple:
            name = m.name.lower()
            s = 0
            if m.supports_tools:
                s += 8
            if any(k in name for k in ("instruct", "chat", "it:", "-it")):
                s += 4
            if any(k in name for k in ("qwen2.5", "llama3.1", "llama3.2",
                                       "mistral-nemo", "gemma3", "phi4", "qwen3")):
                s += 3
            if m.is_embedding:
                s -= 20
            gb = m.size_gb
            if 1.5 <= gb <= 6.5:      # fits comfortably in 8 GB with context
                s += 5
            elif gb > 9:
                s -= 4
            if any(k in name for k in ("7b", "8b", "3b", "4b", "instruct")):
                s += 1
            return (-s, m.name)

        return sorted(usable, key=score)[0].name

    def describe(self) -> Dict[str, Any]:
        """Snapshot for the UI status bar / models page."""
        out: Dict[str, Any] = {"roles": {}, "installed": []}
        for role in (ROLE_MAIN, ROLE_VISION, ROLE_FAST):
            res = self.resolve(role)
            out["roles"][role] = {
                "model": res.model, "ok": res.ok, "reason": res.reason,
                "capabilities": res.capabilities,
            }
        out["installed"] = [m.as_dict() for m in self.client.list_models()]
        with self._lock:
            out["loaded"] = dict(self._loaded_role)
        out["running"] = self.client.running_models()
        return out
