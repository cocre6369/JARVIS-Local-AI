"""Optical character recognition backends.

Priority on Windows:
1. **Windows.Media.Ocr** - built into Windows 10/11, no model download, fast,
   supports ``en-AU``/``en-GB``/``en-US`` language packs.  Accessed through the
   official ``winrt`` projections.
2. **Tesseract** via ``pytesseract`` - works if the user has the binary.
3. **RapidOCR** (ONNX PaddleOCR) - offline wheel, heavier but very accurate.

If none are available JARVIS still works: element discovery falls back to the
UI Automation tree, and the vision model can read the screenshot directly.  The
capability report in Settings shows exactly which of these is live.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from .base import OcrResult, OcrWord, Rect

log = logging.getLogger(__name__)

_LANGUAGE_MAP = {
    "en-AU": ["en-AU", "en-GB", "en-US", "en"],
    "en-GB": ["en-GB", "en-AU", "en-US", "en"],
    "en-US": ["en-US", "en-GB", "en"],
    "en": ["en-US", "en-GB", "en-AU", "en"],
}


def _await_winrt(op, timeout: float = 20.0):
    """Bridge to :func:`jarvis.platform.winrt_util.await_op`."""
    from .winrt_util import await_op

    return await_op(op, timeout=timeout)


class WindowsMediaOcr:
    name = "windows.media.ocr"

    def __init__(self, language: str = "en-AU") -> None:
        self._mods = self._import_modules()
        self._engines: Dict[str, Any] = {}
        self.language = self._resolve_language(language)

    @staticmethod
    def _import_modules() -> Dict[str, Any]:
        mods: Dict[str, Any] = {}
        import_candidates = [
            ("ocr", ["winrt.windows.media.ocr", "windows.media.ocr"]),
            ("imaging", ["winrt.windows.graphics.imaging",
                         "windows.graphics.imaging"]),
            ("streams", ["winrt.windows.storage.streams",
                         "windows.storage.streams"]),
            ("globalization", ["winrt.windows.globalization",
                               "windows.globalization"]),
        ]
        for key, paths in import_candidates:
            last = None
            for path in paths:
                try:
                    import importlib

                    mods[key] = importlib.import_module(path)
                    last = None
                    break
                except Exception as exc:  # noqa: BLE001
                    last = exc
            if key not in mods:
                raise ImportError(f"WinRT OCR projection missing ({key}): {last}")
        return mods

    def _resolve_language(self, language: str) -> str:
        gl = self._mods["globalization"]
        ocr = self._mods["ocr"]
        candidates = _LANGUAGE_MAP.get(language, [language, "en"])
        available = []
        try:
            available = [str(t.LanguageTag) for t in ocr.OcrEngine.get_available_recognizer_languages()]
        except Exception:  # noqa: BLE001
            pass
        for cand in candidates:
            try:
                lang = gl.Language(cand)
                engine = ocr.OcrEngine.try_create_from_language(lang)
                if engine is not None:
                    self._engines[cand] = engine
                    return cand
            except Exception:  # noqa: BLE001
                continue
        try:
            engine = ocr.OcrEngine.try_create_from_user_profile_languages()
            if engine is not None:
                tag = str(getattr(engine, "recognizer_language", language))
                self._engines[tag] = engine
                return tag
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"No Windows OCR language available: {exc}") from exc
        raise RuntimeError(
            "Windows OCR has no matching language pack"
            + (f" (available: {', '.join(available[:8])})" if available else ""))

    def read(self, image_path: str, language: str = "") -> OcrResult:
        t0 = time.time()
        ocr = self._mods["ocr"]
        imaging = self._mods["imaging"]
        streams = self._mods["streams"]
        engine = self._engines.get(language) or self._engines.get(self.language)
        if engine is None:
            raise RuntimeError("OCR engine not initialised")

        with open(image_path, "rb") as fh:
            data = fh.read()

        stream = streams.InMemoryRandomAccessStream()
        writer = streams.DataWriter(stream.get_output_stream_at(0))
        writer.write_bytes(bytearray(data))
        _await_winrt(writer.store_async())
        _await_winrt(writer.flush_async())
        writer.detach_stream()

        decoder = _await_winrt(imaging.BitmapDecoder.create_async(stream))
        bitmap = _await_winrt(decoder.get_software_bitmap_async(
            imaging.BitmapPixelFormat.B8G8R8A8,
            imaging.BitmapAlphaMode.PREMULTIPLIED))
        result = _await_winrt(engine.recognize_async(bitmap))

        words: List[OcrWord] = []
        lines: List[str] = []
        try:
            for line in result.get_lines_view():
                parts = []
                for word in line.get_words_view():
                    r = word.bounding_rect
                    rect = Rect(int(r.x), int(r.y), int(r.width), int(r.height))
                    text = str(word.text)
                    parts.append(text)
                    words.append(OcrWord(text=text, rect=rect, confidence=1.0))
                lines.append(" ".join(parts))
        except Exception:  # noqa: BLE001 - projection naming differences
            text_all = str(getattr(result, "text", "") or "")
            lines = [l for l in text_all.splitlines() if l.strip()]
        stream.close()
        size = (0, 0)
        try:
            bs = decoder.pixel_width, decoder.pixel_height
            size = (int(bs[0]), int(bs[1]))
        except Exception:  # noqa: BLE001
            pass
        return OcrResult(words=words, lines=lines, text="\n".join(lines),
                         engine=self.name, duration_ms=(time.time() - t0) * 1000,
                         image_size=size)


class TesseractOcr:
    name = "tesseract"

    def __init__(self, language: str = "eng", binary: Optional[str] = None) -> None:
        import pytesseract  # type: ignore

        self._pt = pytesseract
        if binary:
            pytesseract.pytesseract.tesseract_cmd = binary
        else:
            found = shutil.which("tesseract")
            if found:
                pytesseract.pytesseract.tesseract_cmd = found
        self.language = language

    def read(self, image_path: str, language: str = "") -> OcrResult:
        from PIL import Image

        t0 = time.time()
        img = Image.open(image_path)
        lang = (language or self.language or "eng").split("-")[0]
        if lang == "en":
            lang = "eng"
        try:
            data = self._pt.image_to_data(img, lang=lang,
                                          output_type=self._pt.Output.DICT)
        except Exception as exc:  # noqa: BLE001
            # language pack missing -> retry with default
            log.debug("tesseract lang %s failed (%s); retrying default", lang, exc)
            data = self._pt.image_to_data(img, output_type=self._pt.Output.DICT)
        words: List[OcrWord] = []
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            try:
                conf = float(data["conf"][i]) / 100.0
            except Exception:  # noqa: BLE001
                conf = 0.9
            if conf < 0.2:
                continue
            words.append(OcrWord(
                text=text,
                rect=Rect(int(data["left"][i]), int(data["top"][i]),
                          int(data["width"][i]), int(data["height"][i])),
                confidence=conf))
        # group words into lines by (block, par, line)
        grouped: Dict[tuple, List[str]] = {}
        order: List[tuple] = []
        for i in range(n):
            text = (data["text"][i] or "").strip()
            if not text:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(text)
        lines = [" ".join(grouped[k]) for k in order]
        return OcrResult(words=words, lines=lines, text="\n".join(lines),
                         engine=f"{self.name}:{lang}",
                         duration_ms=(time.time() - t0) * 1000,
                         image_size=(img.width, img.height))


class RapidOcr:
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        self._engine = RapidOCR()

    def read(self, image_path: str, language: str = "") -> OcrResult:
        t0 = time.time()
        result, _ = self._engine(image_path)
        words: List[OcrWord] = []
        lines: List[str] = []
        for item in result or []:
            box, text, conf = item[0], item[1], float(item[2])
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            rect = Rect(int(min(xs)), int(min(ys)),
                        int(max(xs) - min(xs)), int(max(ys) - min(ys)))
            words.append(OcrWord(text=str(text), rect=rect, confidence=conf))
            lines.append(str(text))
        return OcrResult(words=words, lines=lines, text="\n".join(lines),
                         engine=self.name, duration_ms=(time.time() - t0) * 1000,
                         image_size=(0, 0))


class NullOcr:
    name = "none"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    def read(self, image_path: str, language: str = "") -> OcrResult:
        log.debug("OCR unavailable: %s", self.reason)
        return OcrResult(engine="none", text="", lines=[], words=[],
                         duration_ms=0.0)


def create_ocr_backend(preferred: str = "auto", language: str = "en-AU"):
    """Return (backend, detail)."""
    order = {
        "auto": ["windows", "rapidocr", "tesseract"],
        "windows": ["windows"],
        "tesseract": ["tesseract"],
        "rapidocr": ["rapidocr"],
        "none": [],
    }.get(preferred, ["windows", "rapidocr", "tesseract"])
    errors: List[str] = []
    for choice in order:
        try:
            if choice == "windows":
                if os.name != "nt":
                    errors.append("windows OCR needs Windows 10+")
                    continue
                return WindowsMediaOcr(language), f"Windows.Media.Ocr ({language})"
            if choice == "tesseract":
                if not shutil.which("tesseract"):
                    errors.append("tesseract binary not found")
                    continue
                return TesseractOcr(language), "Tesseract"
            if choice == "rapidocr":
                return RapidOcr(), "RapidOCR (ONNX, offline)"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{choice}: {exc}")
            log.debug("OCR backend %s unavailable: %s", choice, exc)
    return NullOcr("; ".join(errors)), ("; ".join(errors) or "no OCR engine")
