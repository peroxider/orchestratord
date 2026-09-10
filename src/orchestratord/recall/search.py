"""Recall search — hand-rolled BM25 + RRF fusion (DESIGN_EXPERIENCE_LOOP §8.4).

Signals fused by reciprocal-rank fusion (k=60):
  - BM25 over tokenized title+body (k1=1.5, b=0.75)
  - exact tag hits on the query tokens
  - same-repo boost (query ``repo`` hint matches doc.repo)
  - time decay ``exp(-delta_days / 30)`` on doc mtime

No external retrieval dependency; volumes are small (rebuild-per-call
index), so quality is carried by fusion rather than a learned model.
Rule-kind hits are annotated "约定" per the design's presentation rule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from orchestratord.recall.index import RecDoc
from orchestratord.recall.tokenizer import tokenize

#: RRF smoothing constant (Cormack et al. default).
RRF_K = 60

#: Weights per fused signal, grouped in one constant block for tuning.
WEIGHTS = {
    "bm25": 1.0,
    "tags": 0.8,
    "repo": 0.5,
    "recency": 0.3,
}

#: Presentation annotation for rule-kind documents.
CONVENTION_MARK = "约定"


@dataclass
class SearchHit:
    doc: RecDoc
    score: float
    bm25_rank: int | None = None
    tag_hits: list[str] = field(default_factory=list)
    repo_match: bool = False
    recency_weight: float = 0.0

    @property
    def is_convention(self) -> bool:
        return self.doc.kind == "rule"


def bm25_scores(
    docs: list[RecDoc], query_tokens: list[str], *, k1: float = 1.5, b: float = 0.75
) -> dict[str, float]:
    """Return doc_id -> BM25 score for the given query tokens."""
    doc_tokens: dict[str, list[str]] = {}
    for doc in docs:
        doc_tokens[doc.doc_id] = tokenize(f"{doc.title}\n{doc.body}")

    total = len(docs)
    if total == 0:
        return {}
    avg_len = sum(len(t) for t in doc_tokens.values()) / total

    df: dict[str, int] = {}
    for tokens in doc_tokens.values():
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    scores: dict[str, float] = {}
    for doc in docs:
        tokens = doc_tokens[doc.doc_id]
        length = len(tokens)
        if length == 0 or avg_len == 0:
            continue
        tf: dict[str, int] = {}
        for term in tokens:
            tf[term] = tf.get(term, 0) + 1
        score = 0.0
        for qt in query_tokens:
            freq = tf.get(qt, 0)
            if freq == 0:
                continue
            idf = math.log((total - df.get(qt, 0) + 0.5) / (df.get(qt, 0) + 0.5) + 1.0)
            score += idf * (freq * (k1 + 1)) / (freq + k1 * (1 - b + b * length / avg_len))
        if score > 0:
            scores[doc.doc_id] = score
    return scores


def _recency_weight(doc: RecDoc, now_ts: float) -> float:
    if doc.mtime <= 0:
        return 0.0
    delta_days = max(0.0, (now_ts - doc.mtime) / 86400.0)
    return math.exp(-delta_days / 30.0)


def _ranks(scores: dict[str, float]) -> dict[str, int]:
    """doc_id -> 1-based rank, best first; ties broken by doc_id for determinism."""
    ordered = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return {doc_id: i for i, (doc_id, _) in enumerate(ordered, start=1)}


def _rrf(rank: int) -> float:
    return 1.0 / (RRF_K + rank)


def search(
    index: list[RecDoc],
    query: str,
    *,
    top_k: int = 10,
    repo: str | None = None,
    kinds: list[str] | None = None,
) -> list[SearchHit]:
    """Fuse BM25 + tag + repo + recency signals into ranked hits."""
    query_tokens = tokenize(query)
    if not query_tokens or not index:
        return []

    if kinds:
        allowed = set(kinds)
        index = [d for d in index if d.kind in allowed]

    now = datetime.now(UTC).timestamp()

    bm25 = bm25_scores(index, query_tokens)
    bm25_rank = _ranks(bm25)

    # Tag signal: exact overlap between query tokens and doc tags.
    tag_signal: dict[str, list[str]] = {}
    for doc in index:
        tag_terms: list[str] = []
        for tag in doc.tags:
            tag_tokens = tokenize(str(tag).lower())
            if any(t in query_tokens for t in tag_tokens):
                tag_terms.append(str(tag))
        if tag_terms:
            tag_signal[doc.doc_id] = tag_terms
    tag_rank = _ranks({d: float(len(t)) for d, t in tag_signal.items()})

    # Recency signal ranked among docs with a real mtime.
    recency: dict[str, float] = {
        d.doc_id: _recency_weight(d, now) for d in index if d.mtime > 0
    }
    recency_rank = _ranks(recency)

    # Repo signal: docs whose repo matches the query hint.
    repo_signal: dict[str, float] = {}
    if repo:
        repo_signal = {d.doc_id: 1.0 for d in index if d.repo == repo}
    repo_rank = _ranks(repo_signal)

    fused: dict[str, float] = {}
    for doc in index:
        score = 0.0
        if doc.doc_id in bm25_rank:
            score += WEIGHTS["bm25"] * _rrf(bm25_rank[doc.doc_id])
        if doc.doc_id in tag_rank:
            score += WEIGHTS["tags"] * _rrf(tag_rank[doc.doc_id])
        if doc.doc_id in repo_rank:
            score += WEIGHTS["repo"] * _rrf(repo_rank[doc.doc_id])
        if doc.doc_id in recency_rank:
            score += WEIGHTS["recency"] * _rrf(recency_rank[doc.doc_id])
        if score > 0.0:
            fused[doc.doc_id] = score

    ranked = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
    by_id = {d.doc_id: d for d in index}

    hits: list[SearchHit] = []
    for doc_id, score in ranked[: max(0, top_k)]:
        doc = by_id[doc_id]
        hits.append(
            SearchHit(
                doc=doc,
                score=score,
                bm25_rank=bm25_rank.get(doc_id),
                tag_hits=tag_signal.get(doc_id, []),
                repo_match=bool(repo) and doc.repo == repo,
                recency_weight=recency.get(doc_id, 0.0),
            )
        )
    return hits


__all__ = ["CONVENTION_MARK", "RRF_K", "WEIGHTS", "SearchHit", "bm25_scores", "search"]
