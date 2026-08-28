"""Tests for the 3-state ResumeStatus + per-backend probe_resume() (ADR-003).

DESIGN_graded_timeouts_and_resume.md §2.5 / §3.5 verification matrix:

  * clawcodex   → RESUMED   (real probe via SDK)
  * codex (AS)  → RESUMED   (real probe via MCP)
  * codex (CLI) → UNDETECTABLE (no cross-process state)
  * dsh         → UNDETECTABLE (SDK has no probe)
  * hermes      → REJECTED    (explicitly unsupported)
  * opencode    → RESUMED   (real probe via session/load HTTP)

When ``resume_session_id`` is None the session is fresh, so all
backends return RESUMED (vacuously true).

Each test stubs the SDK call path so the test runs offline.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus


# ---------------------------------------------------------------------------
# ClawcodexBackend
# ---------------------------------------------------------------------------


class TestClawcodexProbeResume:
    def test_no_resume_session_id_returns_resumed(self) -> None:
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp")
        session = ClawcodexSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_sdk_probe_true_returns_resumed(self) -> None:
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            fake_query = SimpleNamespace(
                QueryRunner=SimpleNamespace(
                    probe_transcript=lambda *a, **kw: True
                ),
                QueryConfig=lambda **kw: None,
            )
            with patch.dict("sys.modules",
                            {"extensions.api.query": fake_query}):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_sdk_probe_false_returns_rejected(self) -> None:
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-gone")
        session = ClawcodexSession(spec)
        try:
            fake_query = SimpleNamespace(
                QueryRunner=SimpleNamespace(
                    probe_transcript=lambda *a, **kw: False
                ),
                QueryConfig=lambda **kw: None,
            )
            with patch.dict("sys.modules",
                            {"extensions.api.query": fake_query}):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.REJECTED
        finally:
            session.close_sync()

    def test_sdk_missing_returns_undetectable(self) -> None:
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            with patch.dict("sys.modules",
                            {"extensions.api.query": None}):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()


# ---------------------------------------------------------------------------
# CodexAppServerSession — needs worker; tests stub ``_worker.session_load``
# ---------------------------------------------------------------------------


class TestCodexAppServerProbeResume:
    def test_no_resume_returns_resumed(self) -> None:
        from orchestratord_codex.app_server_session import (
            CodexAppServerSession,
        )

        spec = SessionSpec(cwd="/tmp")
        session = CodexAppServerSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_no_worker_returns_undetectable(self) -> None:
        from orchestratord_codex.app_server_session import (
            CodexAppServerSession,
        )

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = CodexAppServerSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()

    def test_worker_200_returns_resumed(self) -> None:
        from orchestratord_codex.app_server_session import (
            CodexAppServerSession,
        )

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = CodexAppServerSession(spec)
        fake_worker = AsyncMock()
        fake_worker.session_load = AsyncMock(
            return_value={"result": {"exists": True}}
        )
        session._worker = fake_worker
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_worker_error_returns_rejected(self) -> None:
        from orchestratord_codex.app_server_session import (
            CodexAppServerSession,
        )

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = CodexAppServerSession(spec)
        fake_worker = AsyncMock()
        fake_worker.session_load = AsyncMock(
            return_value={"error": {"code": 404, "message": "not found"}}
        )
        session._worker = fake_worker
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.REJECTED
        finally:
            session.close_sync()


# ---------------------------------------------------------------------------
# CodexSession (CLI) — always UNDETECTABLE on resume
# ---------------------------------------------------------------------------


class TestCodexCliProbeResume:
    def test_no_resume_returns_resumed(self) -> None:
        from orchestratord_codex.session import CodexSession

        spec = SessionSpec(cwd="/tmp")
        session = CodexSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            asyncio.run(session.close())

    def test_resume_returns_undetectable(self) -> None:
        from orchestratord_codex.session import CodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = CodexSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.UNDETECTABLE
        finally:
            asyncio.run(session.close())


# ---------------------------------------------------------------------------
# DshSession — always UNDETECTABLE on resume
# ---------------------------------------------------------------------------


class TestDshProbeResume:
    def test_no_resume_returns_resumed(self) -> None:
        from orchestratord_dsh.session import DshSession

        spec = SessionSpec(cwd="/tmp")
        session = DshSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_resume_returns_undetectable(self) -> None:
        from orchestratord_dsh.session import DshSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = DshSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()


# ---------------------------------------------------------------------------
# HermesSession — always REJECTED on resume
# ---------------------------------------------------------------------------


class TestHermesProbeResume:
    def test_no_resume_returns_resumed(self) -> None:
        from orchestratord_hermes.session import HermesSession

        spec = SessionSpec(cwd="/tmp")
        session = HermesSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            asyncio.run(session.close())

    def test_resume_returns_rejected(self) -> None:
        from orchestratord_hermes.session import HermesSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = HermesSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.REJECTED
        finally:
            asyncio.run(session.close())


# ---------------------------------------------------------------------------
# OpenCodeSession — needs httpx + server; tests stub the client
# ---------------------------------------------------------------------------


class TestOpenCodeProbeResume:
    def test_no_resume_returns_resumed(self) -> None:
        from orchestratord_opencode.session import OpenCodeSession

        spec = SessionSpec(cwd="/tmp")
        session = OpenCodeSession(spec)
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_resume_404_returns_rejected(self) -> None:
        from orchestratord_opencode.session import OpenCodeSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-gone")
        session = OpenCodeSession(spec)
        fake_client = AsyncMock()
        resp = AsyncMock()
        resp.status_code = 404
        fake_client.post = AsyncMock(return_value=resp)
        fake_client.aclose = AsyncMock()
        session._client = fake_client
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.REJECTED
        finally:
            session.close_sync()

    def test_resume_500_returns_undetectable(self) -> None:
        from orchestratord_opencode.session import OpenCodeSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = OpenCodeSession(spec)
        fake_client = AsyncMock()
        resp = AsyncMock()
        resp.status_code = 500
        fake_client.post = AsyncMock(return_value=resp)
        fake_client.aclose = AsyncMock()
        session._client = fake_client
        try:
            status = asyncio.run(session.probe_resume())
            assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()


# ---------------------------------------------------------------------------
# ResumeStatus enum properties
# ---------------------------------------------------------------------------


class TestResumeStatusEnum:
    def test_three_values(self) -> None:
        values = {s.value for s in ResumeStatus}
        assert values == {"resumed", "rejected", "undetectable"}

    def test_member_values(self) -> None:
        # Enum members compare by identity but the values are strings.
        assert ResumeStatus.RESUMED.value == "resumed"
        assert ResumeStatus.REJECTED.value == "rejected"
        assert ResumeStatus.UNDETECTABLE.value == "undetectable"


# ---------------------------------------------------------------------------
# SessionResult dataclass
# ---------------------------------------------------------------------------


class TestSessionResult:
    def test_minimal_construction(self) -> None:
        from orchestratord.spi.session import SessionResult

        r = SessionResult(status=ResumeStatus.RESUMED)
        assert r.status is ResumeStatus.RESUMED
        assert r.final_text is None
        assert r.last_event_seq is None
        assert r.resume_target_session_id is None
        assert r.reason is None
        assert r.error_code is None

    def test_full_construction(self) -> None:
        from orchestratord.spi.session import SessionResult

        r = SessionResult(
            status=ResumeStatus.REJECTED,
            final_text="partial output before reject",
            last_event_seq=42,
            resume_target_session_id="sess-x",
            reason="transcript-gc",
            error_code="transcript_gone",
        )
        assert r.status is ResumeStatus.REJECTED
        assert r.final_text == "partial output before reject"
        assert r.last_event_seq == 42
        assert r.resume_target_session_id == "sess-x"
        assert r.reason == "transcript-gc"
        assert r.error_code == "transcript_gone"


# ---------------------------------------------------------------------------
# BackendCapabilities.resume_detection bit
# ---------------------------------------------------------------------------


class TestCapabilitiesResumeDetectionBit:
    def test_default_is_false(self) -> None:
        from orchestratord.spi.capabilities import BackendCapabilities

        caps = BackendCapabilities()
        assert caps.resume_detection is False

    def test_can_be_set_true(self) -> None:
        from orchestratord.spi.capabilities import BackendCapabilities

        caps = BackendCapabilities(resume_detection=True)
        assert caps.resume_detection is True