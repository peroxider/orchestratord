"""Prompt construction for issue clarity analysis."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from ..issue import Issue


_SYSTEM_PROMPT = """You review issue text before an automated coding agent starts.
Decide whether the issue is clear enough to implement without guessing.

Only report ambiguities that materially block implementation or verification:
- missing: required scope, inputs, expected behavior, or acceptance criteria are absent
- vague: multiple materially different interpretations are possible
- contradictory: requirements conflict
- unexecutable: success cannot be measured or required environment/data is unspecified

Do not invent questions for details an engineer can discover from the repository.
Return JSON only, with this schema:
{
  "is_clear": true,
  "confidence": 0.0,
  "ambiguities": [
    {
      "question": "a complete question that can be posted to the issue author",
      "ambiguity_type": "missing|vague|contradictory|unexecutable",
      "evidence": "short quote or summary from the issue",
      "suggested_options": ["option A", "option B"]
    }
  ]
}
Use at most the requested number of questions. If is_clear is true, ambiguities must be empty."""


def build_clarify_messages(
    issue: "Issue",
    *,
    prior_replies: Iterable[str] = (),
    max_questions: int = 3,
    max_input_tokens: int = 6000,
    workspace_focuses: list[dict] | None = None,  # ★ P2: follow-up workspace focus 富化
) -> list[dict[str, str]]:
    payload = {
        "title": str(getattr(issue, "title", "") or ""),
        "description": str(getattr(issue, "description", "") or ""),
        "labels": list(getattr(issue, "labels", None) or []),
        "author_replies": [str(reply) for reply in prior_replies if str(reply).strip()],
        "max_questions": max(1, int(max_questions)),
    }
    if workspace_focuses:                                   # ★ P2
        payload["workspace_focuses"] = workspace_focuses    # ★ P2
    # Four characters per token is deliberately conservative for mixed
    # English/CJK issue text and avoids importing a tokenizer in the daemon.
    max_chars = max(1000, int(max_input_tokens) * 4)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(raw) > max_chars:
        payload["_truncated"] = True
        raw = _shrink_payload_to_limit(payload, max_chars)
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": raw},
    ]


def _shrink_payload_to_limit(payload: dict, max_chars: int) -> str:
    """Return valid JSON bounded by ``max_chars`` across all user fields."""
    for _ in range(64):
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw) <= max_chars:
            return raw
        overflow = len(raw) - max_chars
        candidates: list[tuple[int, Any, Any]] = []
        for key in ("title", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                candidates.append((len(value), payload, key))
        for key in ("labels", "author_replies"):
            values = payload.get(key)
            if isinstance(values, list):
                for index, value in enumerate(values):
                    if isinstance(value, str) and value:
                        candidates.append((len(value), values, index))
        if not candidates:
            break
        length, container, key = max(candidates, key=lambda row: row[0])
        trim = min(length, max(1, overflow + 16))
        container[key] = container[key][: length - trim]

    # The fixed schema is comfortably below the enforced minimum of 1000
    # characters, so this remains valid JSON even for adversarial escaping.
    return json.dumps(
        {
            "title": "",
            "description": "",
            "labels": [],
            "author_replies": [],
            "max_questions": payload.get("max_questions", 1),
            "_truncated": True,
        },
        separators=(",", ":"),
    )


__all__ = ["build_clarify_messages"]
