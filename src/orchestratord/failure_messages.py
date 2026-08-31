"""User-facing failure messages for the issue-to-PR pipeline.

Each function returns a concise, actionable message following the
"What happened -> Why -> What to do next" structure, suitable for both
IM notifications (WeChat/Slack) and issue tracker comments.

Routing (DESIGN_graded_timeouts_and_resume.md §4):

Failure messages are now routed by ``ResumeStatus`` + ``error_code``
rather than a single ``timeout_ms`` field. The 5-level timeout
classification produces distinct error codes:

    * "handshake_timeout"     — agent didn't produce first event in time
    * "first_turn_timeout"    — agent started but no first TURN_COMPLETE
    * "inactivity_timeout"    — gap between token emissions exceeded
    * "idle_watchdog_timeout" — no events at all for too long
    * "total_timeout"         — run exceeded total wall budget

Combined with the three-state ``ResumeStatus`` we get a 3 × 5 = 15
matrix of distinct user-facing messages, of which 12 are reachable
(``RESUMED`` after a successful resume is not a failure path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestratord.spi.session import ResumeStatus


@dataclass
class FailureContext:
    """Context for generating user-friendly failure messages."""

    timeout_ms: int = 0
    max_turns: int = 0
    attempt: int = 0
    max_attempts: int = 0
    retry_delay_ms: int = 0
    error_detail: str = ""
    hook_name: str = ""
    session_end_summary: str = ""


def _retry_hint(ctx: FailureContext) -> str:
    """Build a retry hint suffix based on attempt/max_attempts context."""
    if ctx.max_attempts and ctx.attempt >= ctx.max_attempts:
        return "已达最大重试次数，请手动检查 issue 后使用 `agent:retry` 标签重新触发"
    if ctx.retry_delay_ms > 0:
        delay_s = ctx.retry_delay_ms / 1000
        return f"系统将在 {delay_s:.0f} 秒后自动重试（第 {ctx.attempt + 1} 次），无需手动干预"
    if ctx.attempt > 0:
        return f"系统将自动重试（第 {ctx.attempt + 1} 次），无需手动干预"
    return "系统将自动重试，无需手动干预"


def timeout_message(ctx: FailureContext) -> str:
    """Agent run exceeded the configured timeout."""
    timeout_s = ctx.timeout_ms / 1000 if ctx.timeout_ms else 0
    retry = _retry_hint(ctx)
    parts = [f"任务执行超时（{timeout_s:.0f} 秒），Agent 未能在规定时间内完成修复"]
    if ctx.session_end_summary:
        parts.append(f"最后状态：{ctx.session_end_summary[:100]}")
    parts.append(
        "可能原因：问题过于复杂或代码库过大导致 Agent 探索耗时过长。"
        "建议：将 issue 拆分为更小的子任务，或在描述中提供精确的文件路径和修改指引。"
    )
    if retry:
        parts.append(retry)
    return " ".join(parts)


def max_turns_message(ctx: FailureContext) -> str:
    """Agent exceeded the maximum turn limit."""
    retry = _retry_hint(ctx)
    parts = ["Agent 达到最大对话轮次限制，对话轮次消耗过多"]
    if ctx.max_turns:
        parts.append(f"（上限 {ctx.max_turns} 轮）")
    parts.append(
        "可能原因：问题复杂度过高或 Agent 在探索代码库时迷失方向。"
        "建议：检查 issue 描述是否足够清晰，提供更具体的文件路径和修改指引。"
    )
    if retry:
        parts.append(retry)
    return " ".join(parts)


def rate_limit_message(ctx: FailureContext) -> str:
    """API rate limit circuit breaker tripped."""
    retry = _retry_hint(ctx)
    parts = [
        "AI 服务暂时不可用（API 速率限制），短时间内请求过于频繁，已触发限流保护。"
    ]
    if retry:
        parts.append(retry)
    parts.append("如持续出现此问题，请联系管理员检查 API 配额。")
    return " ".join(parts)


def stagnation_message(ctx: FailureContext) -> str:
    """Agent stopped making progress (consecutive no-op turns)."""
    parts = ["Agent 执行停滞，连续多轮未产生有效进展"]
    if ctx.session_end_summary:
        parts.append(f"（{ctx.session_end_summary[:100]}）")
    parts.append(
        "Agent 可能在某个步骤陷入困境或无法找到解决方案。"
        "建议：检查 issue 描述是否准确，确认问题在当前代码库中确实可修复；"
        "修正后使用 `agent:retry` 标签重新触发。"
    )
    return " ".join(parts)


def loop_detected_message(ctx: FailureContext) -> str:
    """Agent detected repeating tool-call patterns."""
    parts = ["Agent 检测到重复操作循环，可能陷入了死循环"]
    if ctx.session_end_summary:
        parts.append(f"（{ctx.session_end_summary[:100]}）")
    parts.append(
        "建议：检查 issue 指令是否包含了导致循环的逻辑；"
        "修改 issue 描述后使用 `agent:retry` 标签重新触发。"
    )
    return " ".join(parts)


def no_changes_message(ctx: FailureContext) -> str:  # noqa: ARG001
    """Agent reported completed but produced no file changes."""
    return (
        "Agent 报告任务完成但未产生任何代码变更。"
        "Agent 可能误判了任务完成状态，或修改未正确保存。"
        "建议：确认 issue 描述的问题确实需要代码修改；"
        "使用 `agent:retry` 标签重新触发。"
    )


def empty_branch_message(ctx: FailureContext) -> str:  # noqa: ARG001
    """Agent produced no reviewable commits."""
    return (
        "Agent 未产生任何代码提交，未创建 PR。"
        "Agent 执行过程中未生成可审查的代码变更。"
        "建议：确认 issue 描述清晰且可执行；检查仓库权限是否正确；"
        "使用 `agent:retry` 标签重新触发。"
    )


def verification_failed_message(ctx: FailureContext) -> str:
    """Pre-commit or pre-push verification/hook failed."""
    parts = []
    if ctx.hook_name:
        parts.append(f"代码检查失败（{ctx.hook_name}）")
    else:
        parts.append("代码验证失败")
    if ctx.error_detail:
        detail = ctx.error_detail[:200]
        parts.append(f"错误详情：{detail}")
    parts.append(
        "建议：根据上述错误信息修复代码问题；"
        "或调整 CI/钩子配置后使用 `agent:retry` 标签重新触发。"
    )
    return " ".join(parts)


def post_commit_failed_message(ctx: FailureContext) -> str:
    """Post-commit sync steps failed (e.g., push, PR creation)."""
    parts = ["代码提交后处理失败，commit 已创建但后续步骤出错"]
    if ctx.error_detail:
        detail = ctx.error_detail[:200]
        parts.append(f"错误详情：{detail}")
    parts.append(
        "建议：检查 git 推送权限和 CI 配置；修复后使用 `agent:retry` 标签重新触发。"
    )
    return " ".join(parts)


def generic_failure_message(ctx: FailureContext) -> str:
    """Unclassified failure during agent execution."""
    parts = ["任务执行过程中发生异常"]
    if ctx.error_detail:
        detail = ctx.error_detail[:200]
        parts.append(f"错误详情：{detail}")
    parts.append(
        "建议：查看运行日志确认失败原因；"
        "修正 issue 描述或配置后使用 `agent:retry` 标签重新触发；"
        "如问题持续，请联系管理员。"
    )
    retry = _retry_hint(ctx)
    if retry:
        parts.append(retry)
    return " ".join(parts)


def premise_not_met_guidance(reason: str) -> str:
    """Additional guidance for 'cannot proceed' issue comments."""
    guidance_map = {
        "file_not_found": (
            "请检查 issue 中引用的文件路径是否正确，"
            "确认文件在仓库中确实存在。"
        ),
        "no_repro": (
            "请提供可复现的具体步骤，"
            "包括输入数据、操作序列和期望结果。"
        ),
        "not_a_bug": (
            "请确认 issue 描述的行为是否为预期设计，"
            "如确为 bug 请补充更多细节。"
        ),
        "insufficient_context": (
            "请补充更多上下文信息，"
            "包括相关代码片段、错误日志和环境信息。"
        ),
        "cannot_proceed": (
            "请检查 issue 描述是否准确完整，"
            "提供具体的文件路径、复现步骤和期望行为。"
        ),
    }
    return guidance_map.get(
        reason,
        "请检查 issue 描述是否准确完整，提供更多上下文信息后重新触发。",
    )


def issue_summary_guidance(status: str) -> str:
    """Return next-step guidance for the issue summary comment based on status."""
    guidance_map: dict[str, str] = {
        "failed": (
            "任务执行失败。请检查上方错误信息，修正 issue 描述后"
            "使用 `agent:retry` 标签重新触发。"
        ),
        "agent_timeout": (
            "任务执行超时。建议拆分 issue 为更小的子任务，"
            "或在描述中提供精确的文件路径。系统将自动重试。"
        ),
        "max_turns_exceeded": (
            "对话轮次超限。建议提供更具体的文件路径和修改指引。"
            "系统将自动重试。"
        ),
        "verification_failed": (
            "代码验证失败。请检查 CI 配置和代码质量问题，"
            "修复后使用 `agent:retry` 标签重新触发。"
        ),
        "stagnation": (
            "Agent 执行停滞。请检查 issue 描述是否准确，"
            "确认问题确实可修复后，使用 `agent:retry` 标签重新触发。"
        ),
        "loop_detected": (
            "检测到操作循环。请检查 issue 指令是否导致循环，"
            "修改后使用 `agent:retry` 标签重新触发。"
        ),
        "rate_limit_circuit_open": (
            "API 速率限制触发熔断。系统将在冷却后自动重试，"
            "无需手动干预。如持续出现请联系管理员。"
        ),
        "cancelled": (
            "任务已被手动取消。如需重新执行，"
            "请使用 `agent:retry` 标签触发。"
        ),
        "before_run_failed": (
            "任务启动前检查失败。请检查工作流配置和仓库权限，"
            "修正后使用 `agent:retry` 标签重新触发。"
        ),
    }
    return guidance_map.get(
        status,
        "请检查上方错误信息，修正后使用 `agent:retry` 标签重新触发。"
        "如问题持续，请联系管理员。",
    )


# ---------------------------------------------------------------------------
# User-facing failure guidance keyed by session_end_reason
# ---------------------------------------------------------------------------

_END_REASON_USER_GUIDANCE: dict[str, tuple[str, str]] = {
    "read_only_loop": (
        "Agent 只读探索未产生代码修改",
        "请检查 issue 描述是否清晰（补充具体实现要求）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "no_changes_produced": (
        "Agent 未产生任何文件修改",
        "请检查 issue 描述是否清晰（补充具体改动要求）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "rate_limited": (
        "请求频率超限（限流）",
        "请检查模型 API 额度/限流是否正常（余额、每分钟请求数上限）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "stagnation": (
        "处理停滞（模型响应慢或无输出）",
        "请检查网络连接与模型服务状态是否正常（是否过载/超时）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "llm_gave_up": (
        "模型判断无法完成该任务",
        "请检查 issue 信息是否充分（补充上下文或拆分需求）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "budget_exhausted": (
        "轮次/预算耗尽",
        "请检查 workflow.md 轮次/预算配置是否合理（调大或拆分任务）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "loop_detected": (
        "Agent 重复循环无进展",
        "请检查 issue 上下文是否充分（补充信息）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "megaturn_workspace_idle": (
        "长时间无工作区变化",
        "请检查模型服务与网络是否正常；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "noop_completed": (
        "无文件变化但被判定为完成",
        "请确认需求是否已满足；若未满足可评论 `/agent follow-up` 继续处理。",
    ),
    "empty_branch_no_commits": (
        "分支为空，未产生任何提交",
        "请检查 issue 描述是否清晰（补充具体实现要求）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "not_reproducible": (
        "问题无法复现",
        "请检查 issue 描述是否清晰（补充复现步骤）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "premise_not_met": (
        "前提条件不满足",
        "请检查前置条件是否满足（环境/依赖/前置状态）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "operator_stopped": ("被操作者手动停止", "无需处理（人工主动停止）。"),
    "operator_stop": ("被操作者手动停止", "无需处理（人工主动停止）。"),
    "operator_takeover": ("被操作者接管", "无需处理（人工主动接管）。"),
    "task_complete": ("任务正常完成", "无需处理。"),
    "already_completed": ("该 issue 已处理完成", "无需处理。"),
    "agent_timeout": (
        "Agent 执行超时",
        "请检查模型服务/网络是否正常，或拆分任务降低规模；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "max_turns_exceeded": (
        "轮次上限超限",
        "请检查 workflow.md 轮次上限配置是否合理（调大或拆分任务）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "rate_limit_circuit_open": (
        "限流熔断（连续请求被限）",
        "请检查模型 API 额度/限流是否正常（余额、并发限制）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "verification_failed": (
        "本地验证未通过",
        "请检查测试命令与环境配置是否正确（test_command、依赖）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "before_run_failed": (
        "运行前失败（环境/配置问题）",
        "请检查环境与配置是否正确（token 权限、workflow 配置、网络）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
    ),
    "cancelled": ("被取消", "无需处理（人工取消或系统取消）。"),
}


def end_reason_guidance(
    end_reason: str | None,
    hook_error: str | None = None,
    end_summary: str | None = None,
    output_text: str | None = None,
) -> tuple[str, str] | None:
    """Return (readable_reason, suggested_action) for a session end reason.

    Handles dynamic reasons (exit_code=N, salvaged_after_*) and
    unknown reasons with a generic fallback.
    """
    import re
    if not end_reason:
        return None
    guidance = _END_REASON_USER_GUIDANCE.get(end_reason)
    if guidance is None and end_reason.startswith("exit_code="):
        code = end_reason.split("=", 1)[1]
        _err_blob = (
            f"{hook_error or ''} {end_summary or ''} "
            f"{(output_text or '')[:2000]}"
        )
        if re.search(
            r"401|Invalid Authentication|invalid_authentication|"
            r"Unauthorized|API key|authentication error",
            _err_blob,
            re.IGNORECASE,
        ):
            guidance = (
                f"模型 API 认证失败（退出码 {code}）",
                "请检查模型 API key 是否正确/有效（provider 的 api_key 配置）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
            )
        else:
            guidance = (
                f"工具/命令执行失败（退出码 {code}）",
                f"请检查对应命令/环境是否正确（退出码 {code}）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
            )
    elif guidance is None and end_reason.startswith("salvaged_after_"):
        guidance = (
            "恢复处理失败",
            "请检查环境与配置是否正常；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry。",
        )
    elif guidance is None:
        guidance = (
            "未知错误",
            "请检查环境与配置是否正确（token 权限、workflow 配置、网络）；编排器会自动重试，如需立即重试可在 issue 评论 /agent retry，仍失败请附运行日志（含上方 `Run` 的 run_id）。",
        )
    return guidance


# ---------------------------------------------------------------------------
# ResumeStatus + error_code routing (DESIGN §4)
# ---------------------------------------------------------------------------


# Recognized error codes that trigger the per-phase timeout messages.
_TIMEOUT_ERROR_CODES: frozenset[str] = frozenset({
    "handshake_timeout",
    "first_turn_timeout",
    "inactivity_timeout",
    "idle_watchdog_timeout",
    "total_timeout",
})


def build_failure_message(
    *,
    status: "ResumeStatus",
    error_code: str | None,
    timeout_seconds: float | None = None,
    retry_hint: str = "",
    extra_detail: str = "",
) -> str:
    """Compose a user-facing failure message from resume status + error code.

    Routing priority (DESIGN §4.2):
      1. ``ResumeStatus.REJECTED``  → "transcript 已被 GC / 不支持跨进程恢复"
      2. UNDETECTABLE + handshake_timeout → "agent 未能在 X 秒内启动"
      3. UNDETECTABLE + first_turn_timeout → "agent 启动了但首轮未在 X 秒完成"
      4. UNDETECTABLE + inactivity_timeout → "X 秒无 token 输出"
      5. UNDETECTABLE + idle_watchdog_timeout → "X 秒无任何事件"
      6. UNDETECTABLE + total_timeout → "整轮超过 X 秒"
      7. 兜底 → "未知失败"

    ``timeout_seconds`` is rendered as "X 秒" if provided. ``retry_hint``
    is appended verbatim if non-empty. ``extra_detail`` is appended
    for further context (e.g. raw exception text).
    """
    from orchestratord.spi.session import ResumeStatus as _RS

    secs_text = f"{timeout_seconds:.0f} 秒" if timeout_seconds else "未知时长"

    if status is _RS.REJECTED:
        base = (
            f"恢复目标 session 已被拒绝（{error_code or 'unsupported'}）。"
            "后端明确拒绝 resume — 通常意味着 transcript 已被 GC、配额已满，"
            "或该 backend 明确不支持跨进程 resume。"
        )
    elif status is _RS.UNDETECTABLE:
        if error_code == "handshake_timeout":
            base = f"Agent 未能在 {secs_text} 内完成启动握手。"
        elif error_code == "first_turn_timeout":
            base = f"Agent 启动了，但首个 turn 未在 {secs_text} 内完成。"
        elif error_code == "inactivity_timeout":
            base = f"Agent 连续 {secs_text} 未输出新 token，可能陷入长时间思考或工具调用死锁。"
        elif error_code == "idle_watchdog_timeout":
            base = f"Agent session 已 {secs_text} 无任何事件，疑似连接断开或后端崩溃。"
        elif error_code == "total_timeout":
            base = f"任务整轮执行超过 {secs_text}，触发总超时保护。"
        elif error_code in _TIMEOUT_ERROR_CODES:
            # Should not happen, but defensively handle unknown codes in
            # the timeout set without raising.
            base = f"任务执行超时（{secs_text}）。"
        else:
            base = "任务执行过程中发生未知失败。"
    else:
        # RESUMED → not a failure path; return a benign acknowledgment so
        # callers that always invoke this helper still get a string.
        base = "Resume 成功 — session 已恢复。"

    parts = [base]
    if extra_detail:
        parts.append(f"详情：{extra_detail[:200]}")
    if retry_hint:
        parts.append(retry_hint)
    return " ".join(parts)


def resume_status_message(
    *,
    status: "ResumeStatus",
    error_code: str | None = None,
    retry_hint: str = "",
) -> str:
    """Shorthand: route only on resume status (no error_code).

    Convenience wrapper for callers that only need the status branch.
    """
    return build_failure_message(
        status=status,
        error_code=error_code,
        retry_hint=retry_hint,
    )