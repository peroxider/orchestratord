"""Notification adapter + service tests (§7.5, no live provider).

Uses ``httpx.MockTransport`` so the Slack / Lark adapters are exercised against
a recorded handler without a network call, and a fake adapter to assert the
service's best-effort ``deliver`` contract (True on success, False on a failed
POST / unknown provider).
"""
from __future__ import annotations

import httpx

from orchestratord.notifications import (
    LarkAdapter,
    NotificationService,
    SlackAdapter,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_slack_payload_shape() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = request.content
        return httpx.Response(200)

    await SlackAdapter(_client(handler)).send(
        webhook_url="https://hooks.slack.com/x", text="hello"
    )
    assert captured["url"] == "https://hooks.slack.com/x"
    assert captured["json"] == b'{"text":"hello"}'


async def test_lark_payload_shape() -> None:
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = request.content
        return httpx.Response(200)

    await LarkAdapter(_client(handler)).send(
        webhook_url="https://open.feishu.cn/hook/x", text="hi"
    )
    assert captured["json"] == b'{"msg_type":"text","content":{"text":"hi"}}'


class _FakeAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.provider = "slack"
        self.calls: list[dict] = []
        self._fail = fail

    async def send(self, *, webhook_url: str, text: str) -> None:
        self.calls.append({"webhook_url": webhook_url, "text": text})
        if self._fail:
            raise httpx.ConnectError("boom")


async def test_service_delivers_via_adapter() -> None:
    fake = _FakeAdapter()
    service = NotificationService({"slack": fake})
    assert await service.deliver(
        provider="slack", webhook_url="https://h/x", text="hi"
    ) is True
    assert fake.calls == [{"webhook_url": "https://h/x", "text": "hi"}]


async def test_service_returns_false_on_failure() -> None:
    service = NotificationService({"slack": _FakeAdapter(fail=True)})
    assert await service.deliver(
        provider="slack", webhook_url="https://h/x", text="hi"
    ) is False


async def test_service_returns_false_on_unknown_provider() -> None:
    service = NotificationService({"slack": _FakeAdapter()})
    assert await service.deliver(
        provider="telegram", webhook_url="https://h/x", text="hi"
    ) is False
