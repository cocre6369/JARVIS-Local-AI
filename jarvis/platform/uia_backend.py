"""UI element discovery.

Three tiers, best first:

``uiautomation``  The Windows UI Automation tree.  Gives real control types,
                  automation ids, values, ``IsPassword`` and pattern support -
                  by far the most reliable way to find a button, and it means
                  JARVIS does not have to depend on fixed screen coordinates.

``ocr``           When UI Automation is unavailable (very old Windows, a
                  remote session, a game), JARVIS falls back to reading the
                  pixels: every recognised word becomes a clickable element
                  with a real bounding box.  Slower and less semantic, but it
                  genuinely locates interface elements from natural language.

``none``          Honest unavailability - the tool reports that it cannot see
                  the interface instead of inventing elements.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import Rect, UIElement, WindowInfo

log = logging.getLogger(__name__)

UIA_IS_PASSWORD_PROPERTY_ID = 30047
MAX_HANDLE_CACHE = 4000


class _HandleRegistry:
    """Keeps live references to native controls so UIElement.handle stays valid."""

    def __init__(self, limit: int = MAX_HANDLE_CACHE) -> None:
        self._map: Dict[int, Any] = {}
        self._order: List[int] = []
        self._next = 1
        self._lock = threading.RLock()
        self._limit = limit

    def put(self, obj: Any) -> int:
        with self._lock:
            for key, val in self._map.items():
                if val is obj:
                    return key
            key = self._next
            self._next += 1
            self._map[key] = obj
            self._order.append(key)
            while len(self._order) > self._limit:
                old = self._order.pop(0)
                self._map.pop(old, None)
            return key

    def get(self, key: Any) -> Any:
        try:
            k = int(key)
        except (TypeError, ValueError):
            return None
        with self._lock:
            return self._map.get(k)


def _rect_of(control: Any) -> Rect:
    br = getattr(control, "BoundingRectangle", None)
    if br is None:
        return Rect(0, 0, 0, 0)
    try:
        left = int(getattr(br, "left", 0))
        top = int(getattr(br, "top", 0))
        right = int(getattr(br, "right", 0))
        bottom = int(getattr(br, "bottom", 0))
        return Rect(left, top, max(0, right - left), max(0, bottom - top))
    except Exception:  # noqa: BLE001
        return Rect(0, 0, 0, 0)


def _is_password(control: Any) -> bool:
    for attr in ("IsPassword",):
        val = getattr(control, attr, None)
        if val is not None:
            try:
                return bool(val() if callable(val) else val)
            except Exception:  # noqa: BLE001
                pass
    for getter in ("GetPropertyValue",):
        fn = getattr(control, getter, None)
        if fn is None:
            continue
        try:
            return bool(fn(UIA_IS_PASSWORD_PROPERTY_ID))
        except Exception:  # noqa: BLE001
            continue
    return False


def _patterns_of(control: Any) -> List[str]:
    out = []
    probes = (
        ("invoke", ("GetInvokePattern", "IsInvokePatternAvailable")),
        ("toggle", ("GetTogglePattern", "IsTogglePatternAvailable")),
        ("set_value", ("GetValuePattern", "IsValuePatternAvailable")),
        ("expand", ("GetExpandCollapsePattern", "IsExpandCollapsePatternAvailable")),
        ("select", ("GetSelectionItemPattern", "IsSelectionItemPatternAvailable")),
        ("scroll", ("GetScrollPattern", "IsScrollPatternAvailable")),
        ("range", ("GetRangeValuePattern", "IsRangeValuePatternAvailable")),
    )
    for name, attrs in probes:
        for attr in attrs:
            fn = getattr(control, attr, None)
            if fn is None:
                continue
            try:
                if attr.startswith("Is"):
                    val = fn() if callable(fn) else fn
                    if val:
                        out.append(name)
                        break
                else:
                    if fn() is not None:
                        out.append(name)
                        break
            except Exception:  # noqa: BLE001
                continue
    return out or ["invoke"]


def _value_of(control: Any) -> str:
    for attr in ("GetValuePattern", "GetTextPattern", "GetLegacyIAccessiblePattern"):
        fn = getattr(control, attr, None)
        if fn is None:
            continue
        try:
            pattern = fn()
            if pattern is None:
                continue
            if attr == "GetValuePattern":
                val = getattr(pattern, "Value", None)
                if val is not None:
                    return str(val() if callable(val) else val)[:400]
            if attr == "GetLegacyIAccessiblePattern":
                val = getattr(pattern, "Value", None)
                if val is not None:
                    return str(val() if callable(val) else val)[:400]
        except Exception:  # noqa: BLE001
            continue
    return ""


class UiautomationUIA:
    name = "uiautomation"

    def __init__(self, max_depth: int = 12) -> None:
        import uiautomation as auto  # type: ignore

        self._auto = auto
        self._handles = _HandleRegistry()
        self.max_depth = int(max_depth)
        try:
            auto.SetGlobalSearchTimeout(0.6)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    def _root_for(self, window: Optional[WindowInfo]) -> Any:
        auto = self._auto
        if window is not None and window.handle:
            try:
                ctrl = auto.ControlFromHandle(int(window.handle))
                if ctrl is not None:
                    return ctrl
            except Exception as exc:  # noqa: BLE001
                log.debug("ControlFromHandle failed: %s", exc)
        try:
            return auto.GetFocusedControl() or auto.GetRootControl()
        except Exception:  # noqa: BLE001
            return auto.GetRootControl()

    def elements(self, root: Optional[WindowInfo] = None,
                 max_count: int = 150) -> List[UIElement]:
        start = time.time()
        control = self._root_for(root)
        out: List[UIElement] = []
        self._walk(control, 0, out, max_count, time.time() + 2.5)
        log.debug("UIA discovered %d elements in %.0f ms", len(out),
                  (time.time() - start) * 1000)
        return out

    def _walk(self, control: Any, depth: int, out: List[UIElement],
              max_count: int, deadline: float) -> None:
        if len(out) >= max_count or time.time() > deadline:
            return
        try:
            child = control.GetFirstChildControl()
        except Exception:  # noqa: BLE001
            return
        while child is not None:
            if len(out) >= max_count or time.time() > deadline:
                return
            try:
                name = str(getattr(child, "Name", "") or "")
                ctype = str(getattr(child, "ControlTypeName", "") or "")
                offscreen = bool(getattr(child, "IsOffscreen", False))
                rect = _rect_of(child)
                enabled = bool(getattr(child, "IsEnabled", True))
                interesting = (
                    (name.strip() or ctype in ("Edit", "Button", "CheckBox",
                                               "ComboBox", "ListItem", "MenuItem",
                                               "TabItem", "Hyperlink", "Slider"))
                    and rect.width > 0 and rect.height > 0
                    and not offscreen
                )
                if interesting:
                    out.append(UIElement(
                        name=name[:160], control_type=ctype, rect=rect,
                        automation_id=str(getattr(child, "AutomationId", "") or "")[:80],
                        class_name=str(getattr(child, "ClassName", "") or "")[:80],
                        value=("" if _is_password(child) else _value_of(child)),
                        is_enabled=enabled, is_visible=not offscreen,
                        is_focused=False, is_password=_is_password(child),
                        depth=depth, source="uia",
                        handle=self._handles.put(child),
                        patterns=_patterns_of(child),
                    ))
                if depth + 1 < self.max_depth:
                    self._walk(child, depth + 1, out, max_count, deadline)
            except Exception:  # noqa: BLE001
                log.debug("UIA walk error", exc_info=True)
            try:
                child = child.GetNextSiblingControl()
            except Exception:  # noqa: BLE001
                return

    def element_at(self, x: int, y: int) -> Optional[UIElement]:
        try:
            control = self._auto.ControlFromPoint(int(x), int(y))
        except Exception as exc:  # noqa: BLE001
            log.debug("ControlFromPoint failed: %s", exc)
            return None
        if control is None:
            return None
        return UIElement(
            name=str(getattr(control, "Name", "") or "")[:160],
            control_type=str(getattr(control, "ControlTypeName", "") or ""),
            rect=_rect_of(control),
            automation_id=str(getattr(control, "AutomationId", "") or ""),
            class_name=str(getattr(control, "ClassName", "") or ""),
            value=("" if _is_password(control) else _value_of(control)),
            is_enabled=bool(getattr(control, "IsEnabled", True)),
            is_visible=not bool(getattr(control, "IsOffscreen", False)),
            is_password=_is_password(control), source="uia",
            handle=self._handles.put(control), patterns=_patterns_of(control),
        )

    def focused(self) -> Optional[UIElement]:
        try:
            control = self._auto.GetFocusedControl()
        except Exception:  # noqa: BLE001
            return None
        if control is None:
            return None
        pwd = _is_password(control)
        return UIElement(
            name=str(getattr(control, "Name", "") or "")[:160],
            control_type=str(getattr(control, "ControlTypeName", "") or ""),
            rect=_rect_of(control),
            automation_id=str(getattr(control, "AutomationId", "") or ""),
            value=("[REDACTED-PASSWORD-FIELD]" if pwd else _value_of(control)),
            is_focused=True, is_password=pwd, source="uia",
            handle=self._handles.put(control), patterns=_patterns_of(control),
        )

    # ------------------------------------------------------------------
    def invoke(self, el: UIElement) -> bool:
        control = self._handles.get(el.handle)
        if control is None:
            return False
        attempts = ("GetInvokePattern", "GetTogglePattern", "GetExpandCollapsePattern",
                    "GetSelectionItemPattern")
        for attr in attempts:
            fn = getattr(control, attr, None)
            if fn is None:
                continue
            try:
                pattern = fn()
                if pattern is None:
                    continue
                if attr == "GetInvokePattern":
                    pattern.Invoke(); return True
                if attr == "GetTogglePattern":
                    pattern.Toggle(); return True
                if attr == "GetExpandCollapsePattern":
                    pattern.Expand(); return True
                if attr == "GetSelectionItemPattern":
                    pattern.Select(); return True
            except Exception as exc:  # noqa: BLE001
                log.debug("pattern %s failed: %s", attr, exc)
        # last resort: click the centre of the control
        try:
            control.Click(0.5, 0.5)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("Click failed: %s", exc)
            return False

    def set_value(self, el: UIElement, text: str) -> bool:
        if el.is_password:
            log.warning("Refusing to write to a password field via UIA")
            return False
        control = self._handles.get(el.handle)
        if control is None:
            return False
        fn = getattr(control, "GetValuePattern", None)
        if fn is not None:
            try:
                pattern = fn()
                if pattern is not None:
                    pattern.SetValue(text)
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("SetValue failed: %s", exc)
        try:
            control.SetFocus()
        except Exception:  # noqa: BLE001
            return False
        return False        # caller falls back to synthetic typing


# ---------------------------------------------------------------------------
class OcrDerivedUIA:
    """Element discovery from OCR word boxes (no UI Automation available)."""

    name = "ocr"

    def __init__(self, ocr_backend: Any, screen_backend: Any,
                 input_backend: Optional[Any] = None,
                 settings_provider: Optional[Callable[[], Any]] = None) -> None:
        self._ocr = ocr_backend
        self._screen = screen_backend
        self._input = input_backend
        self._settings_provider = settings_provider
        self._cache: List[UIElement] = []
        self._cache_ts: float = 0.0
        self._cache_path: str = ""
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    def _refresh(self, root: Optional[WindowInfo] = None) -> List[UIElement]:
        with self._lock:
            fresh = (self._cache and time.time() - self._cache_ts < 1.2)
            if fresh:
                return self._cache
        from .. import paths

        img = None
        try:
            if root is not None and root.rect.width > 0:
                img = self._screen.capture(region=root.rect)
            else:
                img = self._screen.capture()
        except Exception as exc:  # noqa: BLE001
            log.debug("capture for OCR-UIA failed: %s", exc)
        if img is None:
            return []
        path = str(paths.screenshots_dir() / f"uia-ocr-{int(time.time() * 1000)}.png")
        try:
            img.save(path)
        except Exception as exc:  # noqa: BLE001
            log.debug("could not write OCR image: %s", exc)
            return []
        lang = "en"
        if self._settings_provider is not None:
            try:
                lang = self._settings_provider().vision.ocr_language or "en"
            except Exception:  # noqa: BLE001
                pass
        result = self._ocr.read(path, lang)
        els: List[UIElement] = []
        offset_x = root.rect.x if root is not None else 0
        offset_y = root.rect.y if root is not None else 0
        for w in result.words:
            text = (w.text or "").strip()
            if not text or len(text) > 90:
                continue
            els.append(UIElement(
                name=text, control_type="Text",
                rect=Rect(w.rect.x + offset_x, w.rect.y + offset_y,
                          w.rect.width, w.rect.height),
                value=text, source="ocr", patterns=["invoke"], handle=None,
            ))
        with self._lock:
            self._cache = els
            self._cache_ts = time.time()
            self._cache_path = path
        return els

    def elements(self, root: Optional[WindowInfo] = None,
                 max_count: int = 150) -> List[UIElement]:
        return self._refresh(root)[:max_count]

    def element_at(self, x: int, y: int) -> Optional[UIElement]:
        from .base import Point

        p = Point(int(x), int(y))
        for el in self._refresh():
            if el.rect.contains(p):
                return el
        return None

    def focused(self) -> Optional[UIElement]:
        return None

    def invoke(self, el: UIElement) -> bool:
        if self._input is None:
            return False
        c = el.rect.center
        try:
            self._input.click(c.x, c.y, "left", 1)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("OCR-UIA click failed: %s", exc)
            return False

    def set_value(self, el: UIElement, text: str) -> bool:
        if el.is_password or self._input is None:
            return False
        if not self.invoke(el):
            return False
        time.sleep(0.08)
        try:
            self._input.type_text(text, 0.01)
            return True
        except Exception:  # noqa: BLE001
            return False


class NullUIA:
    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def elements(self, root: Optional[WindowInfo] = None, max_count: int = 150):
        return []

    def element_at(self, x: int, y: int):
        return None

    def focused(self):
        return None

    def invoke(self, el) -> bool:
        return False

    def set_value(self, el, text: str) -> bool:
        return False


def create_uia_backend(preferred: str = "auto", ocr_backend: Any = None,
                       screen_backend: Any = None, input_backend: Any = None,
                       settings_provider: Optional[Callable[[], Any]] = None):
    errors: List[str] = []
    order = ["uiautomation", "ocr"] if preferred == "auto" else [preferred]
    for choice in order:
        try:
            if choice == "uiautomation":
                b = UiautomationUIA()
                return b, "Windows UI Automation"
            if choice == "ocr":
                if ocr_backend is None or getattr(ocr_backend, "name", "none") == "none":
                    errors.append("ocr fallback needs a working OCR engine")
                    continue
                if screen_backend is None:
                    errors.append("ocr fallback needs screen capture")
                    continue
                return (OcrDerivedUIA(ocr_backend, screen_backend, input_backend,
                                      settings_provider),
                        "OCR-derived element boxes")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{choice}: {exc}")
            log.debug("UIA backend %s unavailable: %s", choice, exc)
    return NullUIA("; ".join(errors)), ("; ".join(errors) or "no element discovery")
