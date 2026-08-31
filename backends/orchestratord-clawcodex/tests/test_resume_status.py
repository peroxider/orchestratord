"""Clawcodex-specific probe_resume() tests.

Lives in the backend package because it stubs the clawcodex SDK's
runtime namespace (which the production ``orchestratord_clawcodex.session``
module imports lazily inside ``probe_resume()``).  Tests for the
generic SPI / other backends (codex / dsh / hermes / opencode) remain
at the repo-root ``tests/`` tree.

The probe has two paths after Plan A + bypass:

  1. SDK path  — ``extensions.api.query.QueryRunner.probe_transcript``
     (clawcodex ≥…).
  2. Bypass     — ``clawcodex_ext.services.session_storage
     .resolve_sessions_dir`` directory check, mirroring the CLI's
     ``--resume`` validation in ``clawcodex_ext/cli/dispatch.py``.

These tests cover both paths plus the dispatcher's fall-through
semantics (SDK raises / SDK missing attribute / both unavailable).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.session import ResumeStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sdk_query(probe_return=..., *, raise_exc: Exception | None = None):
    """Build a fake ``extensions.api.query`` module for sys.modules patching.

    ``probe_return`` accepts:
      * ``True`` / ``False`` — probe_transcript returns that bool
      * ``...`` (Ellipsis)  — probe_transcript is missing entirely
        (older clawcodex without Plan A); used to test the
        "attribute missing → fall through to bypass" path.

    ``raise_exc`` (if given) is the exception ``probe_transcript``
    raises on every call — used to test the "SDK probe raises →
    fall through to bypass" path.
    """
    runner_ns: dict[str, object] = {}
    if raise_exc is not None:
        _exc = raise_exc
        def _raising_probe(*_a, **_kw):
            raise _exc
        runner_ns["probe_transcript"] = _raising_probe
    elif probe_return is not ...:
        _ret = probe_return
        runner_ns["probe_transcript"] = lambda *a, **_kw: _ret
    # else: probe_return is Ellipsis → attribute deliberately missing.
    return SimpleNamespace(
        QueryRunner=SimpleNamespace(**runner_ns),
        QueryConfig=lambda **kw: None,
    )


def _make_storage_module(resolve_returns: Path):
    """Build a fake ``clawcodex_ext.services.session_storage`` module
    whose ``resolve_sessions_dir()`` returns the supplied Path.
    """
    return SimpleNamespace(resolve_sessions_dir=lambda: resolve_returns)


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

    @pytest.mark.parametrize(
        "blank", ["   ", "\t", "\n", " \t\n ", "\u00a0"],
        ids=["spaces", "tab", "newline", "mixed-whitespace", "nbsp"],
    )
    def test_whitespace_resume_session_id_returns_resumed(self, blank: str) -> None:
        """Whitespace-only ``resume_session_id`` is treated as "no target"
        and short-circuits to RESUMED, matching the clawcodex
        ``probe_transcript`` contract (whitespace → False) by keeping
        the two surfaces consistent: a blank target is never rejected."""
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id=blank)
        session = ClawcodexSession(spec)
        try:
            # Even with both SDK and bypass available, a whitespace-only
            # id must not enter the probe path.
            fake_query = _make_sdk_query(probe_return=False)
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": fake_query,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(Path("/nonexistent"))
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_sdk_probe_true_returns_resumed(self) -> None:
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            fake_query = _make_sdk_query(probe_return=True)
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
            fake_query = _make_sdk_query(probe_return=False)
            with patch.dict("sys.modules",
                            {"extensions.api.query": fake_query}):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.REJECTED
        finally:
            session.close_sync()

    def test_sdk_missing_returns_undetectable(self) -> None:
        """Both the SDK and ``clawcodex_ext.services.session_storage``
        unavailable → ``UNDETECTABLE``. This preserves the legacy
        behavior the orchestrator relied on before the bypass was added.
        """
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": None,
                    "clawcodex_ext.services.session_storage": None,
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()


# ---------------------------------------------------------------------------
# Bypass path — when the SDK is missing or its probe is unavailable
# ---------------------------------------------------------------------------


class TestClawcodexProbeResumeBypass:
    """Plan A's ``QueryRunner.probe_transcript`` is the preferred path,
    but the orchestrator also has to work against older clawcodex
    builds that don't ship that method. The bypass replicates the
    CLI's ``--resume`` directory check.
    """

    def test_sdk_module_missing_bypass_returns_resumed(self, tmp_path) -> None:
        """SDK import fails → fall through to bypass → session dir
        exists → ``RESUMED``."""
        from orchestratord_clawcodex.session import ClawcodexSession

        sess_dir = tmp_path / "sess-x"
        sess_dir.mkdir()

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": None,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(tmp_path)
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_sdk_module_missing_bypass_returns_rejected(self, tmp_path) -> None:
        """SDK import fails → fall through to bypass → session dir
        absent → ``REJECTED``."""
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="missing")
        session = ClawcodexSession(spec)
        try:
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": None,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(tmp_path)
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.REJECTED
        finally:
            session.close_sync()

    def test_sdk_probe_attribute_missing_falls_through_to_bypass(self, tmp_path) -> None:
        """The SDK is importable but lacks ``probe_transcript``
        (older clawcodex without Plan A). The bypass must take over
        instead of returning UNDETECTABLE.
        """
        from orchestratord_clawcodex.session import ClawcodexSession

        sess_dir = tmp_path / "sess-x"
        sess_dir.mkdir()

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            # Build an SDK namespace WITHOUT probe_transcript on QueryRunner.
            fake_query = _make_sdk_query(probe_return=...)
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": fake_query,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(tmp_path)
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_sdk_probe_raises_falls_through_to_bypass(self, tmp_path) -> None:
        """SDK probe raises an unexpected exception (e.g. transient
        I/O error in the SDK itself) → fall through to bypass instead
        of returning REJECTED. The bypass is the orchestrator's last
        chance to determine reachability.
        """
        from orchestratord_clawcodex.session import ClawcodexSession

        sess_dir = tmp_path / "sess-x"
        sess_dir.mkdir()

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            fake_query = _make_sdk_query(raise_exc=RuntimeError("boom"))
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": fake_query,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(tmp_path)
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_sdk_probe_raises_bypass_unavailable_returns_undetectable(self) -> None:
        """SDK probe raises AND bypass is also unavailable → ``UNDETECTABLE``."""
        from orchestratord_clawcodex.session import ClawcodexSession

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            fake_query = _make_sdk_query(raise_exc=RuntimeError("boom"))
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": fake_query,
                    "clawcodex_ext.services.session_storage": None,
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()

    def test_sdk_probe_times_out_falls_through_to_bypass(self, tmp_path) -> None:
        """SDK probe hangs past ``handshake_timeout_s`` → fall through
        to bypass rather than returning UNDETECTABLE.
        """
        import time as _time

        from orchestratord_clawcodex.session import ClawcodexSession

        sess_dir = tmp_path / "sess-x"
        sess_dir.mkdir()

        def _slow_probe(*a, **kw) -> bool:
            # Sleep longer than handshake_timeout_s to force timeout.
            _time.sleep(2.0)
            return True

        spec = SessionSpec(
            cwd="/tmp", resume_session_id="sess-x", handshake_timeout_s=0.1
        )
        session = ClawcodexSession(spec)
        try:
            fake_query = SimpleNamespace(
                QueryRunner=SimpleNamespace(probe_transcript=_slow_probe),
                QueryConfig=lambda **kw: None,
            )
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": fake_query,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(tmp_path)
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.RESUMED
        finally:
            session.close_sync()

    def test_bypass_probe_times_out_returns_undetectable(self, tmp_path) -> None:
        """The bypass itself hangs past ``handshake_timeout_s`` →
        ``UNDETECTABLE``. The orchestrator must never block indefinitely
        even when both the SDK and the bypass are sluggish.
        """
        from orchestratord_clawcodex.session import ClawcodexSession

        import time as _time

        def _slow_resolve() -> Path:
            _time.sleep(2.0)
            return tmp_path

        spec = SessionSpec(
            cwd="/tmp", resume_session_id="sess-x", handshake_timeout_s=0.1
        )
        session = ClawcodexSession(spec)
        try:
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": None,
                    "clawcodex_ext.services.session_storage": SimpleNamespace(
                        resolve_sessions_dir=_slow_resolve,
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.UNDETECTABLE
        finally:
            session.close_sync()

    def test_sdk_preferred_when_both_available(self, tmp_path) -> None:
        """When both paths are usable, the SDK wins. The bypass is a
        fallback — never called if the SDK returned a verdict.
        """
        from orchestratord_clawcodex.session import ClawcodexSession

        # Bypass would say RESUMED (session dir exists), SDK says
        # REJECTED. The probe must return REJECTED — the SDK is
        # authoritative when present.
        sess_dir = tmp_path / "sess-x"
        sess_dir.mkdir()

        spec = SessionSpec(cwd="/tmp", resume_session_id="sess-x")
        session = ClawcodexSession(spec)
        try:
            fake_query = _make_sdk_query(probe_return=False)
            with patch.dict(
                "sys.modules",
                {
                    "extensions.api.query": fake_query,
                    "clawcodex_ext.services.session_storage": (
                        _make_storage_module(tmp_path)
                    ),
                },
            ):
                status = asyncio.run(session.probe_resume())
                assert status is ResumeStatus.REJECTED
        finally:
            session.close_sync()
