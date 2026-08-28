"""SPI: BackendCapabilities — 能力协商（Capability Negotiation）

Each backend reports its capabilities; the orchestration core enforces
degradation paths for any capability the backend does not support.

Backend implementations MUST NOT degrade on their own — the core handles
all degradation paths uniformly.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCapabilities:
    """Capability bits reported by an AgentBackend.

    All fields default to False.  A backend sets only the bits it
    actually supports.  The orchestration core reads these bits to
    decide which degradation paths to activate (§4.3).

    Degradation rules (enforced by the core, NOT by backends):
    ┌─────────────────────────┬──────────────────────────────────────┐
    │ streaming_deltas=False  │ core splits whole `text` into pseudo- │
    │                         │ deltas so consumers always see deltas │
    │ resumable=False         │ core replays session log as prompt    │
    │ interrupt=False         │ core marks "abandoned", discards on   │
    │                         │ turn-complete (best-effort)           │
    │ approval_hooks=False    │ core pre-filters dangerous tools via  │
    │                         │ tool_filtering + post-hoc audit       │
    │ tool_filtering=False    │ core DENYs unauthorized tools in      │
    │                         │ approval callback (if approval_hooks) │
    │ cost_reporting=False    │ core uses token estimator             │
    │ goal_mode=False         │ core falls back to swarm/coordinator  │
    │                         │ decomposition for complex issues      │
    └─────────────────────────┴──────────────────────────────────────┘
    """

    streaming_deltas: bool = False
    resumable: bool = False
    interrupt: bool = False
    approval_hooks: bool = False
    parallel_sessions: bool = False
    cost_reporting: bool = False
    tool_filtering: bool = False
    takeover: bool = False
    goal_mode: bool = False
    # resume_detection (DESIGN_graded_timeouts_and_resume.md §2.4, ADR-003):
    # True iff the backend can answer probe_resume() with a meaningful
    # RESUMED / REJECTED verdict. False means the backend MUST return
    # ResumeStatus.UNDETECTABLE.  Note: ``resumable`` is the orthogonal
    # "can I create a session that resumes" bit; ``resume_detection`` is
    # the "can I tell whether the remote still has the transcript" bit.
    resume_detection: bool = False
    # resume_detection (DESIGN_graded_timeouts_and_resume.md §2.4, ADR-003):
    # True iff the backend can answer probe_resume() with a meaningful
    # RESUMED / REJECTED verdict. False means the backend MUST return
    # ResumeStatus.UNDETECTABLE.  Note: ``resumable`` is the orthogonal
    # "can I create a session that resumes" bit; ``resume_detection`` is
    # the "can I tell whether the remote still has the transcript" bit.
    resume_detection: bool = False