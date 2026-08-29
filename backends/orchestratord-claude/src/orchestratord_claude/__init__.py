"""orchestratord-claude — Anthropic Claude Code (CLI) backend.

Spawns the ``claude`` CLI (or its open-source fork ``ccb`` /
``claude-code-best``) as a per-turn subprocess and translates its
``stream-json`` output into the orchestratord SPI events.

Resolution order for the executable:

1. ``spec.runtime_bin`` if the workflow explicitly pins a binary path.
2. The first of ``claude`` / ``ccb`` found on ``$PATH`` via
   :func:`shutil.which`.

Capability surface (matches what the upstream ``claude -p`` CLI offers
out of the box): streaming deltas, parallel sessions, cost reporting.
Resumability is wired through ``--resume <session_id>`` but the
resume probe is reported as ``UNDETECTABLE`` because the CLI does
not expose a way to ask "is this session still alive" without
actually resuming it (DESIGN_graded_timeouts_and_resume.md §2.4).
"""

from __future__ import annotations

from orchestratord_claude.backend import ClaudeBackend
from orchestratord_claude.descriptor import CLAUDE_DESCRIPTOR

__all__ = ["ClaudeBackend", "CLAUDE_DESCRIPTOR"]
