"""Opt-in real backend check for run input/output recording (provider usage).

Run directly with the repository venv. Writes only a disposable local workspace,
normal session transcripts, and a dashboard registry. No daemon or remote Git.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path

from orchestratord.backend_registry import resolve_backend
from orchestratord.backend_runner import BackendRunner
from orchestratord.config.schema import AgentConfig, SandboxConfig
from orchestratord.event_tailer import read_history_direct
from orchestratord.session_state import RunSession, RunSubject
from orchestratord.workspace import Workspace


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="orchestratord-truth-e2e."))
    workspaces = root / "workspaces"
    workspace = workspaces / "VIEW-TRUTH"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text(
        "Isolated read-only Conversation verification.\n"
    )
    git = await asyncio.create_subprocess_exec("git", "init", "--quiet", str(workspace))
    assert await git.wait() == 0
    prompt = (
        "This is a read-only UI verification, not a coding task. No task-tracking "
        "tools are needed. Use exactly one shell tool call to read README.md and "
        "print LIVEVIEW_TRUTH_TOOL_RESULT. Do not edit files, inspect parent folders, "
        "use the network, create tasks, or run git commands. Then reply exactly "
        "LIVEVIEW_TRUTH_REPLY."
    )
    session = RunSession(
        issue=RunSubject(
            id="view-truth", identifier="VIEW-TRUTH", title="Real input recording check"
        ),
        workspace=Workspace(path=workspace, issue_identifier="VIEW-TRUTH"),
        prompt_override=prompt,
    )
    config = AgentConfig(
        model="gpt-5.4", max_turns=5, run_timeout_ms=180000, stall_timeout_ms=120000
    )
    config.env = {"ORCHESTRATORD_CODEX_REASONING_EFFORT": "low"}
    runner = BackendRunner(
        resolve_backend("codex-cli"),
        config,
        SandboxConfig(approval_policy="never"),
    )
    started = time.time()

    def snapshot(current):
        (workspaces / ".orchestratord_issue_registry.json").write_text(
            json.dumps(
                {
                    "view-truth": {
                        "issue_identifier": "VIEW-TRUTH",
                        "issue_title": current.issue.title,
                        "status": current.status,
                        "workspace_path": str(workspace),
                        "run_id": current.run_id,
                        "created_at": started,
                        "updated_at": time.time(),
                        "run_turn_count": current.turn_count,
                        "run_tool_count": current.tool_count,
                        "session_end_reason": current.session_end_reason,
                        "session_end_summary": current.session_end_summary,
                        "run_backend": "codex-cli",
                        "run_model": config.model,
                        "run_token_usage": current.token_usage,
                    },
                }
            )
        )

    print(
        json.dumps(
            {
                "workspace": str(workspaces),
                "backend": "codex-cli",
                "model": config.model,
            }
        ),
        flush=True,
    )
    await runner.run(session, None, diagnostics_callback=snapshot)
    snapshot(session)
    history = read_history_direct(session.run_id)
    assert history[0]["type"] == "RunInput"
    assert history[0]["content"] == [{"type": "text", "text": prompt}]
    assert history[-1]["type"] == "RunEnded"
    assert session.status == "completed", session.session_end_reason
    assert session.tool_count > 0
    assert any(
        "LIVEVIEW_TRUTH_TOOL_RESULT" in json.dumps(message["content"])
        for message in history
        if message["role"] == "user" and message.get("type") != "RunInput"
    )
    assert "LIVEVIEW_TRUTH_REPLY" in session.output_text
    print(
        json.dumps(
            {
                "result": "PASS",
                "workspace": str(workspaces),
                "run_id": session.run_id,
                "messages": len(history),
                "tools": session.tool_count,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
