"""Recall — lightweight retrieval over learnings / rules / local issues.

Public surface: :func:`build_index` (index.py), :func:`search` /
:class:`SearchHit` (search.py), :func:`tokenize` (tokenizer.py).
"""

from orchestratord.recall.index import RecDoc, build_index
from orchestratord.recall.search import SearchHit, search
from orchestratord.recall.tokenizer import tokenize

__all__ = ["RecDoc", "SearchHit", "build_index", "search", "tokenize"]
