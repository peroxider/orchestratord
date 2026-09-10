"""Recall tokenizer — ASCII words + CJK bigrams (DESIGN_EXPERIENCE_LOOP §8.3).

No external tokenizer dependency: ASCII runs become lowercase words,
CJK runs become overlapping character bigrams (single chars pass
through when the run is length 1). Quality is carried by the ranking
fusion in ``search.py`` rather than segmentation precision. Swap this
module's ``tokenize`` to upgrade (e.g. jieba) without touching callers.
"""

from __future__ import annotations

import re

_CJK_START = "\u4e00"
_CJK_END = "\u9fff"
_RUN_RE = re.compile(rf"[a-z0-9_]+|[{_CJK_START}-{_CJK_END}]+")


def _is_cjk(run: str) -> bool:
    return all(_CJK_START <= ch <= _CJK_END for ch in run)


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for run in _RUN_RE.findall(text.lower()):
        if not _is_cjk(run) or len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


__all__ = ["tokenize"]
