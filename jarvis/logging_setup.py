"""Logging configuration.

Console + rotating file log inside the user data directory.  A redaction
filter is installed globally so credentials never reach disk (section 15.B).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from typing import Optional

from . import paths

_CONFIGURED = False

#: Patterns that are scrubbed from every log record.  Kept deliberately broad -
#: a log line is never worth leaking a secret over.
_REDACT_PATTERNS = [
    (re.compile(r"(?i)\b(password|passwd|pwd|passphrase|secret|api[_-]?key|token|"
                r"authorization|cookie|session[_-]?id|private[_-]?key|pin|otp|"
                r"cvv|card[_-]?number)\b(\s*[:=]\s*)(\S+)"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._\-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED-CARDLIKE]"),
    (re.compile(r"(?i)sk-[a-z0-9]{16,}"), "[REDACTED-KEY]"),
]


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        new = msg
        for pattern, repl in _REDACT_PATTERNS:
            new = pattern.sub(repl, new)
        if new != msg:
            record.msg = new
            record.args = ()
        return True


def redact(text: str) -> str:
    """Public helper used by the audit log and the memory store."""
    if not text:
        return text
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def setup(level: str = "INFO", to_file: bool = True, files_kept: int = 7) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(lvl)

    if _CONFIGURED:
        for h in root.handlers:
            h.setLevel(lvl)
        return

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
        datefmt="%H:%M:%S",
    )
    redaction = RedactionFilter()

    console = logging.StreamHandler(stream=sys.stderr)
    console.setFormatter(fmt)
    console.addFilter(redaction)
    console.setLevel(lvl)
    root.addHandler(console)

    if to_file:
        try:
            log_dir = paths.log_dir()
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "jarvis.log",
                maxBytes=4 * 1024 * 1024,
                backupCount=max(1, int(files_kept)),
                encoding="utf-8",
            )
            fh.setFormatter(fmt)
            fh.addFilter(redaction)
            fh.setLevel(lvl)
            root.addHandler(fh)
        except OSError as exc:  # pragma: no cover - read-only home
            root.warning("File logging disabled: %s", exc)

    # Third-party noise reduction.
    for noisy in ("urllib3", "PIL", "matplotlib", "faster_whisper", "ctranslate2",
                  "piper", "comtypes", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    root.debug("Logging configured (level=%s, file=%s)", level, to_file)


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


def dump_diagnostics(target: Optional[os.PathLike] = None) -> str:
    """Write a diagnostics bundle (no personal content) and return its path."""
    import platform

    lines = [
        "JARVIS diagnostics",
        f"version: {__import__('jarvis').__version__}",
        f"python: {sys.version}",
        f"platform: {platform.platform()}",
        f"frozen: {paths.is_frozen()}",
        f"data_dir: {paths.data_dir()}",
    ]
    try:
        import psutil

        vm = psutil.virtual_memory()
        lines.append(f"ram_total_gb: {vm.total / 1024**3:.1f}")
        lines.append(f"ram_available_gb: {vm.available / 1024**3:.1f}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"psutil unavailable: {exc}")
    text = "\n".join(lines) + "\n"
    path = target or (paths.log_dir() / "diagnostics.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    except OSError:
        pass
    return str(path)
