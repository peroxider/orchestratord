"""Mention parsing (§7.4).

Scans comment text for ``@agent-name`` / ``@member-name`` tokens so a comment
can dispatch inbox items + mention events and trigger a new session for a
mentioned agent. The parser is deliberately a pure function: it extracts
handles without resolving them to agents/members (name resolution lives in the
notification/chat layer, Phase 5).

Reference: docs/FEATURE_GAP_VS_MULTICA.md §7.4.
"""

from __future__ import annotations

import re

_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_-]+)")


def parse_mentions(text: str) -> list[str]:
    """Return the unique handles mentioned in ``text``, in first-seen order.

    A handle is ``@`` followed by letters / digits / ``_`` / ``-``. The ``@``
    must not be preceded by a word character or another ``@`` so that email
    addresses (``a@b.com``) and doubled ``@@`` are not misread as mentions.
    Sentence punctuation (``,`` / ``.`` / ``!`` / ``?``) terminates a handle.
    """
    seen: set[str] = set()
    mentions: list[str] = []
    for match in _MENTION_RE.finditer(text):
        handle = match.group(1)
        if handle not in seen:
            seen.add(handle)
            mentions.append(handle)
    return mentions
