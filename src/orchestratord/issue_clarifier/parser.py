"""Strict-but-fail-open response parsing for issue clarity analysis."""

from __future__ import annotations

import json
import re
from typing import Any

from .models import ClarifyQuestion, ClarifyResult


def parse_clarify_response(
    raw: str,
    *,
    min_confidence: float = 0.7,
    max_questions: int = 3,
) -> ClarifyResult:
    data = _loads_json(raw)
    if not isinstance(data, dict):
        return _degraded_clear("provider returned non-JSON output")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    if confidence < min_confidence:
        return ClarifyResult(
            is_clear=True,
            confidence=confidence,
            reason="clarifier confidence below blocking threshold",
            degraded=True,
        )

    rows = data.get("ambiguities")
    if not isinstance(rows, list):
        rows = []
    questions: list[ClarifyQuestion] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        question = ClarifyQuestion.from_dict(row)
        if question.question:
            questions.append(question)
        if len(questions) >= max(1, int(max_questions)):
            break

    raw_is_clear = data.get("is_clear")
    if not isinstance(raw_is_clear, bool):
        return _degraded_clear("provider returned non-boolean is_clear", confidence)
    is_clear = raw_is_clear
    if is_clear:
        questions = []
    elif not questions:
        return _degraded_clear("unclear response contained no actionable questions", confidence)

    return ClarifyResult(
        is_clear=is_clear,
        ambiguities=tuple(questions),
        confidence=confidence,
        reason="provider analysis",
    )


def _degraded_clear(reason: str, confidence: float = 0.0) -> ClarifyResult:
    return ClarifyResult(
        is_clear=True,
        confidence=confidence,
        reason=reason,
        degraded=True,
    )


def _loads_json(raw: str) -> Any:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match is None:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


__all__ = ["parse_clarify_response"]
