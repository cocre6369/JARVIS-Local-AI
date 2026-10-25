"""Generate JARVIS's application icon with nothing but the standard library.

A packaged Windows app needs a real ``.ico`` (the taskbar, the installer, the
desktop shortcut and the exe resource all use it).  Rather than commit a binary
blob that nobody can regenerate, this script *draws* the icon: the same animated
core the interface renders, frozen into concentric rings on a dark plate, then
writes a multi-resolution ICO.

Usage::

    python packaging/make_icon.py                 # -> packaging/assets/jarvis.ico
    python packaging/make_icon.py out.ico 512     # custom path / base size

Only ``zlib``, ``struct`` and ``math`` are used, so it runs anywhere Python
runs - including the Windows build agent, before PyInstaller.
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path
from typing import List, Sequence, Tuple

#: Icon sizes packed into one .ico.  Windows picks the closest match.
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

#: Colours taken straight from jarvis/ui/theme.py so the icon matches the app.
BG_OUTER = (4, 7, 12)
BG_INNER = (9, 16, 26)
ACCENT = (34, 211, 238)          # cyan
SECONDARY = (59, 130, 246)       # blue
CORE = (224, 250, 255)


# ---------------------------------------------------------------------------
# PNG writing (stdlib only)
# ---------------------------------------------------------------------------
def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(width: int, height: int, rgba: bytes) -> bytes:
    """Encode an RGBA pixel buffer as a PNG file in memory."""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter: none
        raw += rgba[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def ico_bytes(images: Sequence[Tuple[int, bytes]]) -> bytes:
    """Pack ``(size, png)`` pairs into a Windows ICO file."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    entries = bytearray()
    payload = bytearray()
    offset = len(header) + 16 * count
    for size, png in images:
        dimension = 0 if size >= 256 else size          # 0 means 256 in ICO
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                               len(png), offset)
        payload += png
        offset += len(png)
    return bytes(header) + bytes(entries) + bytes(payload)


# ---------------------------------------------------------------------------
# drawing
# ---------------------------------------------------------------------------
def _mix(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _ring_intensity(distance: float, radius: float, thickness: float) -> float:
    """1.0 on the ring, falling off smoothly either side."""
    delta = abs(distance - radius)
    if delta >= thickness:
        return 0.0
    return math.cos((delta / thickness) * (math.pi / 2.0)) ** 2


def draw_core(size: int) -> bytes:
    """Render the JARVIS core at ``size`` x ``size`` as RGBA bytes."""
    pixels = bytearray()
    centre = (size - 1) / 2.0
    plate_radius = centre * 0.98
    rings = [
        (0.86, 0.055, SECONDARY, 0.55),
        (0.70, 0.045, ACCENT, 0.95),
        (0.52, 0.035, ACCENT, 0.60),
        (0.34, 0.030, SECONDARY, 0.45),
    ]
    core_radius = size * 0.20
    spoke_count = 12
    for y in range(size):
        for x in range(size):
            dx = x - centre
            dy = y - centre
            distance = math.hypot(dx, dy)
            if distance > plate_radius:
                pixels.extend((0, 0, 0, 0))            # transparent outside
                continue

            # rounded dark plate with a faint vertical sheen
            shade = 1.0 - (distance / plate_radius) * 0.35 + (dy / size) * 0.10
            colour = _mix(BG_OUTER, BG_INNER, shade)
            glow = 0.0

            for radius_fraction, thickness_fraction, tint, strength in rings:
                intensity = _ring_intensity(distance, size * radius_fraction / 2.0,
                                            size * thickness_fraction)
                if intensity:
                    glow = max(glow, intensity * strength)
                    colour = _mix(colour, tint, intensity * strength * 0.85)

            # radial spokes, like the reactor's housing
            if distance > size * 0.24 and distance < size * 0.46:
                angle = math.atan2(dy, dx)
                spokes = abs(math.sin(angle * spoke_count / 2.0))
                if spokes > 0.965:
                    colour = _mix(colour, ACCENT, 0.35)

            # the core itself: white-hot centre fading to cyan
            if distance < core_radius * 1.9:
                falloff = max(0.0, 1.0 - distance / (core_radius * 1.9))
                colour = _mix(colour, ACCENT, falloff ** 1.6 * 0.9)
                if distance < core_radius:
                    inner = max(0.0, 1.0 - distance / core_radius)
                    colour = _mix(colour, CORE, inner ** 1.4)
                    glow = max(glow, inner)

            # anti-alias the plate edge
            alpha = 255
            edge = plate_radius - distance
            if edge < 1.5:
                alpha = int(max(0.0, min(1.0, edge / 1.5)) * 255)

            if glow:
                colour = _mix(colour, CORE, glow * 0.25)
            pixels.extend((max(0, min(255, colour[0])),
                           max(0, min(255, colour[1])),
                           max(0, min(255, colour[2])),
                           alpha))
    return bytes(pixels)


def build_icon(target: Path, base_size: int = 256,
               sizes: Sequence[int] = SIZES) -> Path:
    """Draw every size and write one .ico.  Returns the path written."""
    master = draw_core(base_size)
    images: List[Tuple[int, bytes]] = [(base_size, png_bytes(base_size, base_size,
                                                             master))]
    for size in sizes:
        if size >= base_size:
            continue
        images.append((size, png_bytes(size, size, _resample(master, base_size, size))))
    images.sort(key=lambda item: item[0])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ico_bytes(images))
    return target


def _resample(rgba: bytes, src: int, dst: int) -> bytes:
    """Box-filter downscale.  Small, dependency-free, good enough for an icon."""
    out = bytearray(dst * dst * 4)
    scale = src / dst
    for y in range(dst):
        y0 = int(y * scale)
        y1 = max(y0 + 1, int((y + 1) * scale))
        for x in range(dst):
            x0 = int(x * scale)
            x1 = max(x0 + 1, int((x + 1) * scale))
            totals = [0, 0, 0, 0]
            count = 0
            for sy in range(max(0, y0), min(src, y1)):
                row = sy * src * 4
                for sx in range(max(0, x0), min(src, x1)):
                    base = row + sx * 4
                    totals[0] += rgba[base]
                    totals[1] += rgba[base + 1]
                    totals[2] += rgba[base + 2]
                    totals[3] += rgba[base + 3]
                    count += 1
            if not count:
                continue
            base = (y * dst + x) * 4
            out[base] = totals[0] // count
            out[base + 1] = totals[1] // count
            out[base + 2] = totals[2] // count
            out[base + 3] = totals[3] // count
    return bytes(out)


def build_png(target: Path, size: int = 512) -> Path:
    """A plain PNG of the same art (used by the installer and the README)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png_bytes(size, size, draw_core(size)))
    return target


def main(argv: Sequence[str]) -> int:
    here = Path(__file__).resolve().parent
    target = Path(argv[0]) if argv else here / "assets" / "jarvis.ico"
    base = int(argv[1]) if len(argv) > 1 else 256
    built = build_icon(target, base_size=base)
    png = build_png(built.with_suffix(".png"), size=256)
    print(f"wrote {built} ({built.stat().st_size / 1024:.1f} KB, sizes "
          f"{', '.join(str(s) for s in sorted(s for s in SIZES if s < base))}, {base})")
    print(f"wrote {png} ({png.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
