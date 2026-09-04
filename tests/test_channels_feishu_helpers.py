"""Feishu helper-module tests: approval cards, onboarding, SDK factory, registry.

Merged from ClawCodex ``tests/services/channels/test_feishu_cards.py``,
``test_feishu_onboarding.py``, ``test_feishu_sdk.py`` and
``test_feishu_registry.py``.
"""

from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import socket
import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from orchestratord.channels.feishu_cards import (
    ApprovalCardManager,
    build_permission_card,
    build_resolved_permission_card,
)
from orchestratord.ipc.models import MessageSemantics

_FAKE_PUBLIC_IP = "8.8.8.8"


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep webhook-channel construction DNS-independent (see test_feishu.py)."""

    def _fake_getaddrinfo(
        host: str,
        port: int | str | None,
        *args: Any,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_FAKE_PUBLIC_IP, port or 443)),
            (socket.AF_INET, socket.SOCK_DGRAM, 17, "", (_FAKE_PUBLIC_IP, port or 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


# ---------------------------------------------------------------------------
# permission approval cards (from test_feishu_cards.py)
# ---------------------------------------------------------------------------


def _event(*, user="ou_allowed", chat="oc_chat", approval_id="ap1", nonce="n1", choice="y"):
    return {
        "event": {
            "operator": {"open_id": user},
            "context": {"open_chat_id": chat},
            "action": {
                "value": {
                    "orchestratord_action": "permission_approval",
                    "approval_id": approval_id,
                    "nonce": nonce,
                    "choice": choice,
                }
            },
        }
    }


def test_permission_metadata_renders_feishu_card() -> None:
    payload = build_permission_card(
        message="orchestratord wants to use Bash.",
        suggestion="Check the command first.",
        options=[
            {"value": "y", "label": "允许"},
            {"value": "n", "label": "拒绝"},
        ],
        approval_id="ap1",
        nonce="n1",
    )

    assert payload["msg_type"] == "interactive"
    card = payload["content"]
    assert card["header"]["title"]["content"] == "权限审批"
    assert card["elements"][0]["text"]["content"] == "orchestratord wants to use Bash."
    actions = card["elements"][-1]["actions"]
    assert actions[0]["text"]["content"] == "允许"
    assert actions[0]["value"]["approval_id"] == "ap1"
    assert actions[0]["value"]["choice"] == "y"


def test_resolved_permission_card_removes_action_buttons() -> None:
    card = build_resolved_permission_card(choice="y", operator_open_id="ou_allowed")

    assert card["header"]["template"] == "green"
    assert "已允许" in card["header"]["title"]["content"]
    assert all(element.get("tag") != "action" for element in card["elements"])


def test_resolved_permission_card_treats_session_choice_as_allowed() -> None:
    card = build_resolved_permission_card(choice="s", allowed=True)

    assert card["header"]["template"] == "green"
    assert "已允许" in card["header"]["title"]["content"]


def test_card_click_from_allowed_user_emits_approval_inbound() -> None:
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "n"},
        ttl_seconds=60,
    )

    inbound = manager.resolve_action(_event())

    assert inbound is not None
    assert inbound.origin == "feishu:dm:cli_app:ou_allowed"
    assert inbound.text == "y"
    assert inbound.context_token == "oc_chat"
    assert inbound.from_user_id == "ou_allowed"
    assert inbound.semantic is MessageSemantics.APPROVAL
    assert inbound.semantic_tags == ["approval"]
    assert inbound.raw["source"] == "feishu_card_action"
    assert "ap1" not in manager.pending


def test_card_click_session_choice_carries_explicit_allow_decision() -> None:
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "s", "n"},
        allow_choices={"y", "s"},
        ttl_seconds=60,
    )

    inbound = manager.resolve_action(_event(choice="s"))

    assert inbound is not None
    assert inbound.text == "s"
    assert inbound.raw["decision"] == "allow"


def test_card_click_from_sdk_model_object_emits_approval_inbound() -> None:
    """The official SDK passes P2CardActionTrigger-style objects, not dicts."""
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "n"},
        ttl_seconds=60,
    )
    payload = SimpleNamespace(
        event=SimpleNamespace(
            operator=SimpleNamespace(open_id="ou_allowed"),
            context=SimpleNamespace(open_chat_id="oc_chat"),
            action=SimpleNamespace(
                value={
                    "orchestratord_action": "permission_approval",
                    "approval_id": "ap1",
                    "nonce": "n1",
                    "choice": "y",
                }
            ),
        )
    )

    inbound = manager.resolve_action(payload)

    assert inbound is not None
    assert inbound.text == "y"
    assert inbound.context_token == "oc_chat"
    assert inbound.from_user_id == "ou_allowed"


def test_card_click_from_other_user_is_rejected() -> None:
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "n"},
        ttl_seconds=60,
    )

    assert manager.resolve_action(_event(user="ou_other")) is None


def test_card_click_wrong_chat_is_rejected() -> None:
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "n"},
        ttl_seconds=60,
    )

    assert manager.resolve_action(_event(chat="oc_other")) is None


def test_card_click_duplicate_token_is_ignored() -> None:
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "n"},
        ttl_seconds=60,
    )

    assert manager.resolve_action(_event()) is not None
    assert manager.resolve_action(_event()) is None


def test_card_click_expired_approval_is_ignored() -> None:
    now = [100.0]
    manager = ApprovalCardManager(clock=lambda: now[0])
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_allowed",
        chat_id="oc_chat",
        allowed_user_open_id="ou_allowed",
        choices={"y", "n"},
        ttl_seconds=10,
    )
    now[0] = 111.0

    assert manager.resolve_action(_event()) is None


def test_card_click_empty_allowlist_accepts_any_user_in_same_chat() -> None:
    """V1 opens to all p2p users: empty allowlist relies on chat_id match."""
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_anyone",
        chat_id="oc_chat",
        allowed_user_open_id="",
        choices={"y", "n"},
        ttl_seconds=60,
    )

    inbound = manager.resolve_action(_event(user="ou_anyone"))

    assert inbound is not None
    assert inbound.from_user_id == "ou_anyone"


def test_card_click_empty_allowlist_still_rejects_wrong_chat() -> None:
    manager = ApprovalCardManager(clock=lambda: 100.0)
    manager.create_pending(
        approval_id="ap1",
        nonce="n1",
        origin="feishu:dm:cli_app:ou_anyone",
        chat_id="oc_chat",
        allowed_user_open_id="",
        choices={"y", "n"},
        ttl_seconds=60,
    )

    assert manager.resolve_action(_event(user="ou_anyone", chat="oc_other")) is None


# ---------------------------------------------------------------------------
# QR scan-to-create onboarding (from test_feishu_onboarding.py)
# ---------------------------------------------------------------------------

from orchestratord.channels.feishu_onboarding import qr_register


def test_feishu_qr_register_uses_sdk_registration_without_bot_probe() -> None:
    calls: dict[str, Any] = {}
    rendered: list[str] = []

    def _register_app(**kwargs: Any) -> dict[str, Any]:
        calls.update(kwargs)
        kwargs["on_qr_code"](
            {
                "url": "https://accounts.feishu.cn/verify?from=sdk&tp=sdk"
                "&source=python-sdk%2Forchestratord",
                "expire_in": 60,
            }
        )
        return {
            "client_id": "cli_app",
            "client_secret": "secret",
            "user_info": {"open_id": "ou_operator", "tenant_brand": "lark"},
        }

    result = qr_register(
        initial_domain="feishu",
        timeout_seconds=30,
        register_app=_register_app,
        render_qr=lambda url: rendered.append(url) or True,
    )

    assert calls["source"] == "orchestratord"
    assert calls["domain"] == "https://accounts.feishu.cn"
    assert calls["lark_domain"] == "https://accounts.larksuite.com"
    assert calls["cancel_event"].is_set() is False
    assert rendered == [
        "https://accounts.feishu.cn/verify?from=sdk&tp=sdk&source=python-sdk%2Forchestratord"
    ]
    assert result == {
        "app_id": "cli_app",
        "app_secret": "secret",
        "domain": "lark",
        "open_id": "ou_operator",
    }


def test_feishu_qr_register_returns_none_on_sdk_denied_scan() -> None:
    def _register_app(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("access_denied: user denied")

    result = qr_register(
        register_app=_register_app,
        render_qr=lambda _url: True,
    )

    assert result is None


def test_feishu_qr_register_rejects_credentials_without_scanner_open_id() -> None:
    result = qr_register(
        register_app=lambda **_kwargs: {
            "client_id": "cli_app",
            "client_secret": "secret",
        },
        render_qr=lambda _url: True,
    )

    assert result is None


def test_sdk_register_app_avoids_lark_oapi_root_import(monkeypatch, tmp_path) -> None:
    import orchestratord.channels.feishu_onboarding as onboarding

    package = tmp_path / "lark_oapi"
    registration = package / "scene" / "registration"
    registration.mkdir(parents=True)
    (package / "__init__.py").write_text(
        'raise AssertionError("root package should not be imported")\n',
        encoding="utf-8",
    )
    (package / "scene" / "__init__.py").write_text("", encoding="utf-8")
    (registration / "errors.py").write_text(
        "class RegisterAppError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    (registration / "__init__.py").write_text(
        "from .errors import RegisterAppError\n\n"
        "def register_app(**kwargs):\n"
        '    kwargs["on_qr_code"]({"url": "https://qr.example"})\n'
        '    return {"client_id": "cli_fast", "client_secret": "secret"}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = importlib.machinery.ModuleSpec("lark_oapi", loader=None, is_package=True)
    spec.origin = str(package / "__init__.py")
    spec.submodule_search_locations = [str(package)]
    original_find_spec = importlib.util.find_spec
    previous_modules = {
        name: module for name, module in sys.modules.items() if name.startswith("lark_oapi")
    }
    for name in list(previous_modules):
        monkeypatch.delitem(sys.modules, name, raising=False)

    def _find_spec(name: str, package_name: str | None = None) -> Any:
        if name == "lark_oapi":
            return spec
        return original_find_spec(name, package_name)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    qr: list[dict[str, Any]] = []

    try:
        result = onboarding._sdk_register_app(on_qr_code=qr.append)
    finally:
        for name in [name for name in sys.modules if name.startswith("lark_oapi")]:
            sys.modules.pop(name, None)
        sys.modules.update(previous_modules)

    assert qr == [{"url": "https://qr.example"}]
    assert result == {"client_id": "cli_fast", "client_secret": "secret"}


def test_sdk_register_app_retries_transient_ssl_error_inside_registration(
    monkeypatch, tmp_path
) -> None:
    import orchestratord.channels.feishu_onboarding as onboarding

    package = tmp_path / "lark_oapi"
    registration = package / "scene" / "registration"
    registration.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "scene" / "__init__.py").write_text("", encoding="utf-8")
    (registration / "errors.py").write_text(
        "class RegisterAppError(Exception):\n    pass\n",
        encoding="utf-8",
    )
    (registration / "__init__.py").write_text(
        "class SSLError(Exception):\n    pass\n"
        "class Exceptions:\n"
        "    SSLError = SSLError\n"
        "    ConnectionError = SSLError\n"
        "    Timeout = SSLError\n"
        "class Requests:\n"
        "    exceptions = Exceptions\n"
        "    def __init__(self):\n"
        "        self.calls = 0\n"
        "    def post(self, *args, **kwargs):\n"
        "        self.calls += 1\n"
        "        if self.calls == 1:\n"
        '            raise self.exceptions.SSLError("eof")\n'
        '        return {"ok": True, "timeout": kwargs.get("timeout")}\n'
        "requests = Requests()\n\n"
        "def register_app(**kwargs):\n"
        '    kwargs["on_qr_code"]({"url": "https://qr.example"})\n'
        '    response = requests.post("https://accounts.feishu.cn/oauth/v1/app/registration")\n'
        '    return {"client_id": "cli_fast", "client_secret": "secret", "response": response}\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    spec = importlib.machinery.ModuleSpec("lark_oapi", loader=None, is_package=True)
    spec.origin = str(package / "__init__.py")
    spec.submodule_search_locations = [str(package)]
    original_find_spec = importlib.util.find_spec
    previous_modules = {
        name: module for name, module in sys.modules.items() if name.startswith("lark_oapi")
    }
    for name in list(previous_modules):
        monkeypatch.delitem(sys.modules, name, raising=False)

    def _find_spec(name: str, package_name: str | None = None) -> Any:
        if name == "lark_oapi":
            return spec
        return original_find_spec(name, package_name)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)

    try:
        result = onboarding._sdk_register_app(on_qr_code=lambda _info: None)
        registration_module = sys.modules["lark_oapi.scene.registration"]
    finally:
        for name in [name for name in sys.modules if name.startswith("lark_oapi")]:
            sys.modules.pop(name, None)
        sys.modules.update(previous_modules)

    assert result["response"] == {"ok": True, "timeout": 30}
    assert registration_module.requests.calls == 2


def test_feishu_qr_register_uses_sdk_status_for_domain_switch() -> None:
    def _register_app(**kwargs: Any) -> dict[str, Any]:
        kwargs["on_status_change"]({"status": "domain_switched"})
        return {
            "client_id": "cli_lark",
            "client_secret": "secret",
            "user_info": {"open_id": "ou_operator"},
        }

    result = qr_register(
        register_app=_register_app,
        render_qr=lambda _url: True,
    )

    assert result["domain"] == "lark"


# ---------------------------------------------------------------------------
# SDK factory wiring (from test_feishu_sdk.py)
# ---------------------------------------------------------------------------

from orchestratord.channels.feishu_sdk import (
    _ensure_private_ws_loop,
    build_feishu_channel,
)
from orchestratord.channels.feishu_settings import FeishuAppSettings


def _sdk_settings(**overrides) -> FeishuAppSettings:
    values = {
        "channel_id": "feishu",
        "connection_mode": "websocket",
        "app_id": "cli_app",
        "app_secret": "secret",
        "encrypt_key": "encrypt-key",
        "verification_token": "verification-token",
    }
    values.update(overrides)
    return FeishuAppSettings(**values)


def test_build_feishu_channel_passes_event_security_fields_to_sdk() -> None:
    pytest.importorskip("lark_oapi", reason="gateway-feishu extras (lark-oapi) not installed")

    channel = build_feishu_channel(_sdk_settings())

    assert channel.config.encrypt_key == "encrypt-key"
    assert channel.config.verification_token == "verification-token"


@pytest.mark.asyncio
async def test_build_feishu_channel_replaces_sdk_ws_loop_when_imported_on_running_loop(
    monkeypatch,
) -> None:
    pytest.importorskip("lark_oapi", reason="gateway-feishu extras (lark-oapi) not installed")
    from lark_oapi.ws import client as ws_client

    running_loop = asyncio.get_running_loop()
    original_loop = ws_client.loop
    monkeypatch.setattr(ws_client, "loop", running_loop)

    try:
        build_feishu_channel(_sdk_settings())

        assert ws_client.loop is not running_loop
        assert not ws_client.loop.is_running()
        assert not ws_client.loop.is_closed()
    finally:
        monkeypatch.setattr(ws_client, "loop", original_loop)


def test_feishu_sdk_ws_runtime_preserves_websocket_env_proxy(monkeypatch) -> None:
    root_module = ModuleType("lark_oapi")
    ws_module = ModuleType("lark_oapi.ws")
    client_module = ModuleType("lark_oapi.ws.client")
    loop = asyncio.new_event_loop()

    async def _connect(_uri, *, proxy=True):
        return proxy

    client_module.loop = loop
    client_module.websockets = type("Websockets", (), {"connect": _connect})
    client_module._ws_connect_kwargs = lambda: {"proxy": None}
    ws_module.client = client_module
    monkeypatch.setitem(sys.modules, "lark_oapi", root_module)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws", ws_module)
    monkeypatch.setitem(sys.modules, "lark_oapi.ws.client", client_module)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    try:
        _ensure_private_ws_loop()

        assert client_module._ws_connect_kwargs() == {"proxy": True}
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# registry mode dispatch (from test_feishu_registry.py; adapted — the
# websocket app adapter is gateway-owned, not registry-owned)
# ---------------------------------------------------------------------------

from orchestratord.channels.models import ChannelConfig, ChannelType
from orchestratord.channels.registry import (
    WebhookChannelAdapter,
    build_default_registry,
)


def test_feishu_registry_uses_webhook_when_legacy_webhook_url_present() -> None:
    cfg = ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/abcdef",
        name="feishu",
    )

    adapter = build_default_registry().create(cfg)

    assert isinstance(adapter, WebhookChannelAdapter)


def test_feishu_registry_websocket_mode_is_gateway_managed() -> None:
    """``connection_mode=websocket`` is built by ``MessageGateway._build_adapter``
    (lazy gateway-feishu extras import), so the registry itself rejects it —
    building it here would defeat the base-environment lazy-import boundary."""
    cfg = ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="",
        name="feishu",
        extra={
            "connection_mode": "websocket",
            "app_id": "cli_app",
            "app_secret": "secret",
            "allowed_user_open_id": "ou_allowed",
        },
    )

    with pytest.raises(ValueError, match="websocket"):
        build_default_registry().create(cfg)


def test_feishu_registry_rejects_unknown_mode() -> None:
    cfg = ChannelConfig(
        type=ChannelType.FEISHU,
        webhook_url="",
        name="feishu",
        extra={"connection_mode": "sideways"},
    )

    with pytest.raises(ValueError, match="connection_mode"):
        build_default_registry().create(cfg)
