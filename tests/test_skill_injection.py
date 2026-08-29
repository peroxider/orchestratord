"""Tests for skill prompt injection (Scheme C).

Covers ``build_skill_index`` / ``load_skill`` in
``orchestratord.skills.tools`` and the best-effort injection in
``BackendRunner._append_skill_index``.
"""

from __future__ import annotations

import pytest

from orchestratord.backend_runner import BackendRunner
from orchestratord.skills.tools import (
    SkillNotFoundError,
    build_skill_index,
    load_skill,
    reset_cached_skills,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_cached_skills()
    yield
    reset_cached_skills()


class TestBuildSkillIndex:
    def test_contains_header_and_all_skills(self):
        index = build_skill_index()
        assert "## 可用 Skills（agent 可调用）" in index
        assert "- mode-selector:" in index
        assert "- capability-explainer:" in index
        assert "- failure-recovery:" in index

    def test_includes_load_skill_invocation_hint(self):
        assert "load_skill(name='<name>')" in build_skill_index()

    def test_preserves_base_append(self):
        base = "BASE RULES\nline2"
        out = build_skill_index(base_append=base)
        assert out.startswith(base)
        assert "## 可用 Skills" in out


class TestLoadSkill:
    def test_returns_body_without_frontmatter(self):
        body = load_skill("mode-selector")
        assert body.startswith("#")  # markdown body
        assert "name:" not in body.split("\n")[0]
        assert not body.lstrip().startswith("---")

    def test_unknown_name_raises(self):
        with pytest.raises(SkillNotFoundError):
            load_skill("no-such-skill")

    def test_error_has_name_attribute(self):
        try:
            load_skill("nope")
        except SkillNotFoundError as exc:
            assert exc.name == "nope"
        else:  # pragma: no cover
            pytest.fail("expected SkillNotFoundError")


class TestRunnerInjection:
    def test_appends_index_to_base(self):
        out = BackendRunner._append_skill_index("BASE")
        assert out.startswith("BASE")
        assert "## 可用 Skills（agent 可调用）" in out

    def test_survives_injection_failure(self, monkeypatch):
        import orchestratord.skills.tools as tools_mod

        def _boom(*, base_append: str) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(tools_mod, "build_skill_index", _boom)
        out = BackendRunner._append_skill_index("BASE")
        assert out == "BASE"

    def test_empty_base_still_yields_index(self):
        out = BackendRunner._append_skill_index("")
        assert "## 可用 Skills（agent 可调用）" in out
