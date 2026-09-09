"""Migration-file inspection tests (§6.1 migration rules, no live DB).

Parse ``alembic.ini`` and ``alembic/versions/*.py`` to assert the
non-negotiable rules: no ``ForeignKey`` / cascading DDL anywhere, every index
is ``CONCURRENTLY``, each ``CONCURRENTLY`` index lives in its own file, the
version table is ``schema_migrations``, and the ``events`` partition + GIN +
lookup directives are present.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VERSIONS = REPO / "alembic" / "versions"
INI = REPO / "alembic.ini"


def _version_files() -> list[Path]:
    return sorted(VERSIONS.glob("*.py"))


def _index_files() -> list[Path]:
    return [p for p in _version_files() if "_index_" in p.name]


EXPECTED_INDEX_NAMES = {
    "uq_workspaces_slug",
    "uq_agents_workspace_name",
    "uq_runtimes_token_hash",
    "uq_auth_tokens_token_hash",
    "uq_skills_name",
    "uq_approvals_session_request",
    "uq_usage_aggregates_bucket",
    "ix_events_payload_gin",
    "ix_events_session_sequence",
    "ix_events_workspace_created",
    "ix_events_issue_created",
    "ix_members_workspace_id",
    "ix_agents_runtime_id",
    "uq_agent_capabilities_cache_agent_id",
    "ix_runtimes_workspace_id",
    "ix_runtime_backends_runtime_id",
    "ix_issues_workspace_id",
    "ix_issues_assignee_type_id",
    "ix_issue_comments_issue_id",
    "ix_issue_status_history_issue_id",
    "ix_sessions_workspace_issue_agent_run",
    "ix_runs_workspace_id",
    "ix_skill_source_maps_skill_id",
    "ix_skill_references_skill_source_map",
    "ix_inbox_workspace_id",
    "ix_squads_workspace_id",
    "ix_projects_workspace_id",
    "ix_project_repos_project_id",
    "ix_project_docs_project_id",
    "ix_autopilots_workspace_id",
    "ix_autopilot_runs_autopilot_run",
    "ix_audit_log_workspace_created",
    "ix_auth_tokens_workspace_id",
    "ix_channels_workspace_id",
    "uq_integrations_workspace_provider",
    "ix_installations_workspace_id",
    "uq_installations_installation_id",
    "ix_pull_requests_issue_id",
    "uq_pull_requests_repo_number",
}


def test_version_table_is_application_specific() -> None:
    assert "version_table = orchestratord_schema_migrations" in INI.read_text(
        encoding="utf-8"
    )


def test_no_foreign_key_or_cascade_in_migrations() -> None:
    for path in _version_files():
        text = path.read_text(encoding="utf-8").lower()
        # "foreignkey(" — with the open paren — matches a real FK declaration
        # but not the ``ForeignKey`` word in the 0001 docstring.
        assert "foreignkey(" not in text, f"{path.name} declares a ForeignKey"
        assert "cascade" not in text, f"{path.name} contains cascade DDL"


def test_create_tables_has_no_indexes() -> None:
    text = (VERSIONS / "0001_create_tables.py").read_text(encoding="utf-8").lower()
    # The 0001 docstring *mentions* CONCURRENTLY indexes as prose, but the body
    # must not emit any index DDL — only ``op.create_table`` calls.
    assert "op.create_index(" not in text
    assert "op.execute(" not in text


def test_each_concurrently_index_in_own_file() -> None:
    assert len(_index_files()) == 40
    for path in _index_files():
        text = path.read_text(encoding="utf-8")
        statements = text.count("op.create_index(") + text.count(
            "CREATE INDEX CONCURRENTLY"
        )
        assert statements == 1, f"{path.name} must contain exactly one index"


def test_every_index_is_concurrent() -> None:
    for path in _index_files():
        text = path.read_text(encoding="utf-8")
        is_concurrent = (
            "postgresql_concurrently=True" in text
            or "CREATE INDEX CONCURRENTLY" in text
        )
        assert is_concurrent, f"{path.name} index is not CONCURRENTLY"


def test_events_partition_and_index_directives_present() -> None:
    combined = "\n".join(p.read_text(encoding="utf-8") for p in _version_files())
    for needle in (
        "RANGE (created_at)",
        "ix_events_payload_gin",
        "jsonb_path_ops",
        "ix_events_session_sequence",
        "ix_events_workspace_created",
        "ix_events_issue_created",
    ):
        assert needle in combined, f"missing {needle!r} in migrations"


def test_full_index_inventory_present() -> None:
    combined = "\n".join(p.read_text(encoding="utf-8") for p in _version_files())
    missing = sorted(name for name in EXPECTED_INDEX_NAMES if name not in combined)
    assert not missing, f"missing indexes: {missing}"


# -- PR-B1: migration 0048 adds ``peers.client_kind`` --


def test_migration_0048_adds_client_kind_with_default_v1_sunset() -> None:
    """PR-B1: migration 0048 backfills ``peers.client_kind`` with the
    ``v1_sunset`` default — Phase 1 peers (no ``peer_client_version``
    in invite) are auto-classified on upgrade.

    Pins three non-negotiable properties:

    1. ``down_revision = "0047"`` — the migration sits on top of the
       last pre-Phase B migration, with no branch.
    2. ``upgrade()`` adds a ``client_kind`` column to ``peers``,
       ``nullable=False``, with ``server_default="v1_sunset"`` so
       existing rows adopt the legacy classification automatically
       (Postgres backfills the literal on ``ALTER TABLE ADD COLUMN
       ... DEFAULT``).
    3. ``downgrade()`` drops the column — round-trip safe.
    """
    path = VERSIONS / "0048_add_peer_client_kind.py"
    assert path.exists(), "migration 0048 missing"
    text = path.read_text(encoding="utf-8")

    assert 'down_revision = "0047"' in text
    assert 'op.add_column(\n        "peers",' in text or (
        'op.add_column("peers",' in text
    ), "0048 must add_column('peers', ...)"
    assert "sa.Column(" in text
    assert '"client_kind"' in text
    assert "nullable=False" in text
    assert 'server_default="v1_sunset"' in text
    # Round-trip: downgrade drops the column.
    assert 'op.drop_column("peers", "client_kind")' in text


def test_migration_0048_keeps_server_default_for_legacy_clients() -> None:
    """PR-B1 invariant: the ``server_default`` is intentionally **kept**
    on the column, not dropped in a follow-up ``alter_column``. A future
    Phase 1 client that re-omits ``peer_client_version`` would otherwise
    violate ``NOT NULL`` on insert — the default is the safety net."""
    import re

    path = VERSIONS / "0048_add_peer_client_kind.py"
    text = path.read_text(encoding="utf-8")
    # Strip the docstring (which mentions "alter_column" as prose) and
    # only assert on actual ``op.alter_column(...)`` calls in the body.
    body_only = re.sub(r'"""[\s\S]*?"""', "", text, count=1)
    assert (
        "alter_column" not in body_only
    ), "0048 must not alter_column — the v1_sunset server_default is the safety net"
