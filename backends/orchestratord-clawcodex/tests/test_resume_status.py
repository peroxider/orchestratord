"""Clawcodex-specific probe_resume() tests (ADR-003 §2.5).

Lives in the backend package because it stubs the clawcodex SDK's
runtime namespace (which the production ``orchestratord_clawcodex.session``
module imports lazily inside ``probe_resume()``).  Tests for the
generic SPI / other backends (codex / dsh / hermes / opencode) remain
at the repo-root ``tests/`` tree.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

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
