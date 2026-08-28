"""D7e test helpers: bridge old QueryRunner-stub tests to the SPI path.

After D7e, AgentRunner always consumes events via the SPI
AgentSession interface (never directly via QueryRunner).  Tests that
used to patch ``orchestratord.agent_runner.QueryRunner`` need a stub
backend + a pass-through ``_SpiEventAdapter`` so the old stub event
sequences (backend event types) flow through unchanged.

Usage:

    from tests.spi_stub_helpers import spi_stub_backend, spi_passthrough

    class _MyStub:
        def __init__(self, config):
            self.config = config
        async def stream(self):
            yield SessionComplete(reason="success")

    runner = AgentRunner(..., backend=spi_stub_backend(_MyStub))
    with spi_passthrough():
        await runner.run(session)
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import fields
from typing import Any
from unittest.mock import patch

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.capabilities import BackendCapabilities


class _StubConfig:
    """Wrapper that quacks like ``QueryConfig`` for old-style stubs.

    Old stubs expect ``config.prompt``, ``config.env``, etc.
    ``SessionSpec`` carries most fields but not ``prompt`` (which is
    passed via ``send()``).  This wrapper copies all ``SessionSpec``
    fields and adds ``prompt``.
    """

    def __init__(self, spec: SessionSpec, prompt: str) -> None:
        for f in fields(spec):
            setattr(self, f.name, getattr(spec, f.name))
        self.prompt = prompt


class _StubSession:
    """Adapts an old-style stub (``__init__(config)`` + ``.stream()``)
    into the SPI ``AgentSession`` interface."""

    def __init__(self, stub_factory: type, spec: SessionSpec) -> None:
        self._stub_factory = stub_factory
        self._spec = spec
        self._stub: Any = None

    async def send(self, content: str | list[Any]) -> None:
        prompt = content if isinstance(content, str) else str(content)
        config = _StubConfig(self._spec, prompt)
        self._stub = self._stub_factory(config)

    def events(self):
        if self._stub is None:
            raise RuntimeError("send() must be called before events()")
        return self._stub.stream()

    async def interrupt(self) -> None:
        pass

    async def approve(self, request_id: str, decision: Any) -> None:
        pass

    async def close(self) -> None:
        pass


class _StubBackend:
    """SPI AgentBackend that wraps old-style QueryRunner stub classes."""

    name = "stub"
    display_name = "Stub Backend"

    def __init__(self, stub_factory: type) -> None:
        self._stub_factory = stub_factory

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming_deltas=True,
            resumable=False,
            interrupt=False,
            approval_hooks=False,
            parallel_sessions=False,
            cost_reporting=False,
            tool_filtering=False,
            takeover=False,
        )

    def create_session(self, spec: SessionSpec):
        return _StubSession(self._stub_factory, spec)

    def dispose(self) -> None:
        pass


def spi_stub_backend(stub_factory: type) -> _StubBackend:
    """Create a stub backend that wraps *stub_factory*.

    *stub_factory* must be a class with ``__init__(self, config)`` and
    ``async def stream(self)`` yielding clawcodex event types — exactly
    the same contract as the old ``_QueryRunnerStub`` classes.
    """
    return _StubBackend(stub_factory)


class _SpiEventAdapterPassthrough:
    """Test-only replacement for ``_SpiEventAdapter``.

    The real adapter converts ``EventEnvelope`` → clawcodex types.
    Since our stub sessions already yield clawcodex types, this
    pass-through forwards them unchanged.
    """

    def __init__(self, spi_session: Any) -> None:
        self._spi_session = spi_session

    async def stream(self):
        async for event in self._spi_session.events():
            yield event


@contextmanager
def spi_passthrough():
    """Context manager that replaces ``_SpiEventAdapter`` with a pass-through."""
    with patch(
        "orchestratord.agent_runner._SpiEventAdapter",
        _SpiEventAdapterPassthrough,
    ):
        yield
