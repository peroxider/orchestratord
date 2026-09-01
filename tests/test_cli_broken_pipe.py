"""CLI must exit quietly when the consumer closes the pipe early.

Reproduction: ``orchestratord run logs --id X | head -12`` — head closes
the pipe after 12 lines while the transcript viewer is still printing
thousands of lines. Python turns the resulting SIGPIPE/EPIPE into
``BrokenPipeError`` and used to spew a full traceback (and a nonzero
exit code) even though the consumer got exactly what it asked for.

Contract: a closed stdout pipe is a *normal* termination for a
print-heavy read-only command — exit 0, no traceback.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def run_workspace(tmp_path: Path) -> tuple[Path, str]:
    """A RunStore record + a large stage transcript under tmp."""
    from orchestratord.run_store import RunRecord, RunStore

    ws = tmp_path / "ws"
    ws.mkdir()
    store = RunStore(ws)
    run_id = "11111111-2222-3333-4444-555555555555"
    stage_run_id = "stage-01-abcdef42"
    record = RunRecord(run_id=run_id, workflow="t", task_id="t", task_kind="generic")
    record.status = "completed"
    record.metadata = {"stage_run_ids": {"1": stage_run_id}}
    store.save(record)

    transcript_dir = Path.home() / ".orchestratord" / "sessions" / stage_run_id
    # tests redirect HOME via monkeypatch below — write through the
    # same constant the CLI resolves at import time instead.
    from orchestratord.cli.issue import SESSIONS_DIR

    transcript_dir = SESSIONS_DIR / stage_run_id
    transcript_dir.mkdir(parents=True, exist_ok=True)
    row = json.dumps(
        {"role": "assistant", "content": [{"type": "text", "text": "x" * 120}]},
        ensure_ascii=False,
    )
    # >64KB pipe buffer so the writer is guaranteed to hit EPIPE.
    transcript = "\n".join([row] * 3000) + "\n"
    (transcript_dir / "transcript.jsonl").write_text(transcript, encoding="utf-8")
    return ws, run_id


def test_run_logs_exits_cleanly_on_closed_pipe(
    run_workspace: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, run_id = run_workspace
    monkeypatch.setenv("ORCHESTRATORD_WORKSPACE_ROOT", str(ws))

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from orchestratord.cli.main import app; app()",
            "run", "logs", "--id", run_id, "--workspace", str(ws),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
    )
    assert proc.stdout is not None
    # Consumer (head) closes the pipe immediately after spawning.
    proc.stdout.close()
    try:
        _, stderr = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail("run logs hung after the pipe was closed")

    assert proc.returncode == 0, (
        f"expected a clean exit, got rc={proc.returncode}\n"
        f"stderr: {stderr.decode(errors='replace')[-800:]}"
    )
    assert b"BrokenPipeError" not in stderr, (
        "closed stdout pipe must not produce a traceback"
    )
