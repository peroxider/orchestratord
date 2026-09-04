"""Tests for InboundDispatcher slash-command gate + processing status.

Migrated from ClawCodex ``test_dispatcher.py`` + ``test_processing_status.py``
+ ``test_repl_command_gate.py``.
"""

from __future__ import annotations

import pytest

from orchestratord.channels.capabilities import (
    ChannelCapability,
    ChannelCapabilitySet,
    ProcessingOutcome,
)
from orchestratord.im_gateway.binding import BindingPolicy
from orchestratord.im_gateway.config import CommandAllowlistConfig
from orchestratord.im_gateway.dispatcher import InboundDispatcher
from orchestratord.im_gateway.processing_status import ProcessingStatusManager
from orchestratord.im_gateway.repl_command_gate import (
    ORCHESTRATOR_ALLOWED_COMMANDS,
    REPL_ALLOWED_COMMANDS,
    check_orchestrator_command,
    check_repl_command,
)
from orchestratord.im_gateway.router import SessionRouter
from orchestratord.im_gateway.store import ReliabilityStore
from orchestratord.ipc.models import (
    AckLayer,
    AckReceipt,
    InboundMessage,
    MessageSemantics,
    SessionTarget,
)


def _make_message(origin: str, text: str, *, message_id: str | None = None) -> InboundMessage:
    return InboundMessage(
        origin=origin,
        text=text,
        message_id=message_id or f"mid-{origin}-{abs(hash(text))}",
        channel="wechat",
    )


def _make_dispatcher(
    tmp_path,
    *,
    repl_origin: str = "wechat:acct:user1",
    orchestrator_origin: str = "wechat:acct:user2",
    command_allowlists: CommandAllowlistConfig | None = None,
) -> tuple[InboundDispatcher, SessionRouter, list[InboundMessage]]:
    """构造一个 dispatcher，REPL 与 orchestrator 各绑定一个 origin。

    返回 (dispatcher, router, pushed_messages)。
    pushed_messages 记录 _push_handler 收到的所有消息，用于断言是否被调用。
    """
    store = ReliabilityStore(tmp_path)
    binding = BindingPolicy()
    binding.bind(repl_origin, SessionTarget(session_id="repl-sess", host_type="repl"))
    binding.bind(
        orchestrator_origin,
        SessionTarget(session_id="orch-sess", host_type="orchestrator"),
    )
    router = SessionRouter(binding, store)

    pushed: list[InboundMessage] = []

    async def push_handler(message: InboundMessage) -> bool:
        pushed.append(message)
        return True

    dispatcher = InboundDispatcher(store, router, command_allowlists=command_allowlists)
    dispatcher.set_push_handler(push_handler)
    return dispatcher, router, pushed


# -- REPL 目标：非白名单命令被拒绝 -------------------------------------------


@pytest.mark.asyncio
async def test_repl_blocked_command_not_pushed(tmp_path) -> None:
    """REPL 绑定的 origin 发送 /exit → 返回拒绝 ack，push_handler 不被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "/exit")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 0, "blocked command must not be pushed to REPL"
    assert receipt.layer == AckLayer.ACCEPTED
    assert "/exit" in (receipt.message or "")


@pytest.mark.asyncio
async def test_repl_blocked_command_with_args_not_pushed(tmp_path) -> None:
    """REPL 绑定的 origin 发送 /model gpt-4 → 拒绝，push_handler 不被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "/model gpt-4")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 0
    assert receipt.layer == AckLayer.ACCEPTED
    assert "/model" in (receipt.message or "")


# -- REPL 目标：白名单命令被放行 ---------------------------------------------


@pytest.mark.asyncio
async def test_repl_allowed_command_pushed(tmp_path) -> None:
    """REPL 绑定的 origin 发送 /clear → push_handler 被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "/clear")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 1
    assert pushed[0].text == "/clear"
    assert receipt.layer == AckLayer.ENQUEUED


@pytest.mark.asyncio
async def test_repl_allowed_command_with_args_pushed(tmp_path) -> None:
    """REPL 绑定的 origin 发送 /goal finish → push_handler 被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "/goal finish the task")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 1
    assert receipt.layer == AckLayer.ENQUEUED


@pytest.mark.asyncio
async def test_repl_stop_command_pushed(tmp_path) -> None:
    """REPL 绑定的 origin 发送 /stop → push_handler 被调用（/stop 在白名单内）。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "/stop")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 1
    assert receipt.layer == AckLayer.ENQUEUED


# -- REPL 目标：普通文本放行 -------------------------------------------------


@pytest.mark.asyncio
async def test_repl_plain_text_pushed(tmp_path) -> None:
    """REPL 绑定的 origin 发送普通文本 → push_handler 被调用（白名单不影响非斜杠输入）。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "你好，请帮我写个函数")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 1
    assert receipt.layer == AckLayer.ENQUEUED


@pytest.mark.asyncio
async def test_repl_busy_plain_text_classified_as_follow_up(tmp_path) -> None:
    """busy 普通文本 → followUp（queue-as-followUp）；NL 不猜 interrupt/contextOnly。"""
    dispatcher, _, pushed = _make_dispatcher(tmp_path)

    async def classify_message(text: str) -> str:
        msg = _make_message("wechat:acct:user1", text)
        return dispatcher.classify(msg, is_busy=True).value

    assert await classify_message("补充一下需求") == "followUp"
    # interrupt / contextOnly cannot be produced by plain NL classification
    msg = _make_message("wechat:acct:user1", "先停一下")
    assert dispatcher.classify(msg, is_busy=False) is MessageSemantics.NEW_PROMPT
    assert len(pushed) == 0


@pytest.mark.asyncio
async def test_repl_deliver_as_metadata_wins_over_classification(tmp_path) -> None:
    """结构化 deliverAs 元数据优先于 NL 分类。"""
    dispatcher, _, _pushed = _make_dispatcher(tmp_path)

    msg = _make_message("wechat:acct:user1", "普通文本", message_id="deliveras-1")
    msg.raw = {"deliverAs": "interrupt"}
    semantic = dispatcher.classify(msg)
    assert semantic is MessageSemantics.INTERRUPT

    msg2 = _make_message("wechat:acct:user1", "普通文本", message_id="deliveras-2")
    msg2.raw = {"deliverAs": "contextOnly"}
    assert dispatcher.classify(msg2) is MessageSemantics.CONTEXT_ONLY


@pytest.mark.asyncio
async def test_repl_uses_channels_yaml_command_allowlist(tmp_path) -> None:
    dispatcher, _router, pushed = _make_dispatcher(
        tmp_path,
        command_allowlists=CommandAllowlistConfig(
            repl=("/model",),
            orchestrator=(),
        ),
    )

    allowed_receipt = await dispatcher.process(
        _make_message("wechat:acct:user1", "/model gpt-5", message_id="repl-custom-allowed")
    )
    blocked_receipt = await dispatcher.process(
        _make_message("wechat:acct:user1", "/clear", message_id="repl-default-blocked")
    )

    assert [message.text for message in pushed] == ["/model gpt-5"]
    assert allowed_receipt.layer is AckLayer.ENQUEUED
    assert blocked_receipt.notify_user is True


# -- orchestrator 目标：白名单生效 -------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_blocked_command_not_pushed(tmp_path) -> None:
    """orchestrator 绑定的 origin 发送 /dashboard → 拒绝，push_handler 不被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user2", "/dashboard")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 0, "blocked command must not be pushed to orchestrator"
    assert receipt.layer == AckLayer.ACCEPTED
    assert receipt.notify_user is True
    assert receipt.message == "不支持 /dashboard 执行"


@pytest.mark.asyncio
async def test_orchestrator_issue_takeover_not_pushed(tmp_path) -> None:
    """orchestrator 绑定的 origin 发送 /issue takeover → 拒绝，不进入 IPC。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user2", "/issue takeover --id AGENTSDK-15")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 0
    assert receipt.layer == AckLayer.ACCEPTED
    assert receipt.notify_user is True
    assert receipt.message == "不支持 /issue takeover 执行"


@pytest.mark.asyncio
async def test_orchestrator_allowed_issue_command_pushed(tmp_path) -> None:
    """orchestrator 绑定的 origin 发送 /issue list → push_handler 被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user2", "/issue list")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 1
    assert pushed[0].text == "/issue list"
    assert pushed[0].semantic is MessageSemantics.COMMAND
    assert receipt.layer == AckLayer.ENQUEUED


@pytest.mark.asyncio
async def test_orchestrator_allowed_server_status_pushed(tmp_path) -> None:
    """orchestrator 绑定的 origin 发送 /server status → push_handler 被调用。"""
    dispatcher, _router, pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user2", "/server status")

    receipt = await dispatcher.process(msg)

    assert len(pushed) == 1
    assert pushed[0].text == "/server status"
    assert pushed[0].semantic is MessageSemantics.COMMAND
    assert receipt.layer == AckLayer.ENQUEUED


@pytest.mark.asyncio
async def test_orchestrator_uses_channels_yaml_command_allowlist(tmp_path) -> None:
    dispatcher, _router, pushed = _make_dispatcher(
        tmp_path,
        command_allowlists=CommandAllowlistConfig(
            repl=(),
            orchestrator=("/issue takeover",),
        ),
    )

    allowed_receipt = await dispatcher.process(
        _make_message(
            "wechat:acct:user2",
            "/issue takeover --id AGENTSDK-15",
            message_id="orch-custom-allowed",
        )
    )
    blocked_receipt = await dispatcher.process(
        _make_message(
            "wechat:acct:user2",
            "/server status",
            message_id="orch-default-blocked",
        )
    )

    assert [message.text for message in pushed] == ["/issue takeover --id AGENTSDK-15"]
    assert allowed_receipt.layer is AckLayer.ENQUEUED
    assert blocked_receipt.notify_user is True


# -- 拒绝时记录 audit -------------------------------------------------------


@pytest.mark.asyncio
async def test_repl_blocked_command_records_audit(tmp_path) -> None:
    """REPL 绑定的 origin 发送非白名单命令 → audit.ndjson 记录 repl_command_blocked 事件。"""
    dispatcher, _router, _pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user1", "/exit")

    await dispatcher.process(msg)

    audit_entries = dispatcher._store.audit_entries()
    blocked_entries = [e for e in audit_entries if e.get("event_type") == "repl_command_blocked"]
    assert len(blocked_entries) == 1
    assert "/exit" in (blocked_entries[0].get("command") or "")


@pytest.mark.asyncio
async def test_orchestrator_blocked_command_records_audit(tmp_path) -> None:
    """orchestrator 绑定的 origin 发送非白名单命令 → audit.ndjson 记录事件。"""
    dispatcher, _router, _pushed = _make_dispatcher(tmp_path)
    msg = _make_message("wechat:acct:user2", "/server stop")

    await dispatcher.process(msg)

    audit_entries = dispatcher._store.audit_entries()
    blocked_entries = [
        e for e in audit_entries if e.get("event_type") == "orchestrator_command_blocked"
    ]
    assert len(blocked_entries) == 1
    assert "/server stop" in (blocked_entries[0].get("command") or "")


# -- repl_command_gate ------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    sorted(REPL_ALLOWED_COMMANDS),
    ids=lambda c: f"allowed:{c}",
)
def test_allowed_command_passes(cmd: str) -> None:
    allowed, reason = check_repl_command(cmd)
    assert allowed is True
    assert reason == ""


def test_allowed_command_with_args_passes() -> None:
    """带参数的命令按命令名前缀判定，应放行。"""
    for text in ["/goal finish the task", "/clear all", "/help me", "/stop now"]:
        allowed, reason = check_repl_command(text)
        assert allowed is True, f"expected {text!r} to be allowed"
        assert reason == ""


@pytest.mark.parametrize(
    "cmd",
    [
        "/exit",
        "/quit",
        "/q",
        "/login",
        "/permissions",
        "/permission",
        "/model",
        "/provider",
        "/init",
        "/compact",
        "/save",
        "/load",
        "/resume",
        "/cron-run",
        "/cron-fire",
        "/cron-delete",
        "/tool",
        "/memory",
        "/rewind",
        "/advisor",
        "/telemetry",
        "/vim",
        "/tui",
        "/unknown-cmd-xyz",
    ],
    ids=lambda c: f"blocked:{c}",
)
def test_blocked_command_rejected(cmd: str) -> None:
    allowed, reason = check_repl_command(cmd)
    assert allowed is False
    # reason 必须回显被拒绝的命令
    assert cmd.lower() in reason
    assert "非命令白名单" in reason or "已被" in reason or "禁止" in reason


def test_blocked_command_reason_echoes_command_token() -> None:
    """拒绝消息必须包含被拒绝的命令 token（回显）。"""
    allowed, reason = check_repl_command("/exit")
    assert allowed is False
    assert "`/exit`" in reason


def test_blocked_command_with_args_rejected() -> None:
    """带参数的非白名单命令也应拒绝，reason 回显命令 token。"""
    allowed, reason = check_repl_command("/model gpt-4")
    assert allowed is False
    assert "`/model`" in reason


@pytest.mark.parametrize(
    "text",
    ["hello", "普通文本消息", "  /  ", "/", "", "   ", "not a command"],
    ids=lambda t: f"passthrough:{t!r}",
)
def test_non_slash_input_passes(text: str) -> None:
    allowed, reason = check_repl_command(text)
    assert allowed is True
    assert reason == ""


@pytest.mark.parametrize(
    "cmd",
    ["/STOP", "/Clear", "/RESET", "/NEW", "/GOAL", "/Help", "/COST", "/DOCTOR"],
)
def test_case_insensitive(cmd: str) -> None:
    allowed, reason = check_repl_command(cmd)
    assert allowed is True, f"expected {cmd!r} (case-insensitive) to be allowed"
    assert reason == ""


def test_case_insensitive_blocked() -> None:
    """非白名单命令的大写形式也应拒绝。"""
    allowed, reason = check_repl_command("/EXIT")
    assert allowed is False
    assert "/exit" in reason  # reason 中回显的 token 是小写化后的


def test_repl_command_uses_configured_allowlist() -> None:
    allowed, reason = check_repl_command("/model gpt-5", allowed_commands={"/model"})
    assert allowed is True
    assert reason == ""

    allowed, reason = check_repl_command("/clear", allowed_commands=set())
    assert allowed is False
    assert "`/clear`" in reason


# -- Orchestrator 白名单 -----------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    sorted(ORCHESTRATOR_ALLOWED_COMMANDS),
    ids=lambda c: f"orch-allowed:{c}",
)
def test_orchestrator_allowed_commands_pass(cmd: str) -> None:
    allowed, reason = check_orchestrator_command(cmd)
    assert allowed is True
    assert reason == ""


def test_orchestrator_allowed_command_with_args_passes() -> None:
    for text in [
        "/issue list --status running",
        "/issue show --id AGENTSDK-15",
        '/issue inject --id AGENTSDK-15 "address review comments"',
        "/issue feedback --id AGENTSDK-15 --approve",
        '/issue review --id AGENTSDK-15 --reject --feedback "needs tests"',
        "/issue retry --id AGENTSDK-15 --mode reset",
        "/server status --workflow ./workflow.md",
        "/issue rebase --id AGENTSDK-15",
    ]:
        allowed, reason = check_orchestrator_command(text)
        assert allowed is True, f"expected {text!r} to be allowed"
        assert reason == ""


@pytest.mark.parametrize(
    ("cmd", "reason"),
    [
        ("/server stop", "不支持 /server stop 执行"),
        ("/issue takeover", "不支持 /issue takeover 执行"),
        ("/dashboard", "不支持 /dashboard 执行"),
        ("/workflow init", "不支持 /workflow init 执行"),
        ("/unknown-cmd-xyz", "不支持 /unknown-cmd-xyz 执行"),
    ],
    ids=lambda c: str(c),
)
def test_orchestrator_blocked_commands_rejected(cmd: str, reason: str) -> None:
    allowed, actual = check_orchestrator_command(cmd)
    assert allowed is False
    assert actual == reason


def test_orchestrator_plain_text_passes() -> None:
    allowed, reason = check_orchestrator_command("普通文本 follow-up")
    assert allowed is True
    assert reason == ""


def test_orchestrator_command_uses_configured_allowlist() -> None:
    allowed, reason = check_orchestrator_command(
        "/issue takeover --id AGENTSDK-15",
        allowed_commands={"/issue takeover"},
    )
    assert allowed is True
    assert reason == ""

    allowed, reason = check_orchestrator_command(
        "/server status",
        allowed_commands=set(),
    )
    assert allowed is False
    assert reason == "不支持 /server status 执行"


# -- processing status ------------------------------------------------------


class _StatusAdapter:
    channel_id = "feishu"
    capabilities = ChannelCapabilitySet.of(ChannelCapability.PROCESSING_STATUS)

    def __init__(self) -> None:
        self.starts: list[str] = []
        self.completions: list[tuple[str, ProcessingOutcome]] = []
        self.complete_result = True
        self.raise_on_start = False
        self.raise_on_complete = False

    async def on_processing_start(self, message_id: str) -> bool:
        self.starts.append(message_id)
        if self.raise_on_start:
            raise RuntimeError("reaction add failed")
        return True

    async def on_processing_complete(
        self,
        message_id: str,
        outcome: ProcessingOutcome,
    ) -> bool:
        self.completions.append((message_id, outcome))
        if self.raise_on_complete:
            raise RuntimeError("reaction delete failed")
        return self.complete_result


class _Registry:
    def __init__(self, adapter: _StatusAdapter) -> None:
        self.adapter = adapter

    def get(self, name: str):
        return self.adapter if name == "feishu" else None


def _message(
    message_id: str = "om_1",
    origin: str = "feishu:dm:app:user",
    text: str = "hello",
) -> InboundMessage:
    return InboundMessage(
        origin=origin,
        text=text,
        message_id=message_id,
        channel="feishu",
    )


@pytest.mark.asyncio
async def test_processing_status_manager_is_idempotent_and_validates_origin() -> None:
    adapter = _StatusAdapter()
    manager = ProcessingStatusManager(_Registry(adapter))
    message = _message()

    assert await manager.start(message) is True
    assert await manager.start(message) is True
    assert adapter.starts == ["om_1"]
    assert await manager.complete("om_1", ProcessingOutcome.SUCCESS, origin="wrong") is False
    assert manager.has_pending("om_1") is True

    assert await manager.complete("om_1", ProcessingOutcome.SUCCESS, origin=message.origin) is True
    assert adapter.completions == [("om_1", ProcessingOutcome.SUCCESS)]
    assert manager.has_pending("om_1") is False


@pytest.mark.asyncio
async def test_processing_status_manager_retains_failed_completion_and_bounds_lru() -> None:
    adapter = _StatusAdapter()
    adapter.complete_result = False
    manager = ProcessingStatusManager(_Registry(adapter), max_pending=2)
    await manager.start(_message("om_1"))
    await manager.start(_message("om_2"))
    await manager.start(_message("om_3"))

    assert manager.has_pending("om_1") is False
    assert await manager.complete("om_2", ProcessingOutcome.FAILURE) is False
    assert manager.has_pending("om_2") is True


@pytest.mark.asyncio
async def test_dispatcher_completes_local_handler_and_keeps_opt_in_pending(tmp_path) -> None:
    adapter = _StatusAdapter()
    manager = ProcessingStatusManager(_Registry(adapter))
    store = ReliabilityStore(tmp_path)
    binding = BindingPolicy()
    router = SessionRouter(binding, store)
    dispatcher = InboundDispatcher(store, router, processing_status=manager)

    async def local_handler(message: InboundMessage) -> AckReceipt:
        return AckReceipt(message.message_id, AckLayer.PROCESSED, "done")

    dispatcher.set_handler(local_handler)
    await dispatcher.process(_message("om_local"))
    assert adapter.starts == ["om_local"]
    assert adapter.completions == [("om_local", ProcessingOutcome.SUCCESS)]

    origin = "feishu:dm:app:optin"
    binding.bind(origin, SessionTarget(session_id="repl-1", host_type="repl"))

    async def push_handler(message: InboundMessage) -> bool:
        return True

    dispatcher.set_push_handler(push_handler)
    receipt = await dispatcher.process(_message("om_optin", origin))
    assert receipt.layer is AckLayer.ENQUEUED
    assert manager.has_pending("om_optin") is True


@pytest.mark.asyncio
async def test_dispatcher_dedupe_and_blocked_command_do_not_start_status(tmp_path) -> None:
    adapter = _StatusAdapter()
    manager = ProcessingStatusManager(_Registry(adapter))
    store = ReliabilityStore(tmp_path)
    binding = BindingPolicy()
    origin = "feishu:dm:app:user"
    binding.bind(origin, SessionTarget(session_id="repl-1", host_type="repl"))
    dispatcher = InboundDispatcher(
        store,
        SessionRouter(binding, store),
        processing_status=manager,
    )

    await dispatcher.process(_message("om_blocked", origin=origin, text="/exit"))
    assert adapter.starts == []

    async def push_handler(message: InboundMessage) -> bool:
        return True

    dispatcher.set_push_handler(push_handler)
    accepted = _message("om_duplicate", origin=origin)
    await dispatcher.process(accepted)
    await dispatcher.process(accepted)
    assert adapter.starts == ["om_duplicate"]


@pytest.mark.asyncio
async def test_processing_hook_exceptions_do_not_block_local_handler(tmp_path) -> None:
    adapter = _StatusAdapter()
    adapter.raise_on_start = True
    adapter.raise_on_complete = True
    manager = ProcessingStatusManager(_Registry(adapter))
    store = ReliabilityStore(tmp_path)
    dispatcher = InboundDispatcher(
        store,
        SessionRouter(BindingPolicy(), store),
        processing_status=manager,
    )

    async def local_handler(message: InboundMessage) -> AckReceipt:
        return AckReceipt(message.message_id, AckLayer.PROCESSED, "text reply sent")

    dispatcher.set_handler(local_handler)
    receipt = await dispatcher.process(_message("om_reaction_error"))

    assert receipt.layer is AckLayer.PROCESSED
    assert manager.has_pending("om_reaction_error") is True
