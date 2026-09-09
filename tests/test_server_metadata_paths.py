"""Server status/stop --workspace path normalization.

Historical defect: the daemon wrote ``workspace_root: "workspace"``
(relative) into metadata.json; ``server status --workspace
./workspace`` (or any absolute path) then matched neither the slug
directory nor the fallback string compare — reporting "No orchestrator
metadata found" while the daemon was running.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestratord.cli.server import _find_metadata
from orchestratord.workspace_locator import write_orchestrator_metadata


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    # Redirect the metadata root too — the locator computes it from
    # Path.home() at import time, so a plain home patch would leak test
    # metadata into the developer's real ~/.orchestratord directory.
    import orchestratord.cli.server as server_cli
    import orchestratord.workspace_locator as locator

    metadata_root = home / ".orchestratord" / "orchestrator"
    monkeypatch.setattr(locator, "ORCHESTRATORD_ORCHESTRATOR_DIR", metadata_root)
    # server.py binds the constant via `from … import` at module level —
    # patch that binding as well.
    monkeypatch.setattr(server_cli, "ORCHESTRATORD_ORCHESTRATOR_DIR", metadata_root)
    repo = tmp_path / "repo"
    (repo / "workspace").mkdir(parents=True)
    monkeypatch.chdir(repo)
    return repo


def test_metadata_writer_stores_absolute_workspace_root(project: Path) -> None:
    """The daemon-side writer must persist a resolved absolute path so
    CLI callers from any CWD can match it.
    """
    md = write_orchestrator_metadata("workspace")

    data = json.loads(md.read_text(encoding="utf-8"))
    stored = data["workspace_root"]
    assert Path(stored).is_absolute(), f"workspace_root must be absolute, got {stored!r}"
    assert Path(stored) == project / "workspace"


def test_status_matches_legacy_relative_metadata(project: Path) -> None:
    """Legacy metadata with a relative workspace_root must still be
    found by `server status --workspace ./workspace`.
    """
    # Simulate the legacy daemon: relative root stored in metadata.
    md = write_orchestrator_metadata("workspace")
    data = json.loads(md.read_text(encoding="utf-8"))
    data["workspace_root"] = "workspace"  # legacy relative value
    md.write_text(json.dumps(data), encoding="utf-8")

    args = SimpleNamespace(
        workspace="./workspace", workflow=None, server_subcommand="status"
    )
    found_path, found = _find_metadata(args)

    assert found is not None, (
        "--workspace ./workspace must match metadata regardless of how "
        "the path was stored"
    )
    assert found_path == md


def test_status_matches_absolute_metadata(project: Path) -> None:
    """Modern (absolute) metadata + explicit --workspace must resolve."""
    md = write_orchestrator_metadata(project / "workspace")

    args = SimpleNamespace(
        workspace=str(project / "workspace"),
        workflow=None,
        server_subcommand="status",
    )
    found_path, found = _find_metadata(args)

    assert found is not None
    assert found_path == md


# ---------------------------------------------------------------------------
# Launch-context disclosure (backend / runtime / issue counts)
# ---------------------------------------------------------------------------


def test_metadata_writer_persists_backend_and_runtime(project: Path) -> None:
    """backend_name / runtime kwargs land in metadata.json as top-level keys."""
    md = write_orchestrator_metadata(
        "ws_a",
        backend_name="claude",
        runtime={
            "provider": "anthropic",
            "model": "MiniMax-M3",
            "permission_mode": "bypassPermissions",
            "max_concurrent_agents": 1,
            "poll_interval_ms": 5000,
            "approval_policy": "structured",
        },
    )

    data = json.loads(md.read_text(encoding="utf-8"))
    assert data["backend"] == "claude"
    assert data["runtime"]["model"] == "MiniMax-M3"
    assert data["runtime"]["poll_interval_ms"] == 5000


def test_metadata_writer_omits_extras_for_legacy_callers(project: Path) -> None:
    """Callers that do not pass extras keep the legacy metadata shape."""
    md = write_orchestrator_metadata("ws_b")

    data = json.loads(md.read_text(encoding="utf-8"))
    assert "backend" not in data
    assert "runtime" not in data


def test_runtime_lines_render_full_and_legacy(project: Path) -> None:
    from orchestratord.cli.server import _runtime_lines

    full = _runtime_lines(
        {
            "backend": "claude",
            "runtime": {
                "provider": "anthropic",
                "model": "MiniMax-M3",
                "permission_mode": "bypassPermissions",
                "max_concurrent_agents": 2,
                "poll_interval_ms": 5000,
                "approval_policy": "never",
            },
        }
    )
    assert full[0] == "  Backend        : claude"
    assert any("anthropic/MiniMax-M3" in line for line in full)
    assert any("permission_mode=bypassPermissions" in line for line in full)
    assert any("2 concurrent agent(s)" in line for line in full)
    assert any("poll every 5000ms" in line for line in full)

    # Legacy metadata: no backend / runtime → no extra lines at all.
    assert _runtime_lines({"workspace_root": "/tmp/x"}) == []


def test_registry_counts_line_orders_and_tolerates_garbage(
    project: Path, tmp_path: Path
) -> None:
    from orchestratord.cli.server import _registry_counts_line

    ws = project / "workspace"
    registry = ws / ".orchestratord_issue_registry.json"
    registry.write_text(
        json.dumps(
            {
                "i1": {"status": "pending"},
                "i2": {"status": "running"},
                "i3": {"status": "completed"},
                "i4": {"status": "failed"},
                "i5": {"status": "weird_state"},
            }
        ),
        encoding="utf-8",
    )
    line = _registry_counts_line(str(ws))
    assert line == (
        "pending=1 · running=1 · completed=1 · failed=1 · weird_state=1"
    )

    # No file / unknown root → None.
    assert _registry_counts_line(str(tmp_path / "nope")) is None
    assert _registry_counts_line(None) is None
    assert _registry_counts_line("unknown") is None

    # Unreadable garbage → None, never an exception.
    bad = project / "bad"
    bad.mkdir()
    (bad / ".orchestratord_issue_registry.json").write_text("{oops", encoding="utf-8")
    assert _registry_counts_line(str(bad)) is None

    # Empty registry is reported, not hidden.
    empty = project / "empty"
    empty.mkdir()
    (empty / ".orchestratord_issue_registry.json").write_text("{}", encoding="utf-8")
    assert _registry_counts_line(str(empty)) == "none registered"


def _make_daemon_alive(ws: Path) -> None:
    """Rewrite the just-written metadata so its PID is a live process."""
    import os
    import time

    import orchestratord.workspace_locator as locator

    slug = locator._slug_from_workspace(str(ws))
    md = locator.ORCHESTRATORD_ORCHESTRATOR_DIR / slug / "metadata.json"
    meta = json.loads(md.read_text(encoding="utf-8"))
    meta["pid"] = os.getpid()
    meta["started_at"] = time.time()
    md.write_text(json.dumps(meta), encoding="utf-8")


def test_status_output_discloses_backend_and_issues(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`server status` shows backend, agent summary and live issue counts."""
    import time

    from orchestratord.cli.server import _run_status

    ws = project / "workspace"
    write_orchestrator_metadata(
        ws,
        started_at=time.time(),
        backend_name="claude",
        runtime={
            "provider": "anthropic",
            "model": "MiniMax-M3",
            "permission_mode": "bypassPermissions",
            "max_concurrent_agents": 1,
            "poll_interval_ms": 5000,
            "approval_policy": "structured",
        },
    )
    (ws / ".orchestratord_issue_registry.json").write_text(
        json.dumps(
            {
                "a": {"status": "pending"},
                "b": {"status": "running"},
                "c": {"status": "completed"},
            }
        ),
        encoding="utf-8",
    )
    _make_daemon_alive(ws)

    args = SimpleNamespace(
        workspace=str(ws), workflow=None, server_subcommand="status"
    )
    rc = _run_status(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Orchestrator daemon: RUNNING" in out
    assert "Backend        : claude" in out
    assert "Agent          : anthropic/MiniMax-M3" in out
    assert "Concurrency    : 1 concurrent agent(s) · poll every 5000ms" in out
    assert "Issues         : pending=1 · running=1 · completed=1" in out


def test_slug_collision_does_not_cross_match_other_workspace(project: Path) -> None:
    """Different workspaces may share a slug; `server stop` must not adopt
    the other project's metadata just because the slug directory matches."""
    from orchestratord.cli.server import _find_metadata, _slug_from_workspace
    from orchestratord.workspace_locator import (
        _slug_from_workspace as locator_slug,
    )

    # Two distinct workspace roots that collapse to the same slug because
    # only the last 3 path segments are kept.  Both paths exist so
    # get_workspace_root can resolve them.
    ws_a = project / "corp-a" / "shared" / "team" / "repo"
    ws_b = project / "corp-b" / "shared" / "team" / "repo"
    ws_a.mkdir(parents=True)
    ws_b.mkdir(parents=True)

    # Write metadata for ws_a only.
    md_a = write_orchestrator_metadata(ws_a)
    slug = locator_slug(str(ws_a))

    # Sanity: slugs really are identical.
    assert slug == locator_slug(str(ws_b)) == _slug_from_workspace(str(ws_b))
    assert md_a.parent.name == slug

    # `server stop --workspace ws_b` must NOT resolve to ws_a's metadata —
    # the slug path hit is only valid when workspace_root also matches.
    args = SimpleNamespace(
        workspace=str(ws_b), workflow=None, server_subcommand="stop"
    )
    found_path, found = _find_metadata(args)

    assert found is None, (
        f"slug collision cross-matched another project's metadata: "
        f"stop for {ws_b} adopted {found_path}"
    )


def test_status_output_omits_new_lines_for_legacy_metadata(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Old daemons' metadata renders exactly as before (no new lines)."""
    import time

    from orchestratord.cli.server import _run_status

    ws = project / "workspace"
    write_orchestrator_metadata(ws, started_at=time.time())
    _make_daemon_alive(ws)

    args = SimpleNamespace(
        workspace=str(ws), workflow=None, server_subcommand="status"
    )
    rc = _run_status(args)
    out = capsys.readouterr().out

    assert rc == 0
    assert "Orchestrator daemon: RUNNING" in out
    assert "Backend" not in out
    assert "Agent          :" not in out
    assert "Issues         :" not in out
    assert "Uptime" in out
