"""Privacy scrubbing for experience / learning artifacts.

``scrub`` masks secrets, credentials, internal hosts and personal
identifiers in free-form text *before* it is written to disk. The
experience writer treats any exception escaping ``scrub`` as
fail-closed: the artifact is dropped rather than persisted unscrubbed
(DESIGN_EXPERIENCE_LOOP.md §8.2).
"""

from __future__ import annotations

import re

# Rule set — each entry is (compiled regex, replacement). Replacement is
# a template string or a callable taking the match. Order matters:
# more specific patterns (credential shapes) run before generic ones.
# Keep rules declarative here; allowlist exemption is applied uniformly.
_RULES: tuple[tuple[re.Pattern[str], object], ...] = (
    # Credential-shaped tokens (API keys, PATs, bearer/jwt-ish strings)
    (
        re.compile(
            r"(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
            r"gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"xox[baprs]-[A-Za-z0-9-]{10,}|"
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})"
        ),
        "***REDACTED_TOKEN***",
    ),
    # key=value style secrets (api_key=..., token: ..., password= ...)
    (
        re.compile(
            r"(?i)\b((?:api[_-]?key|secret|token|passwd|password|passphrase)"
            r"\s*[=:]\s*)(['\"]?[^\s\"',;)]{8,})"
        ),
        r"\1***REDACTED***",
    ),
    # Internal / private network URLs (RFC1918 hosts, .internal/.local,
    # localhost with port)
    (
        re.compile(
            r"(?i)\bhttps?://(?:"
            r"(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
            r"192\.168\.\d{1,3}\.\d{1,3}|"
            r"172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|"
            r"localhost(?::\d+)?|"
            r"[A-Za-z0-9.-]+\.(?:internal|local|corp|lan)"
            r")(?::\d+)?(?:/[^\s\"'<>]*)?"
        ),
        "***REDACTED_INTERNAL_URL***",
    ),
    # Email addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "***REDACTED_EMAIL***",
    ),
    # Absolute paths that leak usernames (POSIX and Windows)
    (
        re.compile(r"(?<![A-Za-z0-9])/home/[A-Za-z0-9_.-]+/"),
        "/home/***USER***/",
    ),
    (
        re.compile(r"(?i)\b[A-Z]:\\Users\\[A-Za-z0-9_.-]+\\"),
        lambda _m: "C:\\Users\\***USER***\\",
    ),
)


def scrub(text: str, *, allowlist: list[str] | None = None) -> str:
    """Return ``text`` with secrets/PII/internal hosts masked.

    ``allowlist`` entries are literal substrings that must survive
    scrubbing (e.g. an approved internal hostname configured in the
    workflow ``experience`` section). Internal errors propagate to the
    caller — writers must fail closed, never persist unscrubbed text.
    """
    allowed = [item for item in (allowlist or []) if item]
    result = text

    def _sub(match: re.Match[str], replacement: object) -> str:
        matched = match.group(0)
        if any(item in matched for item in allowed):
            return matched
        if callable(replacement):
            return str(replacement(match))
        return match.expand(replacement)

    result = text
    for pattern, replacement in _RULES:
        if not allowed:
            result = pattern.sub(replacement, result)
        else:
            result = pattern.sub(lambda m, r=replacement: _sub(m, r), result)
    return result


__all__ = ["scrub"]
