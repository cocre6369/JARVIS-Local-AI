"""Platform interfaces and value types.

Every piece of desktop access in JARVIS goes through one of these interfaces.
Concrete implementations live in :mod:`jarvis.platform.real` (Windows first,
with Linux/macOS fallbacks) and :mod:`jarvis.platform.simulated` (an in-process
desktop used by the test-suite and by the clearly-labelled Demo Mode).

Keeping the interfaces this narrow is what makes the agent loop testable: the
planner, the policy engine, the verification step and the observation cycle are
all exercised against a real object graph rather than mocks scattered across
the code base.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    def as_tuple(self) -> Tuple[int, int]:
        return (int(self.x), int(self.y))


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> Point:
        return Point(self.x + self.width // 2, self.y + self.height // 2)

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.right and self.y <= p.y < self.bottom

    def intersects(self, other: "Rect") -> bool:
        return not (other.x >= self.right or other.right <= self.x
                    or other.y >= self.bottom or other.bottom <= self.y)

    def as_dict(self) -> Dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class WindowInfo:
    title: str
    handle: Any = None
    process_name: str = ""
    pid: int = 0
    rect: Rect = Rect(0, 0, 0, 0)
    is_active: bool = False
    is_minimised: bool = False
    is_maximised: bool = False
    class_name: str = ""
    monitor: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title, "process_name": self.process_name, "pid": self.pid,
            "handle": str(self.handle) if self.handle is not None else "",
            "rect": self.rect.as_dict(), "is_active": self.is_active,
            "is_minimised": self.is_minimised, "is_maximised": self.is_maximised,
            "class_name": self.class_name,
        }

    def label(self) -> str:
        proc = self.process_name or "?"
        title = (self.title or "(untitled)")[:90]
        return f"{proc} :: {title}"


@dataclass
class ProcessInfo:
    pid: int
    name: str
    executable: str = ""
    memory_mb: float = 0.0
    cpu_percent: float = 0.0
    create_time: float = 0.0
    windows: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"pid": self.pid, "name": self.name, "executable": self.executable,
                "memory_mb": round(self.memory_mb, 1),
                "cpu_percent": round(self.cpu_percent, 1),
                "windows": list(self.windows)}


@dataclass
class UIElement:
    """One control discovered through UI Automation / accessibility / OCR."""
    name: str
    control_type: str = ""
    rect: Rect = Rect(0, 0, 0, 0)
    automation_id: str = ""
    class_name: str = ""
    value: str = ""
    is_enabled: bool = True
    is_visible: bool = True
    is_focused: bool = False
    is_password: bool = False
    depth: int = 0
    source: str = "uia"           # uia | ocr | vision | simulated
    handle: Any = None
    children_count: int = 0
    #: Patterns the control supports ("click", "set_value", "toggle"...).
    patterns: List[str] = field(default_factory=list)

    @property
    def center(self) -> Point:
        return self.rect.center

    def as_dict(self, include_handle: bool = False) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name, "control_type": self.control_type,
            "rect": self.rect.as_dict(), "automation_id": self.automation_id,
            "class_name": self.class_name, "is_enabled": self.is_enabled,
            "is_visible": self.is_visible, "is_focused": self.is_focused,
            "is_password": self.is_password, "source": self.source,
            "patterns": list(self.patterns),
        }
        # Values are truncated: an edit control can hold a whole document, and
        # a password field must never be echoed at all.
        if self.is_password:
            d["value"] = "[REDACTED-PASSWORD-FIELD]"
        else:
            d["value"] = (self.value or "")[:200]
        if include_handle and self.handle is not None:
            d["handle"] = str(self.handle)
        return d

    def signature(self) -> str:
        return f"{self.control_type}|{self.name}|{self.automation_id}"


@dataclass
class OcrWord:
    text: str
    rect: Rect
    confidence: float = 1.0


@dataclass
class OcrResult:
    words: List[OcrWord] = field(default_factory=list)
    lines: List[str] = field(default_factory=list)
    text: str = ""
    engine: str = ""
    duration_ms: float = 0.0
    image_size: Tuple[int, int] = (0, 0)

    def as_dict(self, max_lines: int = 120) -> Dict[str, Any]:
        return {
            "engine": self.engine, "text": self.text[:6000],
            "lines": self.lines[:max_lines], "duration_ms": round(self.duration_ms, 1),
            "word_count": len(self.words), "image_size": list(self.image_size),
        }


@dataclass
class Screenshot:
    path: str
    width: int
    height: int
    ts: float = field(default_factory=time.time)
    monitor: int = 0
    window_label: str = ""
    #: base64 jpeg for the vision model (filled in lazily)
    b64: str = ""
    perceptual_hash: str = ""
    masked_regions: List[Rect] = field(default_factory=list)
    simulated: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "width": self.width, "height": self.height,
                "monitor": self.monitor, "window_label": self.window_label,
                "perceptual_hash": self.perceptual_hash,
                "masked_regions": [r.as_dict() for r in self.masked_regions],
                "simulated": self.simulated, "ts": self.ts}


@dataclass
class Capability:
    """Result of probing one optional subsystem."""
    name: str
    available: bool
    backend: str = ""
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "available": self.available,
                "backend": self.backend, "detail": self.detail}


# ---------------------------------------------------------------------------
# Interfaces
# ---------------------------------------------------------------------------
class InputBackend(Protocol):
    name: str

    def move(self, x: int, y: int, duration: float = 0.0) -> bool: ...
    def click(self, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", clicks: int = 1) -> bool: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.3,
             button: str = "left") -> bool: ...
    def scroll(self, dx: int = 0, dy: int = -3,
               x: Optional[int] = None, y: Optional[int] = None) -> bool: ...
    def position(self) -> Point: ...
    def type_text(self, text: str, interval: float = 0.01) -> bool: ...
    def press(self, keys: Sequence[str]) -> bool: ...
    def key_down(self, key: str) -> bool: ...
    def key_up(self, key: str) -> bool: ...


class ScreenBackend(Protocol):
    name: str

    def capture(self, monitor: int = 0, region: Optional[Rect] = None) -> Optional[Any]: ...
    def size(self) -> Tuple[int, int]: ...
    def monitors(self) -> List[Dict[str, Any]]: ...


class WindowBackend(Protocol):
    name: str

    def windows(self) -> List[WindowInfo]: ...
    def active(self) -> Optional[WindowInfo]: ...
    def focus(self, win: WindowInfo) -> bool: ...
    def minimise(self, win: WindowInfo) -> bool: ...
    def maximise(self, win: WindowInfo) -> bool: ...
    def restore(self, win: WindowInfo) -> bool: ...
    def close(self, win: WindowInfo) -> bool: ...
    def move_resize(self, win: WindowInfo, rect: Rect) -> bool: ...
    def processes(self) -> List[ProcessInfo]: ...


class UIABackend(Protocol):
    name: str

    def elements(self, root: Optional[WindowInfo] = None,
                 max_count: int = 150) -> List[UIElement]: ...
    def element_at(self, x: int, y: int) -> Optional[UIElement]: ...
    def invoke(self, el: UIElement) -> bool: ...
    def set_value(self, el: UIElement, text: str) -> bool: ...
    def focused(self) -> Optional[UIElement]: ...


class OCRBackend(Protocol):
    name: str

    def read(self, image_path: str, language: str = "en") -> OcrResult: ...


class AppBackend(Protocol):
    name: str

    def installed(self) -> List[Dict[str, str]]: ...
    def resolve(self, query: str) -> List[Dict[str, str]]: ...
    def launch(self, target: str, arguments: Sequence[str] = ()) -> Dict[str, Any]: ...
