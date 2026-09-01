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
