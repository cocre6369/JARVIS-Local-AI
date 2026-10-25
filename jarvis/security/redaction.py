"""Scrubbing of sensitive information.

Used by
  * the audit log          - so approval history never stores a password,
  * the memory store       - so facts never store a secret,
  * the logging filter     - so the log file never stores a secret,
  * the vision pipeline    - so password fields are blacked out of images and
                             excluded from OCR / UI Automation output.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

# --- textual secrets -----------------------------------------------------
_KEY_WORDS = (
    "password", "passwd", "pwd", "passphrase", "secret", "api_key", "apikey",
    "api-key", "token", "access_token", "refresh_token", "authorization",
    "cookie", "session_id", "sessionid", "private_key", "privatekey", "pin",
    "otp", "cvv", "cvc", "card_number", "cardnumber", "bank_account",
    "account_number", "routing_number", "ssh-rsa", "ssh-ed25519",
    "begin rsa private key", "begin openssh private key", "master password",
    "recovery code", "seed phrase", "mnemonic",
)

_TEXT_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(
        r"(?i)\b(" + "|".join(re.escape(k) for k in _KEY_WORDS) + r")\b"
        r"(\s*[:=]\s*)([^\s,;\"'}{]\S*)"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._\-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(basic\s+)[a-z0-9+/=]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"), "[REDACTED-APIKEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED-TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED-TOKEN]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED-AWSKEY]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED-PRIVATE-KEY]"),
    # 13-19 digit runs = plausible card numbers.
    (re.compile(r"\b(?:\d[ \-]?){13,19}\b"), "[REDACTED-CARDLIKE]"),
    # Australian BSB / account pairs.
    (re.compile(r"(?i)\bbsb\b\s*[:#]?\s*\d{3}[\- ]?\d{3}"), "bsb [REDACTED]"),
]

#: Words that mark a value as a secret even without a `key = value` shape.
_CONTEXT_WORDS = (
    "my password is", "the password is", "password:", "pin is", "code is",
    "card number", "credit card", "debit card", "cvv", "expiry",
)


def contains_secret(text: str) -> bool:
    """Cheap heuristic: does this text look like it carries a credential?"""
    if not text:
        return False
    low = text.lower()
    if any(w in low for w in _CONTEXT_WORDS):
        return True
    for pattern, _ in _TEXT_RULES:
        if pattern.search(text):
            return True
    return False


def redact_text(text: Optional[str]) -> str:
    if not text:
        return text or ""
    out = text
    for pattern, repl in _TEXT_RULES:
        out = pattern.sub(repl, out)
    return out


def redact_dict(data, depth: int = 0):
    """Recursively redact strings inside dicts/lists (for the audit log)."""
    if depth > 6:
        return "…"
    if isinstance(data, str):
        return redact_text(data)
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            ks = str(k)
            if ks.lower() in _KEY_WORDS or any(w in ks.lower() for w in ("pass", "secret", "token", "key")):
                out[ks] = "[REDACTED]"
            else:
                out[ks] = redact_dict(v, depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact_dict(v, depth + 1) for v in data]
    if isinstance(data, (bytes, bytearray)):
        return f"<{len(data)} bytes>"
    return data


def mask_middle(value: str, keep: int = 2) -> str:
    """`hunter2secret` -> `hu**********` - used for UI display of targets."""
    if not value:
        return value
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "*" * max(4, len(value) - keep)


# --- image-space redaction ----------------------------------------------
#: UI Automation control types / class names that denote a secret field.
PASSWORD_UIA_TYPES = frozenset({"Edit"})  # combined with IsPassword=True


def is_password_element(
    control_type: str = "",
    class_name: str = "",
    automation_id: str = "",
    name: str = "",
    is_password: Optional[bool] = None,
) -> bool:
    """Decide whether a discovered UI element is a credential input.

    ``is_password`` comes from the UI Automation ``IsPassword`` property and is
    authoritative when present.  The name/class heuristics cover browsers and
    apps that do not set it (Chromium notoriously reports IsPassword=False).
    """
    if is_password:
        return True
    hay = " ".join([class_name, automation_id, name]).lower()
    markers = (
        "password", "passwd", "pwd", "current-password", "new-password",
        "loginpassword", "userpassword", "secret", "pin-input", "otp",
        "creditcard", "cardnumber", "cvv", "cvc", "ssn", "passphrase",
    )
    if any(m in hay for m in markers):
        return True
    return False


def password_field_hint_names() -> Iterable[str]:
    return ("password", "passwd", "pwd", "otp", "pin", "cvv", "card")


def redact_ocr_lines(lines: Iterable[str]) -> List[str]:
    return [redact_text(l) for l in lines]
