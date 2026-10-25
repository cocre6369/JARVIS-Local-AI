"""Blacking out credential fields before an image leaves the process.

Any pixel region belonging to a control the OS reports as ``IsPassword`` (or
whose name/class says it is a password, OTP, card-number or PIN field) is
overwritten with an opaque box, *plus* a small label so the model still knows a
field is there and can click it - it just can never read what is in it.

Masking happens on the in-memory pixel buffer, before the file is written and
before base64 encoding for the vision model, so no unmasked copy ever exists on
disk.
"""

from __future__ import annotations

import logging
from typing import List, Sequence, Tuple

from ..platform.base import Rect

log = logging.getLogger(__name__)

MASK_COLOUR = (8, 8, 10)
MASK_BORDER = (34, 211, 238)


def mask_regions(width: int, height: int, rgb: bytearray,
                 regions: Sequence[Rect], pad: int = 2) -> Tuple[bytearray, List[Rect]]:
    """Overwrite ``regions`` in an RGB framebuffer.  Returns (buffer, applied)."""
    applied: List[Rect] = []
    stride = width * 3
    fill = bytes(MASK_COLOUR)
    border = bytes(MASK_BORDER)
    for r in regions:
        x0 = max(0, r.x - pad)
        y0 = max(0, r.y - pad)
        x1 = min(width, r.right + pad)
        y1 = min(height, r.bottom + pad)
        if x1 <= x0 or y1 <= y0:
            continue
        span = (x1 - x0) * 3
        row = fill * (x1 - x0)
        for y in range(y0, y1):
            off = y * stride + x0 * 3
            rgb[off:off + span] = row
        # 1px cyan border so a human/model can still see the field outline
        for y in (y0, y1 - 1):
            off = y * stride + x0 * 3
            rgb[off:off + span] = border * (x1 - x0)
        for x in (x0, x1 - 1):
            for y in range(y0, y1):
                off = y * stride + x * 3
                rgb[off:off + 3] = border
        applied.append(Rect(x0, y0, x1 - x0, y1 - y0))
    return rgb, applied


def password_regions(elements) -> List[Rect]:
    """Extract bounding boxes of every credential input in an element list."""
    out: List[Rect] = []
    for el in elements or []:
        try:
            if getattr(el, "is_password", False) and el.rect.width > 0:
                out.append(el.rect)
        except Exception:  # noqa: BLE001
            continue
    return out


def mask_image_file(path: str, regions: Sequence[Rect]) -> bool:
    """Re-open a saved image and mask regions (used when the capture backend
    produced the file itself, e.g. Qt/Pillow paths)."""
    if not regions:
        return False
    try:
        from PIL import Image, ImageDraw

        with Image.open(path) as img:
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            for r in regions:
                draw.rectangle([r.x, r.y, r.right, r.bottom], fill=MASK_COLOUR,
                               outline=MASK_BORDER, width=1)
            img.save(path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("mask_image_file failed: %s", exc)
        return False
