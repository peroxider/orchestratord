"""Prompt render snapshot test — DESIGN_ORCHESTRATION_BUSINESS_DECOUPLING.md P2 验收.

对同一 AgentTask/Issue 输入，渲染结果必须与拆分前捕获的金样
（tests/goldens/prompt_snapshot.json，由 scripts/capture_prompt_goldens.py
在重构前生成）byte-identical。业务模板迁移（prompt_builder →
applications.issue_pr.prompts / kernel.prompt_core）不得改变任何渲染输出。
"""

from __future__ import annotations

import json
from pathlib import Path

import orchestratord.applications.issue_pr.prompts  # noqa: F401  (registers business profiles/hooks)
from scripts.capture_prompt_goldens import GOLDEN_PATH, build_cases


def test_prompt_render_matches_p2_goldens(monkeypatch) -> None:
    assert GOLDEN_PATH.exists(), f"goldens missing: {GOLDEN_PATH} — regenerate with --write"
    expected = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    # 与捕获脚本 main() 相同的机器路径隔离：金样中不含绝对路径。
    import orchestratord.prompt_builder as pb

    monkeypatch.setattr(pb, "SESSIONS_DIR", Path("sessions-home"))

    outputs = {name: fn() for name, fn in build_cases().items()}
    assert set(outputs) == set(expected), (
        f"case matrix drifted: missing={set(expected) - set(outputs)} "
        f"extra={set(outputs) - set(expected)}"
    )
    for name in sorted(expected):
        assert outputs[name] == expected[name], (
            f"prompt render drifted for case {name!r} — prompt templates must stay "
            f"byte-identical through the P2 split (DESIGN §7 P2)"
        )


def test_business_profiles_registered_into_kernel_router() -> None:
    from orchestratord.kernel.prompt_core import get_prompt_router

    router = get_prompt_router()
    # issue profile registered by applications.issue_pr.prompts import.
    assert router.profile_template("issue") is not None
    # premise warning hook mounted exactly once (idempotent registration).
    from orchestratord.applications.issue_pr.prompts import _premise_warning_hook

    assert router.post_render_hooks.count(_premise_warning_hook) == 1
