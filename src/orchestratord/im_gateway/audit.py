"""Audit redaction helpers.

PII / secret fields are masked before writing to ``audit.ndjson`` so the
audit log is safe to share. Sensitive keys: ``bot_token``,
``context_token``, ``Authorization``, ``webhook_url`` (token segment),
``from_user_id`` (hashed), ``user_id`` (hashed), ``bot_token_enc``.
"""

from __future__ import annotations

import hashlib
import urllib.parse
from typing import Any

_SENSITIVE_EXACT = frozenset(
    {"bot_token", "bot_token_enc", "context_token", "authorization", "secret", "password", "token"}
)
_HASH_KEYS = frozenset({"from_user_id", "user_id", "to_user_id"})
_REDACTED_URL_KEYS = frozenset({"webhook_url"})


def hash_user(user_id: str) -> str:
    if not user_id:
        return ""
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]


def _redact_webhook_url(url: str) -> str:
    if not isinstance(url, str) or not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    parts = parsed.path.rsplit("/", 1)
    redacted_path = parts[0] + "/***" if len(parts) == 2 else parsed.path
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, redacted_path, "", "", ""))


def redact(fields: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``fields`` with sensitive values masked/hashed."""
    out: dict[str, Any] = {}
    for k, v in fields.items():
        lk = k.lower()
        if lk in _SENSITIVE_EXACT:
            out[k] = "***" if v else v
        elif lk in _HASH_KEYS:
            out[k] = hash_user(str(v)) if v else v
        elif lk in _REDACTED_URL_KEYS:
            out[k] = _redact_webhook_url(str(v)) if v else v
        elif isinstance(v, dict):
            out[k] = redact(v)
        else:
            out[k] = v
    return out


__all__ = ["hash_user", "redact"]
