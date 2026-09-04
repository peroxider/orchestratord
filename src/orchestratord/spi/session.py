"""SPI: AgentSession — 会话协议（Session Protocol）

An AgentSession represents one running conversation with an agent
backend.  The session's output is exclusively via the `events()`
async iterator — there is no `run()` or `await`-for-completion API.
This split (send + events) is deliberate: multi-agent modes (debate
round-robin, pipeline) need to inject a user message without waiting
for completion.

Resume semantics (DESIGN_graded_timeouts_and_resume.md §2):

The resume outcome is a **three-state** signal (ResumeStatus), not a
bool. The motivation: some backends (dsh, opencode) cannot probe
whether the transcript is still alive on the remote side; this
"unknown" must be expressible instead of being collapsed to False.
Reference: multica ``agent.go:203-231`` and ``agent.go:350-364``.

A backend must declare ``resume_detection`` in its
``BackendCapabilities`` when it can answer the probe at all.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from orchestratord.spi.approval import ApprovalDecision
from orchestratord.spi.capabilities import BackendCapabilities
from orchestratord.spi.events import EventEnvelope


class ResumeStatus(Enum):
    """Resume outcome signal — three states, not two.

    ┌──────────────────┬─────────────────────────────────────────────────┐
    │ RESUMED          │ Transcript is still reachable on the remote side │
    │                  │ — the session can continue with ``send()``.       │
    │ REJECTED         │ Explicit refusal: transcript was GC'd, server    │
    │                  │ quota exhausted, or the operator revoked the      │
    │                  │ session. Orchestrator must NOT attempt to send;  │
    │                  │ treat as terminal.                                │
    │ UNDETECTABLE     │ Backend protocol offers no resume probe at all.  │
    │                  │ Orchestrator may attempt ``send()`` and fall      │
    │                  │ back to ERROR on failure — but it must surface   │
    │                  │ "undetectable" in user-facing diagnostics so the │
    │                  │ operator knows the resume was optimistic.        │
    └──────────────────┴─────────────────────────────────────────────────┘
    """

    RESUMED = "resumed"
    REJECTED = "rejected"
    UNDETECTABLE = "undetectable"


@dataclass
class SessionResult:
    """Terminal outcome of an AgentSession.

    Returned when the consumer has finished draining ``events()`` and
    wants a structured summary. ``status`` is required; the other
    fields are populated based on the kind of outcome.
    """

    status: ResumeStatus
    final_text: str | None = None
    last_event_seq: int | None = None
    resume_target_session_id: str | None = None
    reason: str | None = None
    error_code: str | None = None


@runtime_checkable
class AgentSession(Protocol):
    """A live conversation with a backend agent.

    Output flows exclusively through the `events()` async iterator.
    """

    session_id: str
    # Optional orchestrator-level identity; native ``session_id`` remains
    # the backend resume/debug key.
    conversation_id: str | None
    capabilities: BackendCapabilities

    async def send(self, content: str | list[Any]) -> None:
        """Send one round of user input; results arrive via events()."""
        ...

    def events(self) -> AsyncIterator[EventEnvelope]:
        """Normalized event stream — the sole output channel."""
        ...

    async def interrupt(self) -> None:
        """Interrupt the current turn (capability bit: interrupt)."""
        ...

    async def approve(self, request_id: str, decision: ApprovalDecision) -> None:
        """Respond to a pending approval request (capability bit: approval_hooks)."""
        ...

    async def close(self) -> None:
        """Graceful teardown; process-based backends wind down the worker."""
        ...

    async def probe_resume(self) -> ResumeStatus:
        """Probe whether the session's transcript is still reachable.

        Must be called **before** the first ``send()`` when
        ``SessionSpec.resume_session_id`` is set. The orchestrator
        uses this to decide between three behaviors (RESUMED →
        continue normally; REJECTED → emit a structured
        ``resume_rejected`` event and skip send; UNDETECTABLE →
        attempt send anyway and surface the failure if it happens).

        Backends that cannot answer the probe (e.g. hermes, dsh) must
        return ``ResumeStatus.UNDETECTABLE`` — not silently treat it
        as RESUMED, because the orchestrator relies on the distinction
        for diagnostics (DESIGN §2.3).

        Capability bit: ``BackendCapabilities.resume_detection``. A
        backend whose bit is ``False`` is contractually obligated to
        return ``UNDETECTABLE``.
        """
        ...
