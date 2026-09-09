"""Shared CLI/IM command behavior, process boundaries and cancellation."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestratord.commands import issue
from orchestratord.commands.parsing import parse_command
from orchestratord.commands.service import OrchestratorCommandService
from orchestratord.im_gateway_client import (
    OrchestratorGatewayClient,
    OrchestratorHandlers,
)
from orchestratord.issue_registry import IssueRegistry


def gateway(service):
    handlers = OrchestratorHandlers(
        **{
            name: lambda *args: None
            for name in OrchestratorHandlers.__dataclass_fields__
        }
    )
    return OrchestratorGatewayClient(handlers, command_service=service)


@pytest.fixture
def workspace(tmp_path):
    registry = IssueRegistry(tmp_path / ".orchestratord_issue_registry.json")
    registry.register("5", "ISSUE-5", workspace_path=str(tmp_path / "ISSUE-5"))
    (tmp_path / "ISSUE-5").mkdir()
    (tmp_path / "ISSUE-5" / "note.txt").write_text("fixture content")
    return tmp_path


@pytest.mark.parametrize(
    "argv",
    [
        ["issue", "list"],
        ["issue", "show", "--id", "5"],
        ["issue", "workspace", "--id", "5", "--cat", "note.txt"],
        ["issue", "inject", "--id", "5", "--list"],
        ["issue", "feedback", "--id", "5", "--list"],
        ["issue", "clarify", "--id", "5", "--answer", "confirmed"],
        ["issue", "review", "--id", "5", "--approve"],
        ["issue", "rebase", "--id", "5"],
        ["server", "status"],
    ],
)
def test_cli_and_gateway_share_results(argv, workspace, capsys, monkeypatch):
    """The real terminal adapter and IM adapter agree on output and exit code."""
    from orchestratord.cli import issue as cli_issue
    from orchestratord.cli import server as cli_server

    monkeypatch.setenv("ORCHESTRATORD_WORKSPACE_ROOT", str(workspace))
    request = parse_command(
        [*argv, "--workspace", str(workspace)] if argv[0] == "server" else argv
    )
    adapter = cli_issue if request.resource == "issue" else cli_server
    code = adapter.run(request.namespace())
    terminal = capsys.readouterr()
    service = OrchestratorCommandService(workspace_root=workspace)
    actual = asyncio.run(gateway(service)._run_cli_isolated(argv))
    assert actual == (code, terminal.out, terminal.err)


async def test_gateway_does_not_spawn_or_mutate_process_globals(
    workspace, monkeypatch, capsys
):
    spawn = AsyncMock(side_effect=AssertionError("command must stay in-process"))
    worker = AsyncMock(
        side_effect=AssertionError("command must stay on the event loop")
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(asyncio, "to_thread", worker)
    before = (sys.argv, sys.stdout, sys.stderr, sys.stdin)
    rc, stdout, stderr = await gateway(
        OrchestratorCommandService(workspace_root=workspace)
    )._run_cli_isolated(["issue", "show", "--id", "5"])
    assert rc == 0 and "ISSUE-5" in stdout and not stderr
    assert all(
        a is b
        for a, b in zip(
            before, (sys.argv, sys.stdout, sys.stderr, sys.stdin), strict=True
        )
    )
    assert capsys.readouterr() == ("", "")
    spawn.assert_not_called()
    worker.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["issue", "show"],
        ["issue", "list", "--bad-option"],
        ["issue", "--help"],
        ["server", "stop"],
    ],
)
async def test_parse_errors_are_request_local(argv, workspace, capsys):
    result = await gateway(
        OrchestratorCommandService(workspace_root=workspace)
    )._run_cli_isolated(argv)
    assert result[0] == 2 and result[2]
    assert capsys.readouterr() == ("", "")


async def test_command_timeout_cancels_remaining_mutation(workspace, monkeypatch):
    cancelled = asyncio.Event()
    marker = workspace / "late-write"

    async def delayed(context, args):
        try:
            await asyncio.Event().wait()
            marker.write_text("must not happen")
        finally:
            cancelled.set()

    monkeypatch.setattr(issue, "_run_inject", delayed)
    service = OrchestratorCommandService(workspace_root=workspace, timeout_seconds=0.02)
    result = await service.execute(
        parse_command(["issue", "inject", "--id", "5", "hint"])
    )
    assert result.exit_code == 124
    assert cancelled.is_set() and not marker.exists()
    following = await service.execute(parse_command(["issue", "show", "--id", "5"]))
    assert following.exit_code == 0


async def test_disconnect_cancellation_propagates_and_releases_lock(
    workspace, monkeypatch
):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def delayed(context, args):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(issue, "_run_inject", delayed)
    service = OrchestratorCommandService(workspace_root=workspace)
    task = asyncio.create_task(
        service.execute(parse_command(["issue", "inject", "--id", "5", "hint"]))
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert (await service.execute(parse_command(["issue", "list"]))).exit_code == 0


async def test_concurrent_requests_serialize_and_keep_output_separate(
    workspace, monkeypatch
):
    active = 0
    order = []

    async def echo(context, args):
        nonlocal active
        active += 1
        assert active == 1
        context.output.write(args.hint)
        await asyncio.sleep(0)
        order.append(args.hint)
        active -= 1
        return 0

    monkeypatch.setattr(issue, "_run_inject", echo)
    service = OrchestratorCommandService(workspace_root=workspace)
    results = await asyncio.gather(
        *[
            service.execute(parse_command(["issue", "inject", "--id", "5", text]))
            for text in ("first", "second")
        ]
    )
    assert order == ["first", "second"]
    assert [result.stdout for result in results] == ["first\n", "second\n"]


async def test_workspace_binding_overrides_environment_and_rejects_foreign_paths(
    workspace, tmp_path, monkeypatch
):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("ORCHESTRATORD_WORKSPACE_ROOT", str(other))
    service = OrchestratorCommandService(workspace_root=workspace)
    client = gateway(service)
    assert (await client._run_cli_isolated(["issue", "show", "--id", "5"]))[0] == 0
    for option in ("--workspace", "--workflow"):
        result = await client._run_cli_isolated(
            ["issue", "inject", "--id", "5", "hint", option, str(other)]
        )
        assert result[0] == 2
    assert not (workspace / "ISSUE-5" / ".operator_hints.md").exists()


async def test_live_registry_and_control_are_shared(workspace):
    registry = IssueRegistry(workspace / ".orchestratord_issue_registry.json")
    # An unflushed update is visible only via the daemon's actual registry.
    registry.get("5").issue_identifier = "LIVE-IDENTIFIER"
    calls = []
    runtime = SimpleNamespace(
        _registry=registry,
        _state=SimpleNamespace(running={"5": object()}),
        _apply_control_command=lambda *args: calls.append(args),
    )
    service = OrchestratorCommandService(
        workspace_root=workspace, runtime_supplier=lambda: runtime
    )
    client = gateway(service)
    assert (
        "LIVE-IDENTIFIER"
        in (await client._run_cli_isolated(["issue", "show", "--id", "5"]))[1]
    )
    current_workspace = workspace / "current-run"
    current_workspace.mkdir()
    (current_workspace / "live.txt").write_text("current workspace")
    registry.get("5").workspace_path = str(current_workspace)
    assert (
        "current workspace"
        in (
            await client._run_cli_isolated(
                ["issue", "workspace", "--id", "LIVE-IDENTIFIER", "--cat", "live.txt"]
            )
        )[1]
    )
    for verb in ("pause", "resume", "stop"):
        args = ["issue", verb, "--id", "LIVE-IDENTIFIER"]
        if verb == "pause":
            args += ["--reason", "review first"]
        assert (await client._run_cli_isolated(args))[0] == 0
    assert calls == [
        ("pause", "5", "review first"),
        ("resume", "5", ""),
        ("stop", "5", ""),
    ]
    assert not (workspace / ".orchestrator_control").exists()
    assert (await client._run_cli_isolated(["issue", "pause", "--id", "missing"]))[
        0
    ] == 1


async def test_mutations_and_failure_codes(workspace, monkeypatch):
    monkeypatch.setattr(
        "orchestratord.commands.models.AUDIT_LOG", workspace / "audit.jsonl"
    )
    client = gateway(OrchestratorCommandService(workspace_root=workspace))
    assert (
        await client._run_cli_isolated(
            ["issue", "inject", "--id", "5", "run tests", "--no-wait"]
        )
    )[0] == 0
    assert "run tests" in (workspace / "ISSUE-5" / ".operator_hints.md").read_text()
    assert (
        await client._run_cli_isolated(
            [
                "issue",
                "workspace",
                "--id",
                "5",
                "--edit",
                "note.txt",
                "--with",
                "updated",
            ]
        )
    )[0] == 0
    assert (workspace / "ISSUE-5" / "note.txt").read_text() == "updated"
    registry = IssueRegistry(workspace / ".orchestratord_issue_registry.json")
    registry.mark_pending_review("5")
    assert (
        await client._run_cli_isolated(
            ["issue", "review", "--id", "5", "--reject", "--feedback", "add tests"]
        )
    )[0] == 0
    assert (
        "add tests"
        in (workspace / ".orchestrator_control" / "review_retry_5.control").read_text()
    )


async def test_unready_runtime_fails_without_side_effects(workspace):
    service = OrchestratorCommandService(
        workspace_root=workspace, runtime_supplier=lambda: None
    )
    result = await service.execute(
        parse_command(["issue", "inject", "--id", "5", "hint"])
    )
    assert result.exit_code == 1 and "not ready" in result.stderr
    assert not (workspace / "ISSUE-5" / ".operator_hints.md").exists()


def test_output_is_bounded():
    from orchestratord.commands.models import CommandOutput

    output = CommandOutput(limit=100)
    output.write("x" * 200)
    output.write("more")
    assert len(output.stdout) <= 100 and "truncated" in output.stdout


async def test_expired_queued_command_never_starts(workspace, monkeypatch):
    operation = AsyncMock(return_value=0)
    monkeypatch.setattr(issue, "_run_inject", operation)
    service = OrchestratorCommandService(workspace_root=workspace, timeout_seconds=0.02)
    await service._lock.acquire()
    try:
        result = await service.execute(
            parse_command(["issue", "inject", "--id", "5", "hint"])
        )
    finally:
        service._lock.release()
    assert result.exit_code == 124
    operation.assert_not_called()
    assert (await service.execute(parse_command(["issue", "list"]))).exit_code == 0


async def test_socket_wait_is_cancelled(tmp_path, monkeypatch):
    disconnected = asyncio.Event()
    received = []

    async def peer(reader, writer):
        try:
            received.append(await reader.readline())
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()
            disconnected.set()

    sock = tmp_path / "s"
    server = await asyncio.start_unix_server(peer, path=str(sock))

    async def inject(context, args):
        await issue._send_and_wait(
            context, sock, "inject", "hint", "Injected", timeout=30
        )
        return 0

    monkeypatch.setattr(issue, "_run_inject", inject)
    service = OrchestratorCommandService(workspace_root=tmp_path, timeout_seconds=0.05)
    async with server:
        result = await service.execute(
            parse_command(["issue", "inject", "--id", "5", "hint"])
        )
        await asyncio.wait_for(disconnected.wait(), timeout=1)
    assert result.exit_code == 124
    assert b'"cmd": "inject"' in received[0]


async def test_clarify_updates_live_queue(workspace):
    from orchestratord.issue_clarifier.queue import ClarificationQueue

    registry = IssueRegistry(workspace / ".orchestratord_issue_registry.json")
    queue = ClarificationQueue(workspace / ".orchestratord_clarification_queue.json")
    queue.enqueue("5", "ISSUE-5", "Which branch?")
    runtime = SimpleNamespace(_registry=registry, _clarification_queue=queue)
    service = OrchestratorCommandService(
        workspace_root=workspace, runtime_supplier=lambda: runtime
    )
    result = await service.execute(
        parse_command(["issue", "clarify", "--id", "5", "--answer", "main"])
    )
    assert result.exit_code == 0
    assert queue.get("5").answer == "main"


async def test_feedback_control_write_failure_is_reported(workspace, monkeypatch):
    registry = IssueRegistry(workspace / ".orchestratord_issue_registry.json")
    registry.get("5").pending_feedback_ids = ["comment:123"]
    registry._save()
    monkeypatch.setattr(issue, "_write_control", AsyncMock(return_value=1))
    result = await OrchestratorCommandService(workspace_root=workspace).execute(
        parse_command(["issue", "feedback", "--id", "5", "--approve"])
    )
    assert result.exit_code == 1
    assert "Approved" not in result.stdout


@pytest.mark.parametrize("action", ["retry", "rebase"])
async def test_lifecycle_intent_uses_live_registry_and_durable_control(
    workspace, monkeypatch, action
):
    from orchestratord.intent import Intent

    monkeypatch.setattr(
        "orchestratord.commands.models.AUDIT_LOG", workspace / "audit.jsonl"
    )
    registry = IssueRegistry(workspace / ".orchestratord_issue_registry.json")
    record = registry.get("5")
    record.pr_number = "42"
    record.branch_name = "feature/5"
    registry._save()
    runtime = SimpleNamespace(_registry=registry, tracker=None)
    service = OrchestratorCommandService(
        workspace_root=workspace, runtime_supplier=lambda: runtime
    )
    argv = ["issue", action, "--id", "5", "--reason", "operator request"]
    if action == "retry":
        argv += ["--mode", "reset"]
    result = await service.execute(parse_command(argv))
    assert result.exit_code == 0
    assert registry.get("5").intent == (
        Intent.RETRY if action == "retry" else Intent.REBASE
    )
    assert (
        "operator request"
        in (workspace / ".orchestrator_control" / f"{action}_5.control").read_text()
    )
    assert (workspace / "audit.jsonl").exists()


def test_cli_prints_context_before_confirmation(workspace, monkeypatch, capsys):
    from orchestratord.cli.issue import run

    monkeypatch.setenv("ORCHESTRATORD_WORKSPACE_ROOT", str(workspace))
    prompts = []

    def confirm(prompt):
        prompts.append(prompt)
        assert "not currently running" in capsys.readouterr().err
        return "n"

    monkeypatch.setattr("builtins.input", confirm)
    assert run(parse_command(["issue", "stop", "--id", "5"]).namespace()) == 0
    assert prompts
    assert "Stop cancelled" in capsys.readouterr().out
    assert not (workspace / ".orchestrator_control").exists()
