"""Secret redaction helpers.

Single rule for the whole codebase: secrets (private keys, API keys, tokens,
signatures, passphrases) must NEVER reach logs, webhooks, or the DB. Anything
that might carry a secret is passed through ``redact`` first.
"""

from __future__ import annotations

import re
from typing import Any

# Substrings (case-insensitive) that mark a dict key as sensitive.
_SENSITIVE_KEY_HINTS = (
    "private_key",
    "privatekey",
    "secret",
    "passphrase",
    "password",
    "token",
    "api_key",
    "apikey",
    "signature",
    "authorization",
    "bearer",
    "mnemonic",
    "seed",
)

# Value patterns that look like secrets even without a telling key name.
_VALUE_PATTERNS = (
    re.compile(r"0x[a-fA-F0-9]{40,}"),          # hex private keys / signatures
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),     # long opaque tokens
)

_MASK = "***REDACTED***"


def _is_sensitive_key(key: str) -> bool:
    k = key.lower()
    return any(hint in k for hint in _SENSITIVE_KEY_HINTS)


def mask_value(value: Any) -> str:
    """Mask a single value while keeping a tiny, non-reversible hint of length."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= 6:
        return _MASK
    return f"{_MASK}(len={len(s)})"


def redact(obj: Any) -> Any:
    """Recursively redact secrets from dicts/lists/strings for safe output.

    - Dict values under sensitive keys are masked.
    - Free-form strings have secret-looking substrings masked.
    """
    if isinstance(obj, dict):
        out: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and _is_sensitive_key(k):
                out[k] = mask_value(v)
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _redact_string(obj)
    return obj


def _redact_string(s: str) -> str:
    redacted = s
    for pat in _VALUE_PATTERNS:
        redacted = pat.sub(_MASK, redacted)
    return redacted
