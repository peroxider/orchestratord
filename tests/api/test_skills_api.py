"""Skills REST API contract (Phase 1, §5.2.5).

These tests pin the HTTP surface the new FastAPI app must expose for the
Skills browser page. They enforce two architectural invariants from
docs/FEATURE_GAP_VS_MULTICA.md \u00a73.3:

* Skills API must NOT re-implement SHA256 source-map verification \u2014 it
  reuses ``orchestratord.skills.loader.load_all_skills`` so drift
  detection stays single-sourced.
* ``refresh-hashes`` must require admin authorization.

Tests fail today; they encode the contract.
"""
from __future__ import annotations

from tests.api.conftest import _repo_override


def _client():
    """A fresh app with the token gate lifted (skills-surface contract only).

    The gate itself is exercised in ``tests/api/test_auth.py``.  Repos are
    overridden to the dedicated test database (a fresh ``create_app()``
    would otherwise fall through to the real ``get_repositories`` and its
    default DSN — the shared ``multica`` instance).  ``NullPool`` keeps
    connections from crossing the TestClient portal loop and the
    pytest-asyncio loop.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from fastapi.testclient import TestClient

    from orchestratord.api.app import create_app
    from orchestratord.api.db import get_repositories
    from orchestratord.api.deps import require_auth
    from orchestratord.db.engine import build_session_factory
    from tests.api.conftest import _TEST_DSN

    ws_engine = create_async_engine(_TEST_DSN, poolclass=NullPool)
    application = create_app()
    application.dependency_overrides[get_repositories] = _repo_override(
        build_session_factory(ws_engine)
    )
    application.dependency_overrides[require_auth] = lambda: None
    return TestClient(application)


class TestSkillsListEndpoint:
    """``GET /api/skills`` \u2014 directory of all skills with stale flags."""

    def test_lists_all_builtin_skills(self) -> None:
        client = _client()
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        names = {s["name"] for s in body}
        assert {"mode-selector", "capability-explainer", "failure-recovery"} <= names

    def test_each_entry_has_required_fields(self) -> None:
        client = _client()
        resp = client.get("/api/skills")
        body = resp.json()
        required = {"name", "display_name", "description", "is_stale", "stale_reasons"}
        for entry in body:
            missing = required - set(entry)
            assert not missing, f"skill {entry.get('name')!r} missing fields {missing}"

    def test_stale_flag_matches_loader_state(self) -> None:
        """API must NOT re-verify; it must read ``Skill.is_stale`` as-is."""
        from orchestratord.skills.loader import load_all_skills

        live = {s.name: s.is_stale for s in load_all_skills()}
        body = _client().get("/api/skills").json()
        for entry in body:
            assert entry["is_stale"] is live[entry["name"]], (
                f"API stale flag diverges from loader for {entry['name']!r}"
            )


class TestSkillDetailEndpoint:
    """``GET /api/skills/{name}`` \u2014 full SKILL.md body + parsed frontmatter."""

    def test_returns_skill_md_body(self) -> None:
        client = _client()
        resp = client.get("/api/skills/mode-selector")
        assert resp.status_code == 200
        body = resp.json()
        assert "skill_md" in body
        assert "name: mode-selector" in body["skill_md"]

    def test_includes_source_map_refs(self) -> None:
        client = _client()
        body = client.get("/api/skills/mode-selector").json()
        assert "source_map" in body
        assert isinstance(body["source_map"], list)
        if body["source_map"]:
            ref = body["source_map"][0]
            for key in ("file_path", "start_line", "end_line", "expected_sha256_prefix"):
                assert key in ref

    def test_404_for_unknown_skill(self) -> None:
        client = _client()
        resp = client.get("/api/skills/__no_such_skill__")
        assert resp.status_code == 404


class TestSkillSourceMapEndpoint:
    """``GET /api/skills/{name}/source-map`` \u2014 raw source-map table."""

    def test_returns_full_ref_list(self) -> None:
        client = _client()
        body = client.get("/api/skills/mode-selector/source-map").json()
        assert isinstance(body, list)
        for ref in body:
            assert len(ref["expected_sha256_prefix"]) >= 8
            assert int(ref["start_line"]) < int(ref["end_line"])

    def test_404_for_unknown_skill(self) -> None:
        client = _client()
        resp = client.get("/api/skills/__no_such__/source-map")
        assert resp.status_code == 404


class TestSkillVerifyEndpoint:
    """``POST /api/skills/{name}/verify`` \u2014 re-runs loader's verification."""

    def test_returns_verify_outcome(self) -> None:
        client = _client()
        resp = client.post("/api/skills/mode-selector/verify")
        assert resp.status_code == 200
        body = resp.json()
        assert "verified" in body and isinstance(body["verified"], bool)
        assert "stale_refs" in body and isinstance(body["stale_refs"], list)

    def test_verify_idempotent(self) -> None:
        client = _client()
        first = client.post("/api/skills/mode-selector/verify").json()
        second = client.post("/api/skills/mode-selector/verify").json()
        assert first["verified"] == second["verified"]


class TestRefreshHashesEndpoint:
    """``POST /api/skills/refresh-hashes`` \u2014 admin-only hash regeneration."""

    def test_requires_admin_role(self) -> None:
        client = _client()
        no_auth = client.post("/api/skills/refresh-hashes")
        assert no_auth.status_code in (401, 403), (
            f"refresh-hashes accepted anonymous caller with {no_auth.status_code}"
        )

    def test_admin_call_dry_run_default(self) -> None:
        """Default behavior is dry-run (no file mutation) for safety."""
        client = _client()
        from orchestratord.api.deps import admin_principal_override

        with admin_principal_override():
            resp = client.post(
                "/api/skills/refresh-hashes",
                json={"dry_run": True},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert "would_update" in body
        assert "actually_updated" in body
        assert body["actually_updated"] == []
