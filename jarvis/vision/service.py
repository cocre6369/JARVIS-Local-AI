"""The observation service - what JARVIS actually *sees*.

One call, :meth:`VisionService.observe`, produces an :class:`Observation`:
a masked screenshot, the UI Automation element tree, OCR text, the window
inventory, and a set of derived signals (error dialogs, loading indicators,
sign-in walls).  That bundle is what the agent reasons over.

Performance and memory rules baked in here:

* **Never re-analyse an identical screen.**  The framebuffer is perceptually
  hashed; if nothing changed within ``cache_ttl_s`` the cached observation is
  returned and no OCR / model call happens.
* **Respect the resource guard.**  Under memory pressure the image is dropped
  entirely (elements + OCR are far cheaper) or downscaled harder.
* **Mask before writing.**  Password fields are blacked out in the pixel buffer
  *before* the file is created, so no unmasked screenshot ever touches disk.
* **Event-driven, not polling.**  Observation happens after an action, on
  explicit request, or on the configurable interval - never in a tight loop.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..platform.base import OcrResult, OcrWord, Rect, UIElement, WindowInfo
from . import masking
from .elements import (
    describe_elements,
    match_elements,
    ocr_words_to_elements,
    summarise_elements,
)

log = logging.getLogger(__name__)

ERROR_MARKERS = (
    "error", "failed", "failure", "cannot", "can't", "unable to", "not responding",
    "access denied", "permission denied", "exception", "crashed", "fatal",
    "invalid", "not found", "missing", "denied", "unauthorized", "forbidden",
    "trial expired", "license", "activation required", "something went wrong",
    "oops", "whoops", "no connection", "offline", "timeout", "timed out",
)
LOADING_MARKERS = (
    "loading", "please wait", "just a moment", "initialising", "initializing",
    "starting", "buffering", "updating", "installing", "preparing", "working",
    "connecting", "signing in", "authenticating", "%",
)
SIGNIN_MARKERS = (
    "sign in", "log in", "login", "create account", "subscribe", "premium",
    "password", "authentication required", "verify your", "two-factor",
    "2fa", "captcha", "i'm not a robot",
)


@dataclass
class Observation:
    ts: float = field(default_factory=time.time)
    # screen
    image_path: str = ""
    image_b64: str = ""
    width: int = 0
    height: int = 0
    perceptual_hash: str = ""
    changed: bool = True
    cached: bool = False
    simulated: bool = False
    masked_regions: List[Rect] = field(default_factory=list)
    # structure
    active_window: Dict[str, Any] = field(default_factory=dict)
    windows: List[Dict[str, Any]] = field(default_factory=list)
    elements: List[UIElement] = field(default_factory=list)
    ocr: OcrResult = field(default_factory=OcrResult)
    # derived
    signals: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    duration_ms: float = 0.0

    # ------------------------------------------------------------------
    @property
    def has_image(self) -> bool:
        return bool(self.image_b64 or self.image_path)

    def element_dicts(self, limit: int = 40) -> List[Dict[str, Any]]:
        return summarise_elements(self.elements, limit=limit)

    def find(self, query: str, limit: int = 6, min_score: float = 0.45,
             prefer_types: Sequence[str] = (),
             require_pattern: Optional[str] = None):
        screen = Rect(0, 0, self.width or 1920, self.height or 1080)
        return match_elements(query, self.elements, screen=screen,
                              min_score=min_score, limit=limit,
                              prefer_types=prefer_types,
                              require_pattern=require_pattern)

    def text(self) -> str:
        return self.ocr.text or ""

    def to_dict(self, include_elements: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "ts": self.ts, "width": self.width, "height": self.height,
            "changed": self.changed, "cached": self.cached,
            "simulated": self.simulated, "has_image": self.has_image,
            "image_path": self.image_path,
            "active_window": self.active_window,
            "windows": self.windows,
            "ocr_lines": self.ocr.lines[:80],
            "ocr_engine": self.ocr.engine,
            "signals": self.signals,
            "notes": self.notes,
            "duration_ms": round(self.duration_ms, 1),
            "element_count": len(self.elements),
            "masked_regions": len(self.masked_regions),
        }
        if include_elements:
            d["elements"] = self.element_dicts()
        return d

    def prompt_text(self, max_chars: int = 5200) -> str:
        """A compact textual description of the screen for the model."""
        parts: List[str] = []
        aw = self.active_window or {}
        if aw:
            parts.append(
                f"ACTIVE WINDOW: {aw.get('process_name', '?')} :: "
                f"{aw.get('title', '')}"
                + (" [minimised]" if aw.get("is_minimised") else "")
                + (" [maximised]" if aw.get("is_maximised") else ""))
        others = [w for w in self.windows if not w.get("is_active")]
        if others:
            parts.append("OTHER OPEN WINDOWS: "
                         + "; ".join(f"{w.get('process_name')}: {w.get('title')}"
                                     for w in others[:8]))
        sig = self.signals or {}
        if sig.get("errors"):
            parts.append("POSSIBLE ERRORS ON SCREEN: "
                         + "; ".join(sig["errors"][:5]))
        if sig.get("loading"):
            parts.append("SCREEN LOOKS LIKE IT IS STILL LOADING: "
                         + "; ".join(sig["loading"][:4]))
        if sig.get("signin"):
            parts.append("SIGN-IN / PAYWALL INDICATORS PRESENT (do not attempt "
                         "to enter credentials): " + "; ".join(sig["signin"][:4]))
        if self.masked_regions:
            parts.append(f"NOTE: {len(self.masked_regions)} password field(s) are "
                         "blacked out of the screenshot and their contents are "
                         "never read.")
        els = self.element_dicts(limit=34)
        if els:
            rendered = []
            for e in els:
                line = f"- {e['type']} \"{e['name']}\""
                if e.get("value"):
                    line += f" value=\"{e['value']}\""
                line += f" @({e['at'][0]},{e['at'][1]}) {e['size'][0]}x{e['size'][1]}"
                if e.get("focused"):
                    line += " FOCUSED"
                if e.get("disabled"):
                    line += " DISABLED"
                rendered.append(line)
            parts.append("VISIBLE CONTROLS (from UI Automation / OCR):\n"
                         + "\n".join(rendered))
        lines = [l for l in self.ocr.lines if l.strip()][:40]
        if lines:
            parts.append("SCREEN TEXT:\n" + "\n".join(lines))
        for n in self.notes:
            parts.append(f"NOTE: {n}")
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(observation truncated)"
        return text


# ---------------------------------------------------------------------------
class VisionService:
    def __init__(self,
                 settings_provider: Callable[[], Any],
                 platform: Any,
                 guard: Any = None,
                 bus: Any = None) -> None:
        self.settings_provider = settings_provider
        self.platform = platform
        self.guard = guard
        self.bus = bus
        self._last: Optional[Observation] = None
        self._last_hash: str = ""
        self._last_ts: float = 0.0
        self._cache_valid: bool = False
        #: ``window key -> (captured_at, elements)``.  Reading a control tree is
        #: the most expensive part of an observation, and inside
        #: ``automation.element_cache_ttl_s`` the same window rarely changes.
        self._elements_cache: Dict[str, "tuple"] = {}
        self._stats = {"observations": 0, "cache_hits": 0, "images": 0,
                       "ocr_calls": 0, "uia_calls": 0, "uia_cache_hits": 0,
                       "ocr_skipped": 0}

    # ------------------------------------------------------------------
    @property
    def settings(self):
        return self.settings_provider()

    @property
    def stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def invalidate(self) -> None:
        self._cache_valid = False
        self._last = None
        self._last_hash = ""
        self._elements_cache.clear()

    @property
    def last(self) -> Optional[Observation]:
        return self._last

    # ------------------------------------------------------------------
    def observe(self, window_title: str = "", include_image: bool = True,
                include_elements: bool = True, include_ocr: bool = True,
                force: bool = False, reason: str = "") -> Observation:
        """Capture and interpret the screen.  Never raises."""
        t0 = time.time()
        s = self.settings
        v = s.vision
        if not v.enabled:
            obs = Observation(notes=["Vision is disabled in settings."],
                              simulated=self.platform.is_simulated)
            obs.duration_ms = (time.time() - t0) * 1000
            return obs

        self._stats["observations"] += 1
        notes: List[str] = []

        # 1. window inventory -----------------------------------------
        windows: List[WindowInfo] = []
        try:
            windows = self.platform.windows.windows() or []
        except Exception as exc:  # noqa: BLE001
            notes.append(f"window enumeration failed: {exc}")
        active: Optional[WindowInfo] = None
        try:
            active = self.platform.windows.active()
        except Exception:  # noqa: BLE001
            pass
        if active is None and windows:
            active = next((w for w in windows if w.is_active), windows[0])
        if window_title:
            target = _pick_window(window_title, windows)
            if target is not None:
                active = target
            else:
                notes.append(f"no window matching '{window_title}' is open")

        # 2. UI elements -------------------------------------------------
        elements: List[UIElement] = []
        if include_elements and v.use_uia:
            cache_key = _window_key(active)
            ttl = float(getattr(v, "element_cache_ttl_s", 0.0) or 0.0)
            cached_elements: Optional[List[UIElement]] = None
            if ttl > 0 and cache_key and not force:
                hit = self._elements_cache.get(cache_key)
                if hit and (time.time() - float(hit[0])) < ttl:
                    cached_elements = list(hit[1])
            if cached_elements is not None:
                elements = cached_elements
                self._stats["uia_cache_hits"] += 1
                notes.append("Control tree reused from "
                             f"{ttl:.1f}s cache (no UI Automation call).")
            else:
                try:
                    elements = self.platform.uia.elements(
                        root=active, max_count=v.uia_max_elements) or []
                    self._stats["uia_calls"] += 1
                    if ttl > 0 and cache_key:
                        self._elements_cache[cache_key] = (time.time(), list(elements))
                        if len(self._elements_cache) > 12:
                            oldest = sorted(self._elements_cache.items(),
                                            key=lambda kv: kv[1][0])[:-12]
                            for key, _ in oldest:
                                self._elements_cache.pop(key, None)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"UI Automation unavailable: {exc}")
                    elements = []

        # 3. screenshot (masked before it is written) --------------------
        image_path = ""
        image_b64 = ""
        width = height = 0
        masked_regions: List[Rect] = []
        phash = ""
        want_image = include_image and self.guard_allows_image()
        if include_image and not want_image:
            notes.append("Screenshot skipped: memory pressure. Working from the "
                         "control tree and OCR text only.")
        if want_image:
            try:
                captured = None
                if active is not None and not active.is_minimised and active.rect.width > 8:
                    captured = self.platform.screen.capture(region=active.rect)
                    if captured is not None:
                        notes.append(f"Captured the '{active.title}' window only.")
                if captured is None:
                    captured = self.platform.screen.capture(monitor=v.capture_monitor)
                if captured is None:
                    notes.append("Screen capture returned nothing.")
                else:
                    width, height = captured.width, captured.height
                    rgb = bytearray(captured.rgb)
                    if v.mask_password_fields:
                        pw = masking.password_regions(elements)
                        if pw and active is not None:
                            # captured region is window-relative when we grabbed
                            # just the window; translate accordingly.
                            ox = active.rect.x if captured.width != self._screen_width() else 0
                            oy = active.rect.y if captured.height != self._screen_height() else 0
                            shifted = [Rect(r.x - ox, r.y - oy, r.width, r.height)
                                       for r in pw]
                            rgb, masked_regions = masking.mask_regions(
                                width, height, rgb, shifted)
                        elif pw:
                            rgb, masked_regions = masking.mask_regions(
                                width, height, rgb, pw)
                    if v.include_cursor and not self.platform.is_simulated:
                        # A real screen grab never contains the pointer, and the
                        # model often needs to know where it is ("click where the
                        # cursor already is").  Draw a small crosshair instead.
                        ox = active.rect.x if (active is not None
                                               and captured.width != self._screen_width()) else 0
                        oy = active.rect.y if (active is not None
                                               and captured.height != self._screen_height()) else 0
                        rgb = _draw_cursor(rgb, width, height, self.platform, ox, oy)
                    max_dim = self._target_dimension()
                    from ..platform.screen_backends import CapturedImage

                    img = CapturedImage(width, height, bytes(rgb),
                                        captured.backend, captured.ts, captured.monitor)
                    img = img.resized(max_dim)
                    if masked_regions and (img.width != width or img.height != height):
                        scale_x = img.width / float(width)
                        scale_y = img.height / float(height)
                        masked_regions = [
                            Rect(int(r.x * scale_x), int(r.y * scale_y),
                                 max(1, int(r.width * scale_x)),
                                 max(1, int(r.height * scale_y)))
                            for r in masked_regions]
                    width, height = img.width, img.height
                    from .. import paths

                    paths.prune_scratch(max_files=12, max_age_seconds=900)
                    image_path = str(paths.screenshots_dir()
                                     / f"obs-{int(time.time() * 1000)}.jpg")
                    image_path = img.save(image_path, quality=v.jpeg_quality)
                    if not image_path.lower().endswith((".jpg", ".jpeg")):
                        # Pillow was unavailable - the PNG writer was used.
                        pass
                    phash = img.average_hash()
                    image_b64 = self._encode_for_model(image_path, max_dim)
                    if image_b64:
                        self._stats["images"] += 1
            except Exception as exc:  # noqa: BLE001
                log.exception("screenshot pipeline failed")
                notes.append(f"screen capture failed: {exc}")

        # 4. OCR ----------------------------------------------------------
        ocr = OcrResult()
        skip_ocr = bool(getattr(v, "skip_ocr_when_uia_rich", False)) and \
            len(elements) >= int(getattr(v, "uia_rich_threshold", 20) or 0)
        if include_ocr and skip_ocr:
            # The control tree already told us what is on screen, with names and
            # positions.  OCR would only add latency and noise here, so the text
            # is derived from the controls instead - and we say so.
            ocr = _ocr_from_elements(elements)
            self._stats["ocr_skipped"] += 1
            notes.append(f"OCR skipped: UI Automation found {len(elements)} controls "
                         "(text derived from them instead).")
            include_ocr = False
        if include_ocr:
            try:
                if image_path:
                    ocr = self.platform.ocr.read(image_path, v.ocr_language) or OcrResult()
                else:
                    ocr = self.platform.ocr.read("", v.ocr_language) or OcrResult()
                self._stats["ocr_calls"] += 1
            except Exception as exc:  # noqa: BLE001
                notes.append(f"OCR unavailable: {exc}")
                ocr = OcrResult()
            if v.mask_password_fields and ocr.words:
                pw_rects = [e.rect for e in elements if e.is_password]
                if pw_rects:
                    ocr = _scrub_ocr(ocr, pw_rects)

        # UIA gave us nothing -> fall back to OCR-derived pseudo elements so
        # "find the settings button" still works from pixels alone.
        if include_elements and not elements and ocr.words:
            elements = ocr_words_to_elements(ocr.words)
            notes.append("UI Automation returned no controls; using OCR text boxes "
                         "as clickable elements.")

        # 5. change detection ---------------------------------------------
        changed = True
        cached = False
        now = time.time()
        if phash:
            same_hash = (phash == self._last_hash)
            fresh = (now - self._last_ts) < float(v.cache_ttl_s)
            if v.cache_screenshots and same_hash and fresh and not force:
                changed = False
                cached = True
                self._stats["cache_hits"] += 1
                notes.append("Screen unchanged since the last observation "
                             "(cached result reused).")
            self._last_hash = phash
            self._last_ts = now
        else:
            self._last_ts = now

        # 6. signals ---------------------------------------------------------
        signals = _detect_signals(ocr.text, elements, windows)

        obs = Observation(
            ts=now, image_path=image_path, image_b64=image_b64,
            width=width, height=height, perceptual_hash=phash,
            changed=changed, cached=cached,
            simulated=self.platform.is_simulated,
            masked_regions=masked_regions,
            active_window=(active.as_dict() if active else {}),
            windows=[w.as_dict() for w in windows][:24],
            elements=elements, ocr=ocr, signals=signals, notes=notes,
            duration_ms=(time.time() - t0) * 1000,
        )
        self._last = obs
        self._cache_valid = True
        if reason:
            log.debug("observation (%s): %d elements, %d ocr lines, %.0f ms",
                      reason, len(elements), len(ocr.lines), obs.duration_ms)
        if self.bus is not None:
            try:
                from ..events import EventType

                self.bus.publish(EventType.OBSERVATION, {
                    "elements": len(elements), "ocr_lines": len(ocr.lines),
                    "has_image": obs.has_image, "changed": changed,
                    "signals": signals, "duration_ms": obs.duration_ms,
                    "image_path": image_path,
                    "active_window": obs.active_window.get("title", ""),
                }, source="vision")
            except Exception:  # noqa: BLE001
                log.debug("observation event failed", exc_info=True)
        return obs

    # ------------------------------------------------------------------
    def _screen_width(self) -> int:
        try:
            return self.platform.screen.size()[0]
        except Exception:  # noqa: BLE001
            return 0

    def _screen_height(self) -> int:
        try:
            return self.platform.screen.size()[1]
        except Exception:  # noqa: BLE001
            return 0

    def _target_dimension(self) -> int:
        base = int(self.settings.vision.max_dimension)
        if self.guard is not None:
            try:
                return int(self.guard.recommended_image_size())
            except Exception:  # noqa: BLE001
                return base
        return base

    def guard_allows_image(self) -> bool:
        if self.guard is None:
            return True
        try:
            return bool(self.guard.may_run_vision())
        except Exception:  # noqa: BLE001
            return True

    def _encode_for_model(self, path: str, max_dim: int) -> str:
        """Base64 for Ollama, resized again for the vision model if needed."""
        if not path or not os.path.exists(path):
            return ""
        try:
            from PIL import Image

            with Image.open(path) as img:
                img = img.convert("RGB")
                longest = max(img.width, img.height)
                limit = min(int(max_dim), 1152)
                if longest > limit:
                    scale = limit / float(longest)
                    img = img.resize((max(1, int(img.width * scale)),
                                      max(1, int(img.height * scale))),
                                     Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=int(self.settings.vision.jpeg_quality),
                         optimize=True)
                return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            log.debug("image encode failed (%s); sending file bytes", exc)
        try:
            with open(path, "rb") as fh:
                return base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            return ""

    # ------------------------------------------------------------------
    def find_element(self, query: str, fresh: bool = False,
                     min_score: float = 0.45,
                     prefer_types: Sequence[str] = (),
                     require_pattern: Optional[str] = None) -> Dict[str, Any]:
        """Locate an element by natural language.  Re-observes when needed.

        ``prefer_types`` / ``require_pattern`` carry the *caller's intent* into
        the ranking, so an ambiguous name such as "search" resolves to the text
        field for a typing tool and to the button for a clicking tool.
        """
        obs = self._last
        if obs is None or fresh or not self._cache_valid:
            obs = self.observe(reason=f"find_element:{query}")
        matches = obs.find(query, limit=6, min_score=min_score,
                           prefer_types=prefer_types,
                           require_pattern=require_pattern)
        result: Dict[str, Any] = {
            "query": query,
            "found": bool(matches),
            # Rich objects - used by tools that need to click / invoke them.
            "matches": matches,
            # Plain dicts - what gets handed to the model or the UI.
            "match_dicts": [m.as_dict() for m in matches],
            "description": describe_elements(matches),
        }
        if not matches:
            result["hint"] = ("Nothing on screen matches that description. Try a "
                              "different wording, scroll, or observe a specific "
                              "application window.")
        return result

    #: Element control types that are window furniture, not content.
    _CHROME_TYPES = frozenset({"window", "titlebar", "menubar", "scrollbar",
                               "thumb", "splitbutton"})

    def _content_elements(self, obs: "Observation") -> int:
        """How many *content* elements an observation exposes.

        Window chrome (frame, title bar, scrollbars) does not count: a blank,
        still-loading app shell exposes exactly that and nothing else.
        """
        count = 0
        for el in obs.elements or []:
            ctype = (getattr(el, "control_type", "") or "").lower()
            if ctype in self._CHROME_TYPES:
                continue
            if getattr(el, "name", "") or ctype:
                count += 1
        return count

    def _content_pixels(self, obs: "Observation") -> float:
        """Std-dev of the target window's pixels: ~0 for a blank shell,
        high once real content has painted.  0.0 when no image is available.
        """
        try:
            from PIL import Image  # bundled dependency; guarded for headless CI

            raw = None
            if obs.image_b64:
                raw = io.BytesIO(base64.b64decode(obs.image_b64))
            elif obs.image_path and os.path.exists(obs.image_path):
                raw = obs.image_path
            if raw is None:
                return 0.0
            img = Image.open(raw).convert("L")
            rect = (obs.active_window or {}).get("rect") or {}
            w, h = img.size
            x = max(0, min(int(rect.get("x", 0)), w - 1))
            y = max(0, min(int(rect.get("y", 0)), h - 1))
            x2 = max(x + 1, min(x + int(rect.get("width", w)), w))
            y2 = max(y + 1, min(y + int(rect.get("height", h)), h))
            img = img.crop((x, y, x2, y2)).resize((48, 48))
            px = list(img.getdata())
            mean = sum(px) / len(px)
            var = sum((v - mean) ** 2 for v in px) / len(px)
            return var ** 0.5
        except Exception:  # noqa: BLE001 - a missing signal is never an error
            return 0.0

    def wait_until_ready(self, window_title: str = "", timeout_s: float = 10.0,
                         interval_s: float = 0.35, min_content: int = 2,
                         reason: str = "") -> Dict[str, Any]:
        """Wait until the target window has actually LOADED, not just appeared.

        "Loaded" means content - UIA elements beyond the window furniture, or
        real pixels inside the window rect - and a result that stays identical
        for two consecutive polls (loading spinners keep changing).  Never
        raises; on timeout it reports ready=False so the caller can proceed
        with open eyes instead of acting on a blank shell.
        """
        started = time.time()
        timeout_s = max(0.0, float(timeout_s))
        deadline = started + timeout_s
        last: Optional[Tuple[int, float]] = None
        stable = 0
        polls = 0
        elements = 0
        stddev = 0.0
        while True:
            obs = self.observe(window_title=window_title, include_image=True,
                               include_ocr=False, force=True,
                               reason=reason or "wait_until_ready")
            polls += 1
            elements = self._content_elements(obs)
            stddev = self._content_pixels(obs) if elements < min_content else 0.0
            has_content = elements >= min_content or stddev >= 6.0
            fingerprint = (elements, round(stddev, 1))
            if has_content:
                if fingerprint == last:
                    stable += 1
                else:
                    stable = 0
                if stable >= 1:  # two consecutive identical, content-bearing polls
                    return {"ready": True, "waited_s": round(time.time() - started, 2),
                            "polls": polls, "content_elements": elements,
                            "content_stddev": round(stddev, 2)}
            else:
                stable = 0
            last = fingerprint
            if time.time() >= deadline:
                return {"ready": False, "waited_s": round(time.time() - started, 2),
                        "polls": polls, "content_elements": elements,
                        "content_stddev": round(stddev, 2)}
            time.sleep(min(interval_s, max(0.05, deadline - time.time())))


# ---------------------------------------------------------------------------
def _draw_cursor(rgb: bytearray, width: int, height: int, platform: Any,
                 offset_x: int = 0, offset_y: int = 0) -> bytearray:
    """Paint a 13px crosshair at the pointer position into an RGB buffer.

    Cheap, dependency-free, and skipped silently when the position is unknown or
    outside the captured area.
    """
    try:
        point = platform.input.position()
        x = int(getattr(point, "x", 0)) - int(offset_x)
        y = int(getattr(point, "y", 0)) - int(offset_y)
    except Exception:  # noqa: BLE001
        return rgb
    if not (0 <= x < width and 0 <= y < height):
        return rgb

    def _pixel(px: int, py: int, colour=(255, 64, 64)) -> None:
        if 0 <= px < width and 0 <= py < height:
            base = (py * width + px) * 3
            if base + 2 < len(rgb):
                rgb[base] = colour[0]
                rgb[base + 1] = colour[1]
                rgb[base + 2] = colour[2]

    for delta in range(-6, 7):
        _pixel(x + delta, y)
        _pixel(x, y + delta)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            _pixel(x + dx, y + dy, (255, 255, 255))
    return rgb


def _window_key(window: Optional[WindowInfo]) -> str:
    """Stable identity for the control-tree cache.

    Title alone is not enough (two windows can share one), and a handle alone is
    not enough either (recycled by the OS), so both are used.
    """
    if window is None:
        return ""
    handle = getattr(window, "handle", None) or getattr(window, "hwnd", None) or ""
    pid = getattr(window, "pid", 0) or 0
    return f"{handle}|{pid}|{getattr(window, 'title', '')}"


def _ocr_from_elements(elements: Sequence[UIElement],
                       max_words: int = 600) -> OcrResult:
    """Build an :class:`OcrResult` out of a rich control tree.

    Used when OCR is skipped because UI Automation already described the screen.
    Password controls are never included - their values are not ours to read.
    """
    words: List[OcrWord] = []
    lines: List[str] = []
    for element in elements:
        if element.is_password:
            continue
        text = (element.name or "").strip()
        value = "" if element.is_password else (element.value or "").strip()
        pieces = [p for p in (text, value) if p]
        if not pieces:
            continue
        line = " ".join(pieces)[:200]
        lines.append(line)
        for piece in line.split():
            if len(words) >= max_words:
                break
            words.append(OcrWord(text=piece, rect=element.rect, confidence=1.0))
    return OcrResult(words=words, lines=lines[:400], text="\n".join(lines[:400]),
                     engine="ui-automation", duration_ms=0.0)


def _pick_window(query: str, windows: Sequence[WindowInfo]) -> Optional[WindowInfo]:
    if not query or not windows:
        return None
    q = query.strip().lower()
    exact = [w for w in windows if w.title.strip().lower() == q]
    if exact:
        return exact[0]
    contains = [w for w in windows
                if q in w.title.lower() or q in (w.process_name or "").lower()]
    if contains:
        return max(contains, key=lambda w: len(w.title))
    # token overlap
    best, best_score = None, 0.0
    for w in windows:
        hay = f"{w.title} {w.process_name}".lower()
        score = difflib_ratio(q, hay)
        if score > best_score:
            best, best_score = w, score
    return best if best_score >= 0.55 else None


def difflib_ratio(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio()


def _scrub_ocr(ocr: OcrResult, password_rects: Sequence[Rect]) -> OcrResult:
    """Drop any OCR word that falls inside a masked password field."""
    kept_words = []
    dropped = 0
    for w in ocr.words:
        inside = any(r.intersects(w.rect) for r in password_rects)
        if inside:
            dropped += 1
            continue
        kept_words.append(w)
    if not dropped:
        return ocr
    text = "\n".join(l for l in ocr.lines
                     if not any(_norm(l) in _norm(w.text) for w in ocr.words
                                if any(r.intersects(w.rect) for r in password_rects)))
    return OcrResult(words=kept_words, lines=text.splitlines(), text=text,
                     engine=ocr.engine, duration_ms=ocr.duration_ms,
                     image_size=ocr.image_size)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _detect_signals(ocr_text: str, elements: Sequence[UIElement],
                    windows: Sequence[WindowInfo]) -> Dict[str, Any]:
    """Cheap, deterministic screen-state heuristics.

    These run *before* the model is consulted so obvious states (a modal error
    dialog, a spinner, a login wall) are handled immediately instead of
    burning a generation on them.
    """
    hay_parts = [ocr_text or ""]
    hay_parts.extend((e.name or "") for e in elements or [])
    hay_parts.extend((e.value or "") for e in elements or [])
    hay_parts.extend((w.title or "") for w in windows or [])
    hay = _norm(" \n ".join(p for p in hay_parts if p))

    def hits(markers: Sequence[str]) -> List[str]:
        return sorted({m for m in markers if m in hay})[:8]

    errors = hits(ERROR_MARKERS)
    loading = hits(LOADING_MARKERS)
    signin = hits(SIGNIN_MARKERS)

    dialog_like = [w for w in windows or []
                   if w.rect.width and w.rect.width < 900 and w.rect.height < 600]
    return {
        "errors": errors,
        "loading": loading,
        "signin": signin,
        "dialog_present": bool(dialog_like),
        "dialog_titles": [w.title for w in dialog_like][:5],
        "password_field_present": any(e.is_password for e in elements or []),
        "element_count": len(elements or []),
        "has_text": bool(hay.strip()),
    }
