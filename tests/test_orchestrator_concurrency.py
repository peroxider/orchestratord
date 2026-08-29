"""Step 2/3/4a/8 — orchestrator concurrency invariants (G1, G2, G4, G17).

Covers the gaps the chapter calls out as the safety contract for
``run_tools`` / ``partition_tool_calls``:

- G1: an exception escaping ``run_tool_use`` in a concurrent batch must
  produce a synthetic ``tool_use_error`` so the next API turn doesn't
  see an unmatched ``tool_use`` block.
- G2: ``classify_concurrency_safe`` is the single fail-closed barrier
  for "is this safe to parallelize?" — must reject unknown tools,
  non-dict input, and exceptions from the per-tool classifier.
- G4: context modifiers from concurrent-safe tools are accepted as
  ``ContextModifier`` dataclasses (the producer's actual shape) and
  applied in tool-submission order after the batch finishes.
- G17: result ordering matches submission order even when tools
  complete out of order.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from src.services.tool_execution.orchestrator import (
    classify_concurrency_safe,
    partition_tool_calls,
    run_tools,
)
from src.services.tool_execution.streaming_executor import ToolUseBlock
from src.tool_system.build_tool import build_tool
from src.tool_system.context import ToolContext, ToolUseOptions
from collections import namedtuple

ToolResult = namedtuple("ToolResult", ["name", "output"])
from src.types.messages import create_assistant_message
from src.utils.abort_controller import AbortController


def _allow_all(_tool, tool_input, _ctx, _msg, _id):
    return {"behavior": "allow", "updatedInput": tool_input}


def _make_context(tools):
    return ToolContext(
        workspace_root=Path("/tmp"),
        options=ToolUseOptions(tools=tools),
        abort_controller=AbortController(),
    )


# ---------------------------------------------------------------------------