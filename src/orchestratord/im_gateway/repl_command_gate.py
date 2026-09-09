"""IM 侧斜杠命令白名单门禁。

在 InboundDispatcher.process 中，对 opt-in runtime 的 origin 执行白名单检查：
只放行白名单内的斜杠命令。普通文本（非 ``/`` 开头）与未识别的斜杠命令
一样在网关层直接拒绝并返回 bounded notice——gateway 只承担命令查询/控制
与事件汇报，语义聊天（newPrompt/followUp/...）不在范围内。

默认 REPL 白名单覆盖 IM 交互真正需要的会话控制与只读查询命令；默认
Orchestrator 白名单覆盖 README 暴露给 IM 的命令。运行时有效列表来自
``channels.yaml`` 的 ``command_allowlists`` 配置。
"""

from __future__ import annotations

from collections.abc import Collection

from .config import (
    DEFAULT_ORCHESTRATOR_COMMAND_ALLOWLIST,
    DEFAULT_REPL_COMMAND_ALLOWLIST,
)

# Backward-compatible aliases for callers that enumerate the built-in defaults.
# Runtime dispatch receives its effective values from GatewayConfig instead.
REPL_ALLOWED_COMMANDS = frozenset(DEFAULT_REPL_COMMAND_ALLOWLIST)
ORCHESTRATOR_ALLOWED_COMMANDS = frozenset(DEFAULT_ORCHESTRATOR_COMMAND_ALLOWLIST)

# Bounded notice for plain text (review P1-2): the gateway is command-only;
# ordinary chat text must never reach the orchestrator peer or any agent
# handler.
PLAIN_TEXT_NOTICE = "网关仅支持白名单斜杠命令，普通文本不会被转发。"


def _block_reason(cmd_token: str) -> str:
    """构造拒绝消息，回显被拒绝的命令。"""
    return f"`{cmd_token}` 非命令白名单，已被 gateway 禁止执行。"


def _unsupported_reason(command_display: str) -> str:
    return f"不支持 {command_display} 执行"


def _slash_parts(text: str) -> list[str]:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return []
    return stripped.split(maxsplit=2)


def check_repl_command(
    text: str,
    *,
    allowed_commands: Collection[str] | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). 只放行白名单内的斜杠命令。

    带参数的命令（如 /goal finish）按命令名前缀判定；纯命令（如 /stop）
    直接匹配。普通文本（含空白与单独的 ``/``）返回
    ``(False, PLAIN_TEXT_NOTICE)``——IM 渠道没有 slash palette 入口，
    非命令输入一律拒绝。
    """
    parts = _slash_parts(text)
    if not parts:
        return False, PLAIN_TEXT_NOTICE
    # 取第一个 token 作为命令名（含前导 /），小写比较
    cmd_token = parts[0].lower()
    # 单独的 "/"（无命令名）也是普通文本，拒绝
    if cmd_token == "/":
        return False, PLAIN_TEXT_NOTICE
    effective_allowlist = REPL_ALLOWED_COMMANDS if allowed_commands is None else allowed_commands
    if cmd_token in effective_allowlist:
        return True, ""
    return False, _block_reason(cmd_token)


def check_orchestrator_command(
    text: str,
    *,
    allowed_commands: Collection[str] | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason) for orchestrator IM slash commands.

    Only README-listed issue commands and ``/server status`` pass. Plain text
    is rejected with the shared bounded notice: orchestratord's gateway
    surface is commands + event reports only, so ordinary text must never
    reach the orchestrator peer.
    """
    parts = _slash_parts(text)
    if not parts:
        return False, PLAIN_TEXT_NOTICE
    first = parts[0].lower()
    second = parts[1].lower() if len(parts) > 1 else ""
    command_display = first if not second else f"{first} {second}"
    command_key = command_display
    effective_allowlist = (
        ORCHESTRATOR_ALLOWED_COMMANDS if allowed_commands is None else allowed_commands
    )
    if command_key in effective_allowlist:
        return True, ""
    return False, _unsupported_reason(command_display)


__all__ = [
    "ORCHESTRATOR_ALLOWED_COMMANDS",
    "PLAIN_TEXT_NOTICE",
    "REPL_ALLOWED_COMMANDS",
    "check_orchestrator_command",
    "check_repl_command",
]
