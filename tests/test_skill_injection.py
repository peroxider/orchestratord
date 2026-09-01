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


class TestSkillToolExposure:
    """Scheme C §3.2: ``load_skill`` must be exposed on every SessionSpec."""

    @staticmethod
    def _fake_session() -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            workspace=SimpleNamespace(path="."),
            run_id=None,
            _runtime_tasks=None,
        )

    @staticmethod
    def _build_spec(*, tools_allow: list[str] | None) -> object:
        from orchestratord.config.schema import AgentConfig, SandboxConfig

        agent_config = AgentConfig()
        if tools_allow is not None:
            # ``tools_allow`` is a duck-typed runtime attribute on
            # AgentConfig (see BackendRunner._build_session_spec).
            agent_config.tools_allow = tools_allow  # type: ignore[attr-defined]
        runner = BackendRunner(
            backend=object(),  # type: ignore[arg-type]
            agent_config=agent_config,
            sandbox_config=SandboxConfig(),
        )
        session = runner._build_session_spec(
            session=TestSkillToolExposure._fake_session(),  # type: ignore[arg-type]
            workflow=None,  # type: ignore[arg-type]
            system_prompt="SYS",
        )
        return session

    def test_load_skill_appended_to_allow_list(self):
        spec = self._build_spec(tools_allow=["Bash", "Grep"])
        assert "load_skill" in spec.tools_allow
        assert spec.tools_allow[:2] == ["Bash", "Grep"]

    def test_no_duplicate_load_skill_when_already_present(self):
        spec = self._build_spec(tools_allow=["load_skill", "Read"])
        assert spec.tools_allow.count("load_skill") == 1

    def test_no_allow_list_means_no_filter(self):
        spec = self._build_spec(tools_allow=None)
        assert spec.tools_allow is None

    def test_skill_tool_description_in_extra(self):
        spec = self._build_spec(tools_allow=None)
        skill_tools = spec.extra["skill_tools"]
        assert skill_tools[0]["name"] == "load_skill"
        assert "name" in skill_tools[0]["parameters"]["required"]

    def test_runtime_tasks_extra_preserved(self):
        from types import SimpleNamespace

        from orchestratord.config.schema import AgentConfig, SandboxConfig

        runner = BackendRunner(
            backend=object(),  # type: ignore[arg-type]
            agent_config=AgentConfig(),
            sandbox_config=SandboxConfig(),
        )
        spec = runner._build_session_spec(
            session=SimpleNamespace(
                workspace=SimpleNamespace(path="."),
                run_id=None,
                _runtime_tasks={"task-1": "running"},
            ),  # type: ignore[arg-type]
            workflow=None,  # type: ignore[arg-type]
            system_prompt="SYS",
        )
        assert spec.extra["runtime_tasks"] == {"task-1": "running"}

    def test_current_run_id_is_not_an_implicit_resume_target(self):
        from types import SimpleNamespace

        from orchestratord.config.schema import AgentConfig, SandboxConfig

        runner = BackendRunner(
            backend=object(),  # type: ignore[arg-type]
            agent_config=AgentConfig(),
            sandbox_config=SandboxConfig(),
        )
        spec = runner._build_session_spec(
            session=SimpleNamespace(
                workspace=SimpleNamespace(path="."),
                run_id="new-run",
                debug_log_path=None,
                _runtime_tasks=None,
            ),  # type: ignore[arg-type]
            workflow=None,  # type: ignore[arg-type]
            system_prompt="SYS",
        )
        assert spec.run_id == "new-run"
        assert spec.resume_session_id is None

    def test_explicit_resume_target_is_preserved(self):
        from types import SimpleNamespace

        from orchestratord.config.schema import AgentConfig, SandboxConfig

        runner = BackendRunner(
            backend=object(),  # type: ignore[arg-type]
            agent_config=AgentConfig(),
            sandbox_config=SandboxConfig(),
        )
        spec = runner._build_session_spec(
            session=SimpleNamespace(
                workspace=SimpleNamespace(path="."),
                run_id="existing-run",
                debug_log_path=None,
                _runtime_tasks=None,
            ),  # type: ignore[arg-type]
            workflow=None,  # type: ignore[arg-type]
            system_prompt="SYS",
            resume_session_id="existing-run",
        )
        assert spec.resume_session_id == "existing-run"

    def test_agent_timeouts_are_mapped_to_session_spec(self):
        from types import SimpleNamespace

        from orchestratord.config.schema import AgentConfig, SandboxConfig

        agent = AgentConfig(
            run_timeout_ms=90_000,
            stall_timeout_ms=45_000,
            stall_warn_ms=5_000,
        )
        runner = BackendRunner(
            backend=object(),  # type: ignore[arg-type]
            agent_config=agent,
            sandbox_config=SandboxConfig(),
        )
        spec = runner._build_session_spec(
            session=SimpleNamespace(
                workspace=SimpleNamespace(path="."),
                run_id="run-1",
                debug_log_path="debug.ndjson",
                _runtime_tasks=None,
            ),  # type: ignore[arg-type]
            workflow=None,  # type: ignore[arg-type]
            system_prompt="SYS",
        )
        assert spec.total_timeout_s == 90.0
        assert spec.inactivity_timeout_s == 45.0
        assert spec.idle_watchdog_timeout_s == 90.0
        assert spec.stall_warn_s == 5.0
        assert spec.debug_log_path == "debug.ndjson"
