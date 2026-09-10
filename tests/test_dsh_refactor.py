"""Regression tests for the dsh backend dedup refactoring (issue #30).

ST3–ST5 extracted duplicated blocks into helpers; each test below
pins a helper's contract so a future re-inline (or divergent drift
between the two original call sites) fails loudly:

* ST3 — ``DshSession._generate_cordis`` owns the four-parameter group
  (``cwd/.reports``, ``run_id``, ``environ``, ``approval_policy``)
  shared by the custom-provider-route and approval-policy-only code
  paths in ``_default_harness_factory``.
* ST4 — ``DshSession._emit_error`` owns the ERROR envelope shape
  shared by the harness-init (``dsh_init_error``) and harness-run
  (``dsh_error``) except blocks in ``_run_turn``.
* ST5 — ``cordis_gen._dump_block`` owns the stable YAML dump options
  (``sort_keys=False, allow_unicode=True, default_flow_style=False``)
  shared by ``build_approval_block`` and ``build_cordis_text``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from orchestratord_dsh.session import DshSession

from orchestratord.spi.backend import SessionSpec
from orchestratord.spi.events import EventKind

cordis_gen = pytest.importorskip(
    "orchestratord_dsh.cordis_gen",
    reason="orchestratord-dsh not installed in this environment",
)


# ---------------------------------------------------------------------------
# ST3: _generate_cordis helper
# ---------------------------------------------------------------------------


def test_generate_cordis_helper_writes_reports_file_with_run_stem(
    tmp_path,
) -> None:
    """ST3: the extracted helper keeps the cwd/.reports + run_id contract
    of the inline ``generate_cordis_file`` calls it replaced."""
    session = DshSession(SessionSpec(cwd=str(tmp_path), run_id="stage-01-feedface"))
    path = session._generate_cordis(
        {
            "gw": {
                "api": "openai-completions",
                "base_url": "https://gw.example/v1",
                "models": ["m1"],
            }
        },
        {},
        None,
    )
    assert path.startswith(str(tmp_path / ".reports"))
    assert "dsh-cordis-stage-01-feedface.yml" in path
    text = Path(path).read_text(encoding="utf-8")
    assert "id: llm-pi-ai" in text
    assert "apiKeyEnv" not in text  # keyless route: no apiKeyEnv emitted


def test_generate_cordis_helper_approval_policy_only(tmp_path) -> None:
    """ST3: the approval-policy-only code path (``providers or None``)
    still routes through the helper and emits only the approval block."""
    session = DshSession(SessionSpec(cwd=str(tmp_path), run_id="r1"))
    path = session._generate_cordis(None, {}, "never")
    assert path.startswith(str(tmp_path / ".reports"))
    text = Path(path).read_text(encoding="utf-8")
    assert "policy: never" in text
    assert "dsh-llm-pi-ai" not in text


# ---------------------------------------------------------------------------
# ST4: _emit_error helper
# ---------------------------------------------------------------------------


def test_emit_error_sets_last_error_and_envelope() -> None:
    """ST4: the extracted helper preserves the exact envelope the two
    inline except blocks used to build (code + 'Type: message')."""
    session = DshSession(SessionSpec(cwd="/tmp"))

    async def main() -> asyncio.Queue:
        session._loop = asyncio.get_running_loop()
        session._emit_error("dsh_error", RuntimeError("rate limit"))
        envelope = await asyncio.wait_for(session._queue.get(), timeout=1.0)
        return envelope

    envelope = asyncio.run(main())
    assert session._last_error_message == "RuntimeError: rate limit"
    assert envelope.kind is EventKind.ERROR
    assert envelope.payload == {
        "code": "dsh_error",
        "message": "RuntimeError: rate limit",
    }


def test_emit_error_supports_both_codes() -> None:
    """ST4: both call-site codes (dsh_init_error / dsh_error) flow
    through the same helper — the code is a parameter, not a fork."""
    session = DshSession(SessionSpec(cwd="/tmp"))

    async def main() -> list:
        session._loop = asyncio.get_running_loop()
        session._emit_error("dsh_init_error", ValueError("unknown model"))
        session._emit_error("dsh_error", TimeoutError("turn timed out"))
        out = []
        for _ in range(2):
            envelope = await asyncio.wait_for(session._queue.get(), timeout=1.0)
            out.append(envelope.payload)
        return out

    payloads = asyncio.run(main())
    assert payloads[0] == {
        "code": "dsh_init_error",
        "message": "ValueError: unknown model",
    }
    assert payloads[1] == {
        "code": "dsh_error",
        "message": "TimeoutError: turn timed out",
    }


# ---------------------------------------------------------------------------
# ST5: _dump_block helper
# ---------------------------------------------------------------------------


def test_dump_block_matches_safe_dump_options() -> None:
    """ST5: ``_dump_block`` must produce byte-identical YAML to the
    ``yaml.safe_dump`` options the two call sites used to repeat."""
    import yaml

    block = [
        {
            "id": "approval",
            "name": "@deepseek-ai/dsh-user-approval",
            "config": {"policy": "never"},
        }
    ]
    expected = yaml.safe_dump(
        block, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    assert cordis_gen._dump_block(block) == expected


def test_build_cordis_text_providers_and_approval_output_stable() -> None:
    """ST5: routing both plugin blocks through ``_dump_block`` must not
    change the composed text (byte-level shape preserved)."""
    text = cordis_gen.build_cordis_text(
        {
            "gw": {
                "api": "openai-completions",
                "base_url": "https://gw.example/v1",
                "api_key": "sk-literal",
                "models": ["m1"],
            }
        },
        approval_policy="never",
    )
    assert "id: llm-pi-ai" in text
    assert "id: approval" in text
    assert "policy: never" in text
    assert "sk-literal" not in text  # credentials never land in the text
    # The marker line is the header; the two blocks follow in order.
    assert text.index("llm-pi-ai") < text.index("approval")
