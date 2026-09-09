"""Capture/verify prompt-render golden snapshots (DESIGN P2 acceptance).

Run as a script against PRE-refactor code to write the goldens:

    uv run python scripts/capture_prompt_goldens.py --write

After the P2 refactor, tests/test_prompt_snapshot.py rebuilds the same
matrix (wiring moved entry points to their new homes) and compares
byte-identical against tests/goldens/prompt_snapshot.json.

All inputs are deterministic: no network, no real git repos (tmp dirs
are intentionally non-git so diff/status decoration is skipped), fixed
strings for interpreter paths, and SESSIONS_DIR monkey-patched to a
relative path so machine-absolute paths never leak into goldens.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = REPO_ROOT / "tests" / "goldens" / "prompt_snapshot.json"


# ---------------------------------------------------------------- fixtures


@dataclass
class _IssueLike:
    """Legacy Issue-shaped object (to_dict) for compatibility paths."""

    id: str = "ISSUE-7"
    identifier: str = "ISSUE-7"
    title: str = "Fix login cookie expiry"
    description: str = "Session cookie expires too early."
    labels: list = field(default_factory=lambda: ["bug", "auth"])
    priority: int = 2
    state: str = "opened"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "identifier": self.identifier,
            "title": self.title,
            "description": self.description,
            "labels": list(self.labels),
            "priority": self.priority,
            "state": self.state,
        }


@dataclass
class _Ws:
    path: str


@dataclass
class _Session:
    workspace_strategy: str | None = None
    workspace: Any = None
    run_kind: str | None = None
    integration_branch: str = "integration"
    start_commit_sha: str = "abc1234"
    base_commit_sha: str = "def5678"
    previous_issue_id: str = "ISSUE-6"
    sequence_index: int = 3


def _sample_feedback() -> list:
    from orchestratord.tracker import PullRequestFeedback

    return [
        PullRequestFeedback(
            id="FB-1",
            source="inline_review",
            body="Please add a regression test for the cookie TTL.",
            file_path="src/auth/session.py",
            line=42,
            severity="warning",
            status="open",
            commit_sha="abc1234",
            url="https://example.test/pr/7#discussion-1",
            diff_hunk="@@ -40,7 +40,7 @@\n-    ttl = 60\n+    ttl = 3600",
        ),
        PullRequestFeedback(
            id="FB-2",
            source="ci",
            body="Lint failure: unused import in worker.py",
            file_path=None,
            line=None,
            severity="error",
            status="open",
            commit_sha=None,
            url=None,
            diff_hunk=None,
        ),
    ]


def _pull_request_ref():
    from orchestratord.tracker import PullRequestRef

    return PullRequestRef(number="7", url="https://example.test/pr/7", title="Fix cookie TTL")


def build_cases() -> dict[str, Callable[[], str]]:
    """name -> zero-arg renderer. Deterministic; no git/network access."""
    import orchestratord.prompt_builder as pb

    issue = _IssueLike()
    task_issue = {
        "id": "ISSUE-7",
        "kind": "issue",
        "title": "Fix login cookie expiry",
        "description": "Session cookie expires too early. See src/auth/session.py and docs/auth.md.",
        "labels": ["bug"],
        "priority": 2,
        "attempt": 2,
        "context": {
            "issue_id": "7",
            "issue_identifier": "ISSUE-7",
            "issue_state": "opened",
        },
    }

    cases: dict[str, Callable[[], str]] = {}
    tmp = Path(tempfile.mkdtemp(prefix="prompt_golden_"))
    ws_dir = tmp / "ws"
    ws_dir.mkdir()

    def render_task(task: Any, **kwargs: Any) -> str:
        return pb.PromptBuilder.render(task, **kwargs)

    # -- core template matrix ------------------------------------------------
    cases["issue_task_basic"] = lambda: render_task(task_issue)

    def _agent_task(kind: str, **ctx: Any):
        from orchestratord.agent.task import AgentTask

        return AgentTask(
            id="T-1",
            kind=kind,
            title="Stage: implement parser",
            description="Parse the token stream into an AST.",
            context=dict(ctx),
            priority=1,
            attempt=1,
        )

    cases["stage_task"] = lambda: render_task(
        _agent_task("workflow_stage", stage_id="s1", phase="implement", parent_issue="ISSUE-7")
    )
    cases["generic_task"] = lambda: render_task(_agent_task("generic"))
    cases["legacy_issue_object"] = lambda: render_task(issue)
    cases["legacy_issue_object_with_attempt"] = lambda: render_task(issue, attempt=3)

    # -- decorations ---------------------------------------------------------
    cases["clarification_block_args"] = lambda: render_task(
        task_issue,
        clarification_context="PRE-RENDERED CLARIFICATION BLOCK",
        pending_question="Should the TTL be configurable?",
        options=["yes", "no"],
    )
    cases["python_executable"] = lambda: render_task(
        task_issue, python_executable="/opt/venv/bin/python3"
    )
    cases["conflict_files"] = lambda: render_task(
        task_issue, conflict_files=("src/auth/session.py", "src/auth/keys.py")
    )
    cases["previous_run_ids"] = lambda: render_task(
        task_issue, previous_run_ids=["run-AAA", "run-BBB"]
    )
    cases["verification_error"] = lambda: render_task(
        task_issue, previous_verification_error="FAILED tests/test_auth.py::test_ttl"
    )

    def _sequential() -> str:
        session = _Session(
            workspace_strategy="sequential",
            workspace=_Ws(path=str(ws_dir)),
        )
        return render_task(task_issue, session=session)

    cases["sequential_workspace"] = _sequential

    def _operator_hints() -> str:
        (ws_dir / ".operator_hints.md").write_text(
            "Prefer pytest fixtures over manual setup.\n", encoding="utf-8"
        )
        try:
            session = _Session(workspace=_Ws(path=str(ws_dir)))
            return render_task(task_issue, session=session)
        finally:
            (ws_dir / ".operator_hints.md").unlink(missing_ok=True)

    cases["operator_hints"] = _operator_hints

    def _premise_missing() -> str:
        (ws_dir / "existing.py").write_text("# ok\n", encoding="utf-8")
        session = _Session(workspace=_Ws(path=str(ws_dir)))
        # "add/create" wording would mark the line a creation request and
        # skip the warning — phrase it as a factual claim instead.
        t = dict(task_issue)
        t["description"] = "Function gcd in gcd.py crashes on zero; also see existing.py."
        t["context"] = dict(task_issue["context"])
        return render_task(t, session=session)

    cases["premise_missing_files"] = _premise_missing

    # -- combined (several decorations at once) ------------------------------
    cases["kitchen_sink"] = lambda: render_task(
        task_issue,
        attempt=4,
        python_executable="/opt/venv/bin/python3",
        previous_verification_error="FAILED tests/test_auth.py::test_ttl",
        conflict_files=("src/auth/keys.py",),
    )

    # -- split (render_parts) -------------------------------------------------
    def _parts() -> str:
        system, user = pb.PromptBuilder.render_parts(task_issue, attempt=1)
        return f"SYSTEM<<<{system}>>>USER<<<{user}>>>"

    cases["render_parts_no_marker"] = _parts

    # -- business renderers (applications.issue_pr.prompts since P4) ---------
    import orchestratord.applications.issue_pr.prompts as bp

    cases["review_feedback"] = lambda: bp.render_review_feedback(
        issue=issue,
        pull_request=_pull_request_ref(),
        branch_name="fix/cookie-ttl",
        feedback=_sample_feedback(),
    )
    cases["rebase_prompt"] = lambda: bp.render_rebase(
        issue=issue,
        branch_name="fix/cookie-ttl",
        base_branch="master",
        conflict_files=("src/auth/session.py", "src/auth/keys.py"),
        reason="upstream force-push",
    )
    cases["rebase_prompt_no_conflicts"] = lambda: bp.render_rebase(
        issue=issue,
        branch_name="fix/cookie-ttl",
        base_branch="master",
    )
    cases["feedback_summary"] = lambda: bp.render_feedback_summary(
        attempt=2,
        processed=_sample_feedback()[:1],
        skipped=[{"feedback": _sample_feedback()[1], "reason": "unclear request"}],
    )
    cases["clarification_context_pending"] = (
        lambda: bp.build_clarification_context(
            pending_question="Which TTL value?",
            options=["3600", "86400"],
        )
    )
    cases["clarification_context_answer"] = (
        lambda: bp.build_clarification_context(
            clarification_answer="3600 seconds",
            answer_source="issue author",
        )
    )
    cases["clarification_context_none"] = (
        lambda: bp.build_clarification_context()
    )

    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write goldens instead of comparing")
    args = parser.parse_args()

    # Keep machine paths out of goldens.
    import orchestratord.prompt_builder as pb

    pb.SESSIONS_DIR = Path("sessions-home")

    outputs = {name: fn() for name, fn in build_cases().items()}

    if args.write:
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(json.dumps(outputs, indent=2, sort_keys=True), encoding="utf-8")
        print(f"wrote {len(outputs)} goldens to {GOLDEN_PATH}")
        return 0

    if not GOLDEN_PATH.exists():
        print(f"goldens missing: {GOLDEN_PATH}", file=sys.stderr)
        return 2
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    failures = []
    for name in sorted(set(expected) | set(outputs)):
        if name not in outputs:
            failures.append(f"{name}: missing from live render")
        elif name not in expected:
            failures.append(f"{name}: missing from goldens")
        elif expected[name] != outputs[name]:
            failures.append(f"{name}: OUTPUT DIFFERS")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"all {len(outputs)} prompt goldens byte-identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
