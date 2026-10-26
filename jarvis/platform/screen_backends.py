"""Screen capture backends.

``mss``  - fastest cross-platform capture, used when installed.
``qt``   - QScreen.grabWindow, always available in the packaged app (PySide6
           is a hard dependency) and needs no extra wheel.
``win32``- BitBlt through ctypes; the fallback of last resort on Windows.

All of them return a :class:`CapturedImage` holding raw RGB bytes plus size, so
the rest of the pipeline (resize, password masking, JPEG encode for the vision
model, perceptual hashing) never depends on which backend produced the pixels.
"""

from __future__ import annotations

import ctypes
import logging
import os
import struct
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .base import Rect

log = logging.getLogger(__name__)


@dataclass
class CapturedImage:
    width: int
    height: int
    rgb: bytes                 # tightly packed RGB, top-down
    backend: str = ""
    ts: float = 0.0
    monitor: int = 0

    @property
    def size(self) -> Tuple[int, int]:
        return (self.width, self.height)

    def crop(self, region: Rect) -> "CapturedImage":
        x0 = max(0, region.x); y0 = max(0, region.y)
        x1 = min(self.width, region.right); y1 = min(self.height, region.bottom)
        if x1 <= x0 or y1 <= y0:
            return self
        out = bytearray()
        stride = self.width * 3
        span = (x1 - x0) * 3
        for y in range(y0, y1):
            off = y * stride + x0 * 3
            out += self.rgb[off:off + span]
        return CapturedImage(x1 - x0, y1 - y0, bytes(out), self.backend + "+crop",
                             self.ts, self.monitor)

    # ------------------------------------------------------------------
    def save_png(self, path: str) -> str:
        """Dependency-free PNG writer (Pillow is used elsewhere when present,
        but capture must never fail because an optional wheel is missing)."""
        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        stride = self.width * 3
        raw = bytearray()
        for y in range(self.height):
            raw.append(0)
            raw += self.rgb[y * stride:(y + 1) * stride]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")
            fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height,
                                                8, 2, 0, 0, 0)))
            fh.write(chunk(b"IDAT", zlib.compress(bytes(raw), 6)))
            fh.write(chunk(b"IEND", b""))
        return path

    def save(self, path: str, quality: int = 80) -> str:
        """JPEG when Pillow is available (much smaller for the vision model),
        PNG otherwise."""
        try:
            from PIL import Image

            img = Image.frombytes("RGB", (self.width, self.height), self.rgb)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            if path.lower().endswith((".jpg", ".jpeg")):
                img.save(path, "JPEG", quality=int(quality), optimize=True)
            else:
                img.save(path)
            return path
        except Exception as exc:  # noqa: BLE001
            log.debug("Pillow unavailable (%s); writing PNG instead", exc)
            return self.save_png(os.path.splitext(path)[0] + ".png")

    def to_pil(self):
        from PIL import Image

        return Image.frombytes("RGB", (self.width, self.height), self.rgb)

    def resized(self, max_dimension: int) -> "CapturedImage":
        longest = max(self.width, self.height)
        if longest <= max(1, int(max_dimension)):
            return self
        scale = max_dimension / float(longest)
        nw, nh = max(1, int(self.width * scale)), max(1, int(self.height * scale))
        try:
            from PIL import Image

            img = Image.frombytes("RGB", (self.width, self.height), self.rgb)
            img = img.resize((nw, nh), Image.LANCZOS)
            return CapturedImage(nw, nh, img.tobytes(), self.backend + "+resized",
                                 self.ts, self.monitor)
        except Exception as exc:  # noqa: BLE001
            log.debug("Pillow resize unavailable (%s); returning full-size image", exc)
            return self

    def average_hash(self) -> str:
        """Cheap perceptual hash used to skip redundant re-analysis."""
        gw = gh = 16
        cells = [0] * (gw * gh)
        counts = [0] * (gw * gh)
        step_x = max(1, self.width // gw)
        step_y = max(1, self.height // gh)
        for gy in range(gh):
            for gx in range(gw):
                total = 0
                n = 0
                y = gy * step_y
                while y < min(self.height, (gy + 1) * step_y):
                    x = gx * step_x
                    base = y * self.width * 3
                    while x < min(self.width, (gx + 1) * step_x):
                        o = base + x * 3
                        total += self.rgb[o] + self.rgb[o + 1] + self.rgb[o + 2]
                        n += 1
                        x += max(1, step_x // 3)
                    y += max(1, step_y // 3)
                idx = gy * gw + gx
                cells[idx] = total // (n * 3) if n else 0
                counts[idx] = n
        avg = sum(cells) // max(1, len(cells))
        return hex(int("".join("1" if c > avg else "0" for c in cells), 2))[2:].zfill(64)


# ---------------------------------------------------------------------------
class MssScreen:
    name = "mss"

    def __init__(self) -> None:
        import mss  # type: ignore
        import mss.tools  # type: ignore

        self._mss = mss
        self._sct = None

    def _session(self):
        # mss instances are not thread safe across processes; one per thread.
        if self._sct is None:
            self._sct = self._mss.mss()
        return self._sct

    def monitors(self) -> List[Dict[str, Any]]:
        sct = self._session()
        out = []
        for i, m in enumerate(sct.monitors):
            if i == 0:
                continue                      # monitors[0] is the virtual "all"
            out.append({"index": i - 1, "name": f"monitor-{i}", "x": m["left"],
                        "y": m["top"], "width": m["width"], "height": m["height"],
                        "primary": i == 1})
        return out

    def size(self) -> Tuple[int, int]:
        mons = self.monitors()
        if not mons:
            return (1920, 1080)
        return (mons[0]["width"], mons[0]["height"])

    def capture(self, monitor: int = 0, region: Optional[Rect] = None) -> Optional[CapturedImage]:
        from PIL import Image  # mss returns BGRA; Pillow does the conversion

        sct = self._session()
        if region is not None:
            box = {"left": region.x, "top": region.y,
                   "width": region.width, "height": region.height}
        else:
            mons = sct.monitors
            box = mons[0] if monitor in (0, -1) else mons[min(monitor + 1, len(mons) - 1)]
        raw = sct.grab(box)
        img = Image.frombytes("RGBA", (raw.width, raw.height), raw.bgra, "raw", "BGRA")
        return CapturedImage(raw.width, raw.height, img.convert("RGB").tobytes(),
                             self.name, time.time(), monitor)


class QtScreen:
    name = "qt"

    def capture(self, monitor: int = 0, region: Optional[Rect] = None) -> Optional[CapturedImage]:
        try:
            from PySide6.QtGui import QGuiApplication, QImage
        except Exception as exc:  # noqa: BLE001
            log.debug("Qt capture unavailable: %s", exc)
            return None
        app = QGuiApplication.instance()
        if app is None:
            return None
        screens = app.screens()
        if not screens:
            return None
        screen = screens[0] if monitor in (0, -1) else screens[min(monitor, len(screens) - 1)]
        if region is not None:
            from PySide6.QtCore import QRect

            qrect = QRect(region.x, region.y, region.width, region.height)
            pixmap = screen.grabWindow(0, region.x, region.y, region.width, region.height)
            _ = qrect
        else:
            geo = screen.geometry()
            pixmap = screen.grabWindow(0, geo.x(), geo.y(), geo.width(), geo.height())
        image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB888)
        w, h = image.width(), image.height()
        buf = image.constBits()
        bpl = image.bytesPerLine()
        out = bytearray()
        for y in range(h):
            start = y * bpl
            out += bytes(buf[start:start + w * 3])
        return CapturedImage(w, h, bytes(out), self.name, time.time(), monitor)

    def monitors(self) -> List[Dict[str, Any]]:
        try:
            from PySide6.QtGui import QGuiApplication
        except Exception:  # noqa: BLE001
            return []
        app = QGuiApplication.instance()
        if app is None:
            return []
        out = []
        for i, s in enumerate(app.screens()):
            g = s.geometry()
            out.append({"index": i, "name": s.name(), "x": g.x(), "y": g.y(),
                        "width": g.width(), "height": g.height(),
                        "primary": i == 0})
        return out

    def size(self) -> Tuple[int, int]:
        mons = self.monitors()
        return (mons[0]["width"], mons[0]["height"]) if mons else (1920, 1080)


class Win32Screen:
    """BitBlt capture through ctypes - no dependencies at all."""

    name = "win32-gdi"

    SM_CXSCREEN, SM_CYSCREEN = 0, 1
    SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
    SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
    SRCCOPY = 0x00CC0020
    CAPTUREBLT = 0x40000000

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Win32 capture requires Windows")
        self._u = ctypes.windll.user32      # type: ignore[attr-defined]
        self._g = ctypes.windll.gdi32       # type: ignore[attr-defined]

    def monitors(self) -> List[Dict[str, Any]]:
        w = self._u.GetSystemMetrics(self.SM_CXSCREEN)
        h = self._u.GetSystemMetrics(self.SM_CYSCREEN)
        return [{"index": 0, "name": "primary", "x": 0, "y": 0,
                 "width": int(w), "height": int(h), "primary": True}]

    def size(self) -> Tuple[int, int]:
        return (int(self._u.GetSystemMetrics(self.SM_CXSCREEN)),
                int(self._u.GetSystemMetrics(self.SM_CYSCREEN)))

    def capture(self, monitor: int = 0, region: Optional[Rect] = None) -> Optional[CapturedImage]:
        W, H = self.size()
        if region is not None:
            x, y, w, h = region.x, region.y, region.width, region.height
        else:
            x, y, w, h = 0, 0, W, H
        w = max(1, min(w, W - x)); h = max(1, min(h, H - y))
        hdc_screen = self._u.GetDC(0)
        hdc_mem = self._g.CreateCompatibleDC(hdc_screen)
        bmp = self._g.CreateCompatibleBitmap(hdc_screen, w, h)
        try:
            self._g.SelectObject(hdc_mem, bmp)
            ok = self._g.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y,
                                self.SRCCOPY | self.CAPTUREBLT)
            if not ok:
                return None

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                            ("biBitCount", ctypes.c_uint16),
                            ("biCompression", ctypes.c_uint32),
                            ("biSizeImage", ctypes.c_uint32),
                            ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long),
                            ("biClrUsed", ctypes.c_uint32),
                            ("biClrImportant", ctypes.c_uint32)]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h            # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            got = self._g.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)
            if not got:
                return None
            bgra = buf.raw
            rgb = bytearray(w * h * 3)
            # BGRA -> RGB without numpy when numpy is absent.
            try:
                import numpy as np

                arr = np.frombuffer(bgra, dtype=np.uint8).reshape(h, w, 4)
                rgb = bytearray(arr[:, :, [2, 1, 0]].tobytes())
            except Exception:  # noqa: BLE001
                i = 0
                for p in range(0, len(bgra), 4):
                    rgb[i] = bgra[p + 2]; rgb[i + 1] = bgra[p + 1]; rgb[i + 2] = bgra[p]
                    i += 3
            return CapturedImage(w, h, bytes(rgb), self.name, time.time(), monitor)
        finally:
            self._g.DeleteObject(bmp)
            self._g.DeleteDC(hdc_mem)
            self._u.ReleaseDC(0, hdc_screen)


class NullScreen:
    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def capture(self, monitor: int = 0, region: Optional[Rect] = None):
        log.warning("Screen capture unavailable: %s", self.reason)
        return None

    def monitors(self) -> List[Dict[str, Any]]:
        return []

    def size(self) -> Tuple[int, int]:
        return (0, 0)


def create_screen_backend(preferred: str = "auto"):
    """Return (backend, detail)."""
    order = ["mss", "qt", "win32"] if preferred == "auto" else [preferred]
    errors = []
    for choice in order:
        try:
            if choice == "mss":
                return MssScreen(), "mss"
            if choice == "qt":
                b = QtScreen()
                from PySide6.QtGui import QGuiApplication

                if QGuiApplication.instance() is None:
                    errors.append("qt: no QApplication yet")
                    continue
                return b, "Qt QScreen.grabWindow"
            if choice == "win32":
                if os.name != "nt":
                    errors.append("win32 capture needs Windows")
                    continue
                return Win32Screen(), "Win32 BitBlt (ctypes)"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{choice}: {exc}")
            log.debug("Screen backend %s unavailable: %s", choice, exc)
    return NullScreen("; ".join(errors)), ("; ".join(errors) or "no capture backend")
