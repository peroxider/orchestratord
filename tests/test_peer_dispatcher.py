"""PeerMessageDispatcher unit tests (DESIGN §6.1; D18, D19, NG4, R12).

The dispatcher is persistence-free by design: the route handler passes an
``execute`` coroutine, so every wire behavior — dedup, ordering, the
auto-schedule gate and the turn ceiling — is observable here without a
database.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from orchestratord.config.schema import PeerConfig
from orchestratord.peer.dispatcher import PeerMessageDispatcher


def _cfg(**overrides) -> PeerConfig:
    return PeerConfig(**overrides)


class _FakeClock:
    """Controllable ``time.monotonic`` replacement."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_dispatch_executes_and_caches() -> None:
    calls: list[int] = []

    async def execute() -> dict:
        calls.append(1)
        return {"n": len(calls)}

    d = PeerMessageDispatcher(config=_cfg())
    outcome = await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", execute=execute
    )
    assert (outcome.duplicate, outcome.out_of_order, outcome.scheduled) == (
        False,
        False,
        False,
    )
    assert d.cached_result("orch-A", "m1") == {"n": 1}


async def test_dedup_replay_answers_from_cache_without_execute() -> None:
    calls: list[int] = []

    async def execute() -> dict:
        calls.append(1)
        return {"seq": len(calls)}

    d = PeerMessageDispatcher(config=_cfg())
    await d.dispatch_message(orch_id="orch-A", msg_id="m1", execute=execute)
    outcome = await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", execute=execute
    )
    assert outcome.duplicate is True
    assert calls == [1]  # D18: the re-delivery never re-executes
    assert d.cached_result("orch-A", "m1") == {"seq": 1}


async def test_dedup_window_expires_with_clock() -> None:
    clock = _FakeClock()
    d = PeerMessageDispatcher(
        config=_cfg(dedup_window_seconds=30.0), clock=clock
    )
    await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", execute=_ok({"v": 1})
    )
    clock.now += 31.0  # past the D18 window
    assert d.cached_result("orch-A", "m1") is None
    outcome = await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", execute=_ok({"v": 2})
    )
    assert outcome.duplicate is False  # re-delivered after expiry re-executes


async def test_dedup_key_includes_orch_id() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    await d.dispatch_message(orch_id="orch-A", msg_id="m1", execute=_ok("a"))
    assert d.cached_result("orch-B", "m1") is None


async def test_ordering_none_is_always_in_order() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    assert d.check_ordering("orch-A", "s1", None) is True


async def test_ordering_sequential_chain_is_in_order() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    assert d.check_ordering("orch-A", "s1", "1") is True
    assert d.check_ordering("orch-A", "s1", 2) is True


async def test_ordering_gap_warns_and_marks_out_of_order(
    caplog: pytest.LogCaptureFixture,
) -> None:
    d = PeerMessageDispatcher(config=_cfg())
    with caplog.at_level(
        logging.WARNING, logger="orchestratord.peer.dispatcher"
    ):
        assert d.check_ordering("orch-A", "s1", "1") is True
        assert d.check_ordering("orch-A", "s1", "5") is False  # D19 gap
    assert "D19 ordering gap" in caplog.text
    # The chain jumps to the observed seq, so the next adjacent frame is fine.
    assert d.check_ordering("orch-A", "s1", "6") is True


async def test_ordering_repeat_is_out_of_order() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    d.check_ordering("orch-A", "s1", "3")
    assert d.check_ordering("orch-A", "s1", "3") is False


async def test_ordering_non_integer_is_out_of_order() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    assert d.check_ordering("orch-A", "s1", "not-a-seq") is False


async def test_ordering_is_per_session_and_per_peer() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    assert d.check_ordering("orch-A", "s1", "1") is True
    # A different session (D19: cross-session ordering is undefined) and a
    # different peer both start their own independent chains.
    assert d.check_ordering("orch-A", "s2", "1") is True
    assert d.check_ordering("orch-B", "s1", "1") is True


async def test_dispatch_carries_out_of_order_flag() -> None:
    d = PeerMessageDispatcher(config=_cfg())
    await d.dispatch_message(
        orch_id="orch-A",
        msg_id="m1",
        session_id="s1",
        ordering="1",
        execute=_ok(None),
    )
    outcome = await d.dispatch_message(
        orch_id="orch-A",
        msg_id="m2",
        session_id="s1",
        ordering="9",  # gap
        execute=_ok(None),
    )
    assert outcome.out_of_order is True


async def test_auto_schedule_default_off_never_schedules() -> None:
    scheduled: list[tuple[str, dict]] = []

    async def scheduler(session_id: str, payload: dict) -> None:
        scheduled.append((session_id, payload))

    d = PeerMessageDispatcher(
        config=_cfg(auto_schedule=False), turn_scheduler=scheduler
    )
    outcome = await d.dispatch_message(
        orch_id="orch-A",
        msg_id="m1",
        session_id="s1",
        payload={"content": "hi"},
        execute=_ok(None),
    )
    assert outcome.scheduled is False
    await d.wait_for_turns()
    assert scheduled == []  # NG4 default: 落库 only


async def test_auto_schedule_on_dispatches_turn() -> None:
    scheduled: list[tuple[str, dict]] = []

    async def scheduler(session_id: str, payload: dict) -> None:
        scheduled.append((session_id, payload))

    d = PeerMessageDispatcher(
        config=_cfg(auto_schedule=True), turn_scheduler=scheduler
    )
    outcome = await d.dispatch_message(
        orch_id="orch-A",
        msg_id="m1",
        session_id="s1",
        payload={"content": "hi"},
        execute=_ok(None),
    )
    assert outcome.scheduled is True
    await d.wait_for_turns()
    assert scheduled == [("s1", {"content": "hi"})]


async def test_failed_turn_does_not_raise_out_of_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(session_id: str, payload: dict) -> None:
        raise RuntimeError("agent backend exploded")

    d = PeerMessageDispatcher(
        config=_cfg(auto_schedule=True), turn_scheduler=boom
    )
    outcome = await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", session_id="s1", execute=_ok(None)
    )
    assert outcome.scheduled is True
    await d.wait_for_turns()
    assert "peer auto-scheduled turn failed" in caplog.text


async def test_turn_ceiling_blocks_excess_schedules_r12() -> None:
    release = asyncio.Event()

    async def slow_scheduler(session_id: str, payload: dict) -> None:
        await release.wait()

    d = PeerMessageDispatcher(
        config=_cfg(auto_schedule=True, max_concurrent_peer_turns=1),
        turn_scheduler=slow_scheduler,
    )
    first = await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", session_id="s1", execute=_ok(None)
    )
    second = await d.dispatch_message(
        orch_id="orch-A", msg_id="m2", session_id="s1", execute=_ok(None)
    )
    assert first.scheduled is True
    assert second.scheduled is False  # R12: persisted but not scheduled
    release.set()
    await d.wait_for_turns()
    third = await d.dispatch_message(
        orch_id="orch-A", msg_id="m3", session_id="s1", execute=_ok(None)
    )
    assert third.scheduled is True  # slot released → scheduling resumes
    await d.wait_for_turns()


def test_set_auto_schedule_toggle() -> None:
    d = PeerMessageDispatcher(config=_cfg(auto_schedule=False))
    assert d.auto_schedule is False
    d.set_auto_schedule(True)
    assert d.auto_schedule is True
    d.set_auto_schedule(False)
    assert d.auto_schedule is False


async def test_dispatch_on_dedup_hit_skips_execute_and_schedule() -> None:
    calls: list[int] = []

    async def execute() -> dict:
        calls.append(1)
        return {"dup": False}

    async def scheduler(session_id: str, payload: dict) -> None:
        raise AssertionError("no turn on a D18 dedup hit")

    d = PeerMessageDispatcher(
        config=_cfg(auto_schedule=True), turn_scheduler=scheduler
    )
    await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", session_id="s1", execute=execute
    )
    outcome = await d.dispatch_message(
        orch_id="orch-A", msg_id="m1", session_id="s1", execute=execute
    )
    assert outcome.duplicate is True
    assert outcome.scheduled is False
    assert calls == [1]


# -- helpers --


def _ok(result):
    async def execute():
        return result

    return execute
