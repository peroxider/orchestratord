"""Tests for the AgentBackend single-turn clarifier path.

Per ``DESIGN_decoupling_clawcodex.md`` §2.6: the issue clarifier's LLM
call layer may run through an SPI ``AgentBackend`` session
(``max_turns=1``, ``permission_mode="plan"``) instead of a dedicated
provider.  These tests verify the backend path end-to-end with a fake
SPI backend/session.
"""

from __future__ import annotations

import json
import time

from orchestratord.config.schema import ClarifierConfig
from orchestratord.issue import Issue
from orchestratord.issue_clarifier import ClarifierCache, IssueClarifierService
from orchestratord.spi.events import EventEnvelope, EventKind
from orchestratord.spi.backend import SessionSpec


class _FakeSession:
    """SPI session that echoes a canned clarity-analysis JSON reply."""

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.sent_content: str | None = None
        self.closed = False

    async def send(self, content: str) -> None:
        self.sent_content = content

    async def events(self):
        yield EventEnvelope(
            seq=1,
            timestamp=time.time(),
            kind=EventKind.TEXT,
            payload={"text": self.reply_text},
        )
        yield EventEnvelope(
            seq=2,
            timestamp=time.time(),
            kind=EventKind.SESSION_COMPLETE,
            payload={"reason": "success"},
        )

    async def close(self) -> None:
        self.closed = True


class _FakeBackend:
    """Captures the SessionSpec used to create the analysis session."""

    def __init__(self, reply_text: str) -> None:
        self.reply_text = reply_text
        self.created_spec: SessionSpec | None = None

    def create_session(self, spec: SessionSpec) -> _FakeSession:
        self.created_spec = spec
        return _FakeSession(self.reply_text)


def _service(
    backend: _FakeBackend | None,
    *,
    spec_factory=None,
) -> IssueClarifierService:
    cache = ClarifierCache(
        "/nonexistent/clarifier-cache.json",
        enabled=False,
    )
    return IssueClarifierService(
        config=ClarifierConfig(),
        cache=cache,
        backend=backend,
        backend_spec_factory=spec_factory,
    )


def _issue() -> Issue:
    return Issue(
        id="42",
        identifier="ISSUE-42",
        title="Fix login timeout",
        description="Session cookie expires too early.",
    )


class TestBackendSingleTurn:
    def test_uses_backend_when_injected(self) -> None:
        reply = json.dumps(
            {"is_clear": True, "confidence": 0.95, "ambiguities": []},
            ensure_ascii=False,
        )
        backend = _FakeBackend(reply)
        service = _service(backend)
        result = service.analyze(_issue())

        assert result.is_clear is True
        assert result.confidence == 0.95
        # The backend was actually used (single-turn session created).
        assert backend.created_spec is not None

    def test_session_is_plan_only_single_turn(self) -> None:
        reply = json.dumps({"is_clear": True, "confidence": 0.9, "ambiguities": []})
        backend = _FakeBackend(reply)
        spec_factory = lambda: SessionSpec(  # noqa: E731
            cwd="/tmp/ws",
            max_turns=5,  # must be overridden to 1 by the service
            permission_mode="dontAsk",  # must be overridden to "plan"
        )
        service = _service(backend, spec_factory=spec_factory)
        result = service.analyze(_issue())

        assert result.is_clear is True
        spec = backend.created_spec
        assert spec is not None
        assert spec.max_turns == 1
        assert spec.permission_mode == "plan"
        assert spec.cwd == "/tmp/ws"

    def test_send_receives_the_user_prompt(self) -> None:
        reply = json.dumps({"is_clear": True, "confidence": 0.8, "ambiguities": []})
        backend = _FakeBackend(reply)
        service = _service(backend)
        service.analyze(_issue())

        session = backend.create_session(SessionSpec(cwd="."))
        assert session is not None
        # The last user message (the JSON payload) was sent to the session.
        # Re-analyze with a captured session to inspect the sent content.
        captured: list[str] = []

        class _CaptureBackend(_FakeBackend):
            def create_session(self, spec: SessionSpec) -> _FakeSession:
                self.created_spec = spec
                session = _FakeSession(self.reply_text)
                original_send = session.send

                async def _send_and_capture(content: str) -> None:
                    captured.append(content)
                    await original_send(content)

                session.send = _send_and_capture  # type: ignore[method-assign]
                return session

        service = _service(_CaptureBackend(reply))
        service.analyze(_issue())
        assert captured, "send() must be invoked with the user payload"
        payload = json.loads(captured[0])
        assert payload["title"] == "Fix login timeout"

    def test_session_is_closed_after_analysis(self) -> None:
        reply = json.dumps({"is_clear": True, "confidence": 0.9, "ambiguities": []})
        backend = _FakeBackend(reply)
        service = _service(backend)
        service.analyze(_issue())

        # Verify via a backend that records created sessions.
        created: list[_FakeSession] = []

        class _RecordingBackend(_FakeBackend):
            def create_session(self, spec: SessionSpec) -> _FakeSession:
                self.created_spec = spec
                session = _FakeSession(self.reply_text)
                created.append(session)
                return session

        service = _service(_RecordingBackend(reply))
        service.analyze(_issue())
        assert created and created[0].closed is True

    def test_fails_open_when_backend_missing(self) -> None:
        service = _service(None)
        result = service.analyze(_issue())
        # No backend and no provider → fail-open default.
        assert result.is_clear is True
        assert result.degraded is True

    def test_non_json_reply_fails_open(self) -> None:
        backend = _FakeBackend("not json at all")
        service = _service(backend)
        result = service.analyze(_issue())
        # parser fail-open: degraded to clear when min confidence not met.
        assert result.degraded is True
