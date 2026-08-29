"""SPI degradation paths — enforced by the core, not by backends.

Backends report what they have; the core enforces uniform degradation
for any capability bit the backend does not support.  This module
implements the ``streaming_deltas=False`` degradation path documented
in ``capabilities.py``: whole ``TEXT`` events are split into pseudo
``TEXT_DELTA`` events so all downstream consumers only face one text
event kind.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from orchestratord.spi.events import EventEnvelope, EventKind

if TYPE_CHECKING:
    from orchestratord.spi.backend import AgentBackend, SessionSpec
    from orchestratord.spi.session import AgentSession

_CHUNK_CHARS = 200


def _split_chunks(text: str, chunk_size: int = _CHUNK_CHARS) -> list[str]:
    """Split *text* into chunks of roughly *chunk_size* characters.

    Splits at newline boundaries first; lines longer than *chunk_size*
    are hard-cut at the limit.  Chunk concatenation always reconstructs
    *text* exactly (including blank lines and whitespace).
    """
    if not text or not text.strip():
        return []
    chunks: list[str] = []
    # ``splitlines(keepends=True)`` makes each line independently visible
    # while retaining separators.  A final unterminated line is included.
    for line in text.splitlines(keepends=True):
        while len(line) > chunk_size:
            chunks.append(line[:chunk_size])
            line = line[chunk_size:]
        if line:
            chunks.append(line)
    return chunks


class DegradingSession:
    """Session wrapper that splits ``TEXT`` events into pseudo ``TEXT_DELTA``.

    For sessions whose backend reports ``streaming_deltas=False``, the
    core SPI degradation path converts whole ``TEXT`` events into
    multiple ``TEXT_DELTA`` events.  Sessions that already emit
    ``TEXT_DELTA`` are transparently passed through (only ``seq`` is
    re-numbered for total-order consistency).

    All ``seq`` values are re-assigned to guarantee monotonic increase
    across the wrapped stream, preserving the snapshot-tolerant
    contract of ``EventEnvelope``.
    """

    def __init__(self, inner: "AgentSession") -> None:
        self._inner = inner
        self._seq = 0

    @property
    def session_id(self) -> str:
        return self._inner.session_id

    @property
    def capabilities(self):
        return self._inner.capabilities

    async def send(self, content):
        await self._inner.send(content)

    async def probe_resume(self):
        """Forward the resume probe to the wrapped session.

        ``DegradingSession`` exists to normalize event shapes for
        backends that lack ``streaming_deltas``. Resume is orthogonal
        to event shaping; the probe is delegated verbatim so the
        orchestrator's three-state resume decision (RESUMED /
        REJECTED / UNDETECTABLE) is preserved through the wrapper.
        """
        probe = getattr(self._inner, "probe_resume", None)
        if probe is None:
            from orchestratord.spi.session import ResumeStatus
            return ResumeStatus.UNDETECTABLE
        return await probe()

    async def interrupt(self):
        await self._inner.interrupt()

    async def approve(self, request_id, decision):
        await self._inner.approve(request_id, decision)

    async def close(self):
        await self._inner.close()

    def _reseq(self, env: EventEnvelope, kind: EventKind, payload: dict) -> EventEnvelope:
        self._seq += 1
        return replace(env, seq=self._seq, kind=kind, payload=payload)

    async def events(self):
        async for env in self._inner.events():
            if env.kind is EventKind.TEXT:
                text = str(env.payload.get("text", ""))
                for chunk in _split_chunks(text, _CHUNK_CHARS):
                    yield self._reseq(env, EventKind.TEXT_DELTA,
                                      {"text": chunk, "delta": chunk})
            else:
                self._seq += 1
                yield replace(env, seq=self._seq)


class DegradingBackend:
    """Backend wrapper that applies ``DegradingSession`` to every session.

    ``capabilities`` are passed through unchanged — the degradation
    is applied at the session level, not the capability-advertisement
    level.  Downstream code that reads ``capabilities.streaming_deltas``
    still sees the backend's original value; the core enforcement is
    that all consumers get ``TEXT_DELTA`` events.
    """

    def __init__(self, inner: "AgentBackend") -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def display_name(self) -> str:
        return self._inner.display_name

    def capabilities(self):
        return self._inner.capabilities()

    def preflight(self, spec: Any) -> None:
        """Forward to the inner backend when it supports preflight.

        Legacy backends without ``preflight`` are treated as always
        ready (no-op) so wrapping never breaks them.
        """
        preflight = getattr(self._inner, "preflight", None)
        if callable(preflight):
            preflight(spec)

    def create_session(self, spec: Any) -> "DegradingSession":
        """Create a session wrapped in :class:`DegradingSession`.

        ``capabilities`` are passed through unchanged — the degradation
        is applied at the session level, not the capability level.
        """
        return DegradingSession(self._inner.create_session(spec))

    def get_task_registry(self) -> Any | None:
        getter = getattr(self._inner, "get_task_registry", None)
        return getter() if callable(getter) else None

    def dispose(self) -> None:
        dispose = getattr(self._inner, "dispose", None)
        if callable(dispose):
            dispose()
