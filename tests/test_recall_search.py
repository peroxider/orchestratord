"""Tests for the recall subsystem (tokenizer / index / search).

Covers DESIGN_EXPERIENCE_LOOP §8.3-8.4 acceptance points: CJK bigram
matching, ASCII/CJK mixed tokenization, RRF fusion of BM25 + tags +
repo + recency, and rule-kind convention annotation.
"""

from __future__ import annotations

import time

import pytest

from orchestratord.recall import RecDoc, build_index, search, tokenize
from orchestratord.recall.search import bm25_scores


# ---------------------------------------------------------------- tokenize


def test_tokenize_ascii_words():
    assert tokenize("Retry Backoff policy") == ["retry", "backoff", "policy"]


def test_tokenize_cjk_bigrams():
    assert tokenize("重试退避") == ["重试", "试退", "退避"]


def test_tokenize_mixed_ascii_and_cjk():
    tokens = tokenize("git push 被拒绝")
    assert "git" in tokens
    assert "push" in tokens
    assert "被拒" in tokens
    assert "拒绝" in tokens


def test_tokenize_single_cjk_char_passthrough():
    assert tokenize("谢") == ["谢"]


# ------------------------------------------------------------------- bm25


def _doc(doc_id: str, title: str, body: str = "", repo: str = "", **kw) -> RecDoc:
    return RecDoc(doc_id=doc_id, kind="learning", repo=repo, title=title, body=body, **kw)


def test_bm25_ranks_matching_doc_higher():
    docs = [
        _doc("a", "git push rejected non-fast-forward"),
        _doc("b", "unrelated note about cooking"),
    ]
    scores = bm25_scores(docs, tokenize("git push"))
    assert scores["a"] > 0
    assert "b" not in scores


def test_bm25_empty_corpus():
    assert bm25_scores([], tokenize("anything")) == {}


# ------------------------------------------------------------------ search


def test_search_cjk_bigram_finds_partial_match():
    docs = [
        _doc("hit", "重试退避策略", repo="alpha"),
        _doc("miss", "代码风格约定"),
    ]
    hits = search(docs, "重试")
    assert hits
    assert hits[0].doc.doc_id == "hit"


def test_search_empty_query_returns_empty():
    assert search([_doc("a", "x")], "") == []
    assert search([], "anything") == []


def test_search_top_k_limits_results():
    docs = [_doc(f"d{i}", f"git push issue {i}") for i in range(5)]
    assert len(search(docs, "git push", top_k=2)) == 2


def test_search_tag_hit_boosts():
    plain = _doc("plain", "deployment runbook")
    tagged = _doc("tagged", "deployment runbook", tags=["deploy", "rollback"])
    hits = search([plain, tagged], "deploy")
    assert hits[0].doc.doc_id == "tagged"
    assert "deploy" in hits[0].tag_hits


def test_search_repo_hint_boosts_same_repo():
    same = _doc("same", "retry policy", repo="alpha")
    other = _doc("other", "retry policy", repo="beta")
    hits = search([same, other], "retry", repo="alpha")
    assert hits[0].doc.doc_id == "same"
    assert hits[0].repo_match is True
    assert hits[1].repo_match is False


def test_search_time_decay_prefers_recent():
    now = time.time()
    old = _doc("old", "retry backoff", mtime=now - 90 * 86400)
    new = _doc("new", "retry backoff", mtime=now - 86400)
    hits = search([old, new], "retry")
    assert hits[0].doc.doc_id == "new"
    assert hits[0].recency_weight > hits[1].recency_weight


def test_search_rule_kind_annotated_as_convention():
    rule = RecDoc(
        doc_id="rule:1",
        kind="rule",
        repo="",
        tags=["testing"],
        title="always run pytest before push",
        body="always run pytest before push",
    )
    hits = search([rule], "pytest")
    assert hits and hits[0].is_convention


def test_search_kind_filter():
    docs = [
        _doc("l", "retry learning"),
        RecDoc(doc_id="r", kind="rule", repo="", tags=[], title="retry rule", body="retry rule"),
    ]
    hits = search(docs, "retry", kinds=["rule"])
    assert [h.doc.doc_id for h in hits] == ["r"]


# ------------------------------------------------------------- build_index


@pytest.fixture(autouse=True)
def _no_rule_docs(monkeypatch):
    """Isolate build_index from the process-global workflow store singleton
    (earlier test files may leave a real rules path on it)."""
    monkeypatch.setattr("orchestratord.recall.index._rule_docs", lambda: [])


def test_build_index_scans_workspace_and_global_learnings(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws_learn = ws / ".orchestratord" / "learnings"
    ws_learn.mkdir(parents=True)
    (ws_learn / "2026-01-01-ws-note.md").write_text(
        "---\nrepo: alpha\ntags: [\"git\", \"push\"]\n---\n\n# WS Note\npush rejected\n",
        encoding="utf-8",
    )

    home = tmp_path / "home"
    global_learn = home / "learnings"
    global_learn.mkdir(parents=True)
    (global_learn / "2026-01-02-global-note.md").write_text(
        "---\ntags: []\n---\n\n# Global Note\n重试退避\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(home))

    docs = build_index(workspace_root=ws)
    ids = {d.doc_id for d in docs}
    assert "ws-learning:" + str(ws_learn / "2026-01-01-ws-note.md") in ids
    assert "global-learning:" + str(global_learn / "2026-01-02-global-note.md") in ids

    ws_doc = next(d for d in docs if d.title == "WS Note")
    assert ws_doc.repo == "alpha"
    assert ws_doc.tags == ["git", "push"]


def test_build_index_dedupes_same_path_scanned_twice(tmp_path, monkeypatch):
    # When ORCHESTRATORD_HOME is the workspace's .orchestratord dir, the
    # global and workspace scans hit the same files; the seen-set must
    # dedupe by path.
    learn = tmp_path / ".orchestratord" / "learnings"
    learn.mkdir(parents=True)
    (learn / "note.md").write_text("# Dup\ntext\n", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(tmp_path / ".orchestratord"))

    docs = build_index(workspace_root=tmp_path)
    assert len([d for d in docs if d.title == "Dup"]) == 1


def test_build_index_includes_local_issue_docs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "learnings").mkdir(parents=True)
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(home))

    issues = tmp_path / "issues"
    issues.mkdir()
    (issues / "PROJ-1.md").write_text("# PROJ-1\nfix the bug\n", encoding="utf-8")

    docs = build_index(issues_dir=issues)
    issue_docs = [d for d in docs if d.kind == "issue"]
    assert [d.doc_id for d in issue_docs] == ["issue:PROJ-1"]
    assert issue_docs[0].repo == "PROJ-1"


def test_build_index_missing_dirs_are_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(tmp_path / "missing-home"))
    assert build_index(workspace_root=tmp_path / "missing-ws") == []


@pytest.mark.parametrize("malformed", ["---\nno close", "not frontmatter at all"])
def test_frontmatter_parse_failures_still_index_body(tmp_path, monkeypatch, malformed):
    home = tmp_path / "home"
    learn = home / "learnings"
    learn.mkdir(parents=True)
    (learn / "broken.md").write_text(malformed + "\n\n# Broken\nbody here\n", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATORD_HOME", str(home))

    docs = build_index()
    assert len(docs) == 1
    assert "body here" in docs[0].body
