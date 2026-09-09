"""Review-followup prompt must carry explicit commit semantics.

Defect #35: the review feedback template constrained only the *scope* of
the follow-up run ("work on the current branch", "smallest change") and
said nothing about commit semantics. An agent facing an existing PR +
"do not create a new PR" constraint naturally reached for
``git commit --amend`` — rewriting an already-pushed commit, creating a
non-fast-forward fork, and silently stranding the fix (PR never updated).

The template must therefore instruct the agent to (1) append new commits
and (2) forbid rewriting already-pushed history (amend/rebase/anything
similar), with the one-line reason that the branch is already pushed and
linked to the PR.
"""

from __future__ import annotations

from types import SimpleNamespace

from orchestratord.applications.issue_pr.prompts import render_review_feedback
from orchestratord.tracker import PullRequestFeedback, PullRequestRef


def _issue() -> SimpleNamespace:
    return SimpleNamespace(
        identifier="ISSUE-35",
        title="Fix review followup commit semantics",
        to_dict=lambda: {
            "identifier": "ISSUE-35",
            "title": "Fix review followup commit semantics",
        },
    )


def _pr() -> PullRequestRef:
    return PullRequestRef(number="35", url="https://example.test/pr/35", title="Fix cookie TTL")


def _feedback() -> list[PullRequestFeedback]:
    return [
        PullRequestFeedback(
            id="FB-1",
            source="inline_review",
            body="Add a regression test for the TTL.",
            file_path="src/auth/session.py",
            line=42,
            severity="warning",
            status="open",
        )
    ]


def test_review_feedback_prompt_forbids_rewriting_pushed_history() -> None:
    prompt = render_review_feedback(
        issue=_issue(),
        pull_request=_pr(),
        branch_name="fix/cookie-ttl",
        feedback=_feedback(),
    )

    # 追加新 commit：不得改写既有历史，而是以新 commit 追加。
    assert "new commits appended" in prompt
    assert "existing branch" in prompt

    # 禁止 amend/rebase 已推送历史。
    assert "git commit --amend" in prompt
    assert "git rebase" in prompt
    assert "already-pushed history" in prompt

    # 理由：分支已推送并关联 PR，改写历史会产生非快进分叉。
    assert "already pushed and linked to the PR" in prompt
    assert "non-fast-forward" in prompt


def test_review_feedback_prompt_keeps_existing_scope_constraints() -> None:
    prompt = render_review_feedback(
        issue=_issue(),
        pull_request=_pr(),
        branch_name="fix/cookie-ttl",
        feedback=_feedback(),
    )

    # 既有模板约束保持不回归。
    assert "Work on the current branch only" in prompt
    assert "do not create a new branch or pull request" in prompt
    assert "Prefer the smallest correct change" in prompt
    assert "Fix only the PR review feedback and CI failures" in prompt
