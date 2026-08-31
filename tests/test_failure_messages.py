"""Tests for failure_messages module — user-facing error message generation."""

from __future__ import annotations

import pytest

from orchestratord.failure_messages import (
    FailureContext,
    build_failure_message,
    empty_branch_message,
    generic_failure_message,
    issue_summary_guidance,
    loop_detected_message,
    max_turns_message,
    no_changes_message,
    post_commit_failed_message,
    premise_not_met_guidance,
    rate_limit_message,
    resume_status_message,
    stagnation_message,
    timeout_message,
    verification_failed_message,
)
from orchestratord.spi.session import ResumeStatus


class TestFailureContext:
    def test_defaults(self) -> None:
        ctx = FailureContext()
        assert ctx.timeout_ms == 0
        assert ctx.max_turns == 0
        assert ctx.attempt == 0
        assert ctx.max_attempts == 0
        assert ctx.retry_delay_ms == 0
        assert ctx.error_detail == ""
        assert ctx.hook_name == ""
        assert ctx.session_end_summary == ""

    def test_partial_init(self) -> None:
        ctx = FailureContext(timeout_ms=300000, attempt=2, error_detail="something went wrong")
        assert ctx.timeout_ms == 300000
        assert ctx.attempt == 2
        assert ctx.error_detail == "something went wrong"
        assert ctx.max_turns == 0


class TestTimeoutMessage:
    def test_basic(self) -> None:
        ctx = FailureContext(timeout_ms=300000)
        msg = timeout_message(ctx)
        assert "300 秒" in msg
        assert "超时" in msg
        assert "建议" in msg

    def test_with_retry(self) -> None:
        ctx = FailureContext(timeout_ms=60000, attempt=1, max_attempts=3, retry_delay_ms=20000)
        msg = timeout_message(ctx)
        assert "60 秒" in msg
        assert "自动重试" in msg

    def test_max_retries_reached(self) -> None:
        ctx = FailureContext(timeout_ms=60000, attempt=3, max_attempts=3)
        msg = timeout_message(ctx)
        assert "已达最大重试次数" in msg

    def test_no_timeout(self) -> None:
        ctx = FailureContext(timeout_ms=0)
        msg = timeout_message(ctx)
        assert "0 秒" in msg


class TestMaxTurnsMessage:
    def test_basic(self) -> None:
        ctx = FailureContext(max_turns=50)
        msg = max_turns_message(ctx)
        assert "最大对话轮次" in msg
        assert "建议" in msg

    def test_with_turn_limit(self) -> None:
        ctx = FailureContext(max_turns=100)
        msg = max_turns_message(ctx)
        assert "100 轮" in msg


class TestRateLimitMessage:
    def test_basic(self) -> None:
        ctx = FailureContext()
        msg = rate_limit_message(ctx)
        assert "速率限制" in msg
        assert "API 配额" in msg

    def test_with_retry(self) -> None:
        ctx = FailureContext(attempt=1, max_attempts=5, retry_delay_ms=30000)
        msg = rate_limit_message(ctx)
        assert "自动重试" in msg


class TestStagnationMessage:
    def test_basic(self) -> None:
        ctx = FailureContext()
        msg = stagnation_message(ctx)
        assert "停滞" in msg
        assert "建议" in msg
        assert "agent:retry" in msg

    def test_with_summary(self) -> None:
        ctx = FailureContext(session_end_summary="agent stuck at file search")
        msg = stagnation_message(ctx)
        assert "agent stuck at file search" in msg


class TestLoopDetectedMessage:
    def test_basic(self) -> None:
        ctx = FailureContext()
        msg = loop_detected_message(ctx)
        assert "循环" in msg
        assert "建议" in msg
        assert "agent:retry" in msg

    def test_with_summary(self) -> None:
        ctx = FailureContext(session_end_summary="repeated tool calls detected")
        msg = loop_detected_message(ctx)
        assert "repeated tool calls detected" in msg


class TestNoChangesMessage:
    def test_basic(self) -> None:
        ctx = FailureContext()
        msg = no_changes_message(ctx)
        assert "未产生任何代码变更" in msg
        assert "建议" in msg
        assert "agent:retry" in msg


class TestEmptyBranchMessage:
    def test_basic(self) -> None:
        ctx = FailureContext()
        msg = empty_branch_message(ctx)
        assert "未创建 PR" in msg
        assert "建议" in msg
        assert "agent:retry" in msg


class TestVerificationFailedMessage:
    def test_basic(self) -> None:
        ctx = FailureContext(error_detail="lint errors found")
        msg = verification_failed_message(ctx)
        assert "验证失败" in msg
        assert "lint errors found" in msg
        assert "建议" in msg

    def test_with_hook_name(self) -> None:
        ctx = FailureContext(hook_name="pre-commit", error_detail="black failed")
        msg = verification_failed_message(ctx)
        assert "pre-commit" in msg
        assert "black failed" in msg

    def test_no_detail(self) -> None:
        ctx = FailureContext()
        msg = verification_failed_message(ctx)
        assert "验证失败" in msg


class TestPostCommitFailedMessage:
    def test_basic(self) -> None:
        ctx = FailureContext(error_detail="push rejected")
        msg = post_commit_failed_message(ctx)
        assert "提交后处理失败" in msg
        assert "push rejected" in msg
        assert "建议" in msg

    def test_no_detail(self) -> None:
        ctx = FailureContext()
        msg = post_commit_failed_message(ctx)
        assert "提交后处理失败" in msg


class TestGenericFailureMessage:
    def test_basic(self) -> None:
        ctx = FailureContext(error_detail="connection refused")
        msg = generic_failure_message(ctx)
        assert "异常" in msg
        assert "connection refused" in msg
        assert "建议" in msg

    def test_no_detail(self) -> None:
        ctx = FailureContext()
        msg = generic_failure_message(ctx)
        assert "异常" in msg

    def test_with_retry(self) -> None:
        ctx = FailureContext(attempt=1, max_attempts=3, retry_delay_ms=10000)
        msg = generic_failure_message(ctx)
        assert "自动重试" in msg


class TestPremiseNotMetGuidance:
    def test_file_not_found(self) -> None:
        guidance = premise_not_met_guidance("file_not_found")
        assert "文件路径" in guidance

    def test_no_repro(self) -> None:
        guidance = premise_not_met_guidance("no_repro")
        assert "复现" in guidance

    def test_not_a_bug(self) -> None:
        guidance = premise_not_met_guidance("not_a_bug")
        assert "预期设计" in guidance

    def test_insufficient_context(self) -> None:
        guidance = premise_not_met_guidance("insufficient_context")
        assert "上下文" in guidance

    def test_cannot_proceed(self) -> None:
        guidance = premise_not_met_guidance("cannot_proceed")
        assert "准确完整" in guidance

    def test_unknown_reason(self) -> None:
        guidance = premise_not_met_guidance("unknown_reason")
        assert "准确完整" in guidance


class TestIssueSummaryGuidance:
    def test_failed(self) -> None:
        g = issue_summary_guidance("failed")
        assert "agent:retry" in g

    def test_agent_timeout(self) -> None:
        g = issue_summary_guidance("agent_timeout")
        assert "超时" in g

    def test_max_turns_exceeded(self) -> None:
        g = issue_summary_guidance("max_turns_exceeded")
        assert "轮次" in g

    def test_verification_failed(self) -> None:
        g = issue_summary_guidance("verification_failed")
        assert "CI" in g

    def test_stagnation(self) -> None:
        g = issue_summary_guidance("stagnation")
        assert "停滞" in g

    def test_loop_detected(self) -> None:
        g = issue_summary_guidance("loop_detected")
        assert "循环" in g

    def test_rate_limit(self) -> None:
        g = issue_summary_guidance("rate_limit_circuit_open")
        assert "熔断" in g

    def test_cancelled(self) -> None:
        g = issue_summary_guidance("cancelled")
        assert "取消" in g

    def test_before_run_failed(self) -> None:
        g = issue_summary_guidance("before_run_failed")
        assert "启动前" in g

    def test_unknown_status(self) -> None:
        g = issue_summary_guidance("unknown_status")
        assert "agent:retry" in g


class TestMessageActionability:
    """Verify every failure message contains actionable guidance."""

    @pytest.mark.parametrize(
        "message_func,ctx",
        [
            (timeout_message, FailureContext(timeout_ms=300000)),
            (max_turns_message, FailureContext(max_turns=50)),
            (rate_limit_message, FailureContext()),
            (stagnation_message, FailureContext()),
            (loop_detected_message, FailureContext()),
            (no_changes_message, FailureContext()),
            (empty_branch_message, FailureContext()),
            (verification_failed_message, FailureContext(error_detail="test error")),
            (post_commit_failed_message, FailureContext(error_detail="test error")),
            (generic_failure_message, FailureContext(error_detail="test error")),
        ],
    )
    def test_message_contains_guidance(self, message_func, ctx) -> None:
        msg = message_func(ctx)
        has_guidance = any(
            keyword in msg
            for keyword in ("建议", "下一步", "无需手动干预", "agent:retry", "自动重试")
        )
        assert has_guidance, f"Message missing actionable guidance: {msg[:80]}..."


# ---------------------------------------------------------------------------
# 12-cell matrix for build_failure_message()
# (3 ResumeStatus states × 4 error_codes that exercise the timeout
# routing; RESUMED is a benign acknowledgement and only one cell.)
# ---------------------------------------------------------------------------


_TIMEOUT_ERROR_CODES_FOR_MATRIX = [
    "handshake_timeout",
    "first_turn_timeout",
    "inactivity_timeout",
    "idle_watchdog_timeout",
]


class TestBuildFailureMessageRejected:
    """``ResumeStatus.REJECTED`` short-circuits all error_codes.

    The orchestrator must surface "transcript gone" or "unsupported"
    to the user regardless of which timeout bucket the backend
    happened to report.
    """

    @pytest.mark.parametrize("error_code", _TIMEOUT_ERROR_CODES_FOR_MATRIX)
    def test_rejected_short_circuits(self, error_code: str) -> None:
        msg = build_failure_message(
            status=ResumeStatus.REJECTED,
            error_code=error_code,
            retry_hint="系统将自动重试",
        )
        assert "拒绝" in msg or "reject" in msg.lower()
        assert "自动重试" in msg

    def test_rejected_with_unsupported_code(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.REJECTED,
            error_code="unsupported",
        )
        assert "拒绝" in msg


class TestBuildFailureMessageUndetectable:
    """``ResumeStatus.UNDETECTABLE`` routes per error_code."""

    def test_undetectable_handshake(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="handshake_timeout",
            timeout_seconds=30.0,
        )
        assert "30 秒" in msg
        assert "握手" in msg or "启动" in msg

    def test_undetectable_first_turn(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="first_turn_timeout",
            timeout_seconds=120.0,
        )
        assert "120 秒" in msg

    def test_undetectable_inactivity(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="inactivity_timeout",
            timeout_seconds=300.0,
        )
        assert "300 秒" in msg
        assert "token" in msg or "停滞" in msg or "无" in msg

    def test_undetectable_idle_watchdog(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="idle_watchdog_timeout",
            timeout_seconds=1800.0,
        )
        assert "1800 秒" in msg

    def test_undetectable_total_timeout(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="total_timeout",
            timeout_seconds=1800.0,
        )
        assert "1800 秒" in msg
        assert "总超时" in msg or "整轮" in msg

    def test_undetectable_unknown_error_code(self) -> None:
        # Defensive: unknown error_code under UNDETECTABLE → 兜底.
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="something_weird",
        )
        assert "未知" in msg or "失败" in msg


class TestBuildFailureMessageResumed:
    """``ResumeStatus.RESUMED`` is a benign acknowledgement."""

    def test_resumed_no_failure(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.RESUMED,
            error_code="any",
        )
        # Should not carry a failure adjective.
        assert "失败" not in msg
        assert "Resume" in msg or "成功" in msg


class TestBuildFailureMessageExtras:
    def test_extra_detail_appended(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.REJECTED,
            error_code="transcript_gc",
            extra_detail="ConnectionError: server returned 410",
        )
        assert "ConnectionError" in msg

    def test_retry_hint_appended(self) -> None:
        msg = build_failure_message(
            status=ResumeStatus.UNDETECTABLE,
            error_code="total_timeout",
            timeout_seconds=1800.0,
            retry_hint="系统将在 60 秒后自动重试",
        )
        assert "60 秒后自动重试" in msg


class TestResumeStatusMessageShorthand:
    def test_status_only_no_error_code(self) -> None:
        msg = resume_status_message(
            status=ResumeStatus.REJECTED,
            error_code="unsupported",
        )
        assert "拒绝" in msg

    def test_default_retry_hint_empty(self) -> None:
        # Empty retry_hint must NOT inject a "建议" segment into the
        # message; the user-facing message stays clean.
        msg = resume_status_message(status=ResumeStatus.REJECTED)
        assert "拒绝" in msg