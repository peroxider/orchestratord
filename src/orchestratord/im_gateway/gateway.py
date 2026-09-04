"""MessageGateway — the unified IM entry point.

Facade over the inbound dispatcher, outbound dispatcher, session router,
binding policy, capability gate, and reliability store. External callers
(Community Radar, Orchestrator event sink, REPL wrapper) use this API;
it does not reverse-import Orchestrator.

P1 ships the skeleton with working outbound send/broadcast, capability
fail-closed, session routing/binding, and a ``reload_channel`` hook for
``orchestratord gateway restart <channel>``. Inbound adapter lifecycle
(WeChat) lands in P2; full reliability hardening in P4; six-semantics
in P5.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from pathlib import Path

from orchestratord.channels.capabilities import ChannelAdapter, ChannelCapability
from orchestratord.channels.registry import (
    ChannelAdapterRegistry,
    build_default_registry,
)
from orchestratord.channels.results import SendStatus
from orchestratord.ipc.models import AckReceipt, InboundMessage, OutboundMessage

from .binding import BindingEntry, BindingPolicy
from .capability_gate import CapabilityGate
from .config import GatewayConfig
from .dispatcher import InboundDispatcher, InboundHandler
from .outbound import OutboundDispatcher
from .processing_status import ProcessingStatusManager
from .router import SessionRouter
from .store import ReliabilityStore

logger = logging.getLogger(__name__)


class MessageGateway:
    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        registry: ChannelAdapterRegistry | None = None,
        store: ReliabilityStore | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        self.config = _normalize_config_channels(config or GatewayConfig())
        # On-disk config source for transactional channel reloads. ``None``
        # keeps load_config()'s default channels.yaml path.
        self._config_path = Path(config_path).expanduser() if config_path is not None else None
        self.registry = registry or build_default_registry()
        self.store = store or ReliabilityStore(self.config.state_dir, self.config.reliability)
        self.binding = BindingPolicy(auditor=self._audit_binding)
        self.router = SessionRouter(self.binding, self.store)
        self.gate = CapabilityGate(self.registry)
        self.outbound = OutboundDispatcher(self.registry, self.gate, self.store, self.config)
        self.processing_status = ProcessingStatusManager(self.registry)
        self.inbound = InboundDispatcher(
            self.store,
            self.router,
            command_allowlists=self.config.command_allowlists,
            processing_status=self.processing_status,
        )
        self._inbound_adapters: list = []
        self._running = False
        self._stop_lock = asyncio.Lock()
        self._load_channels()

    def _build_adapter(self, cfg) -> ChannelAdapter | None:
        """Build one adapter from a ChannelConfig (gateway-owned deps for WeChat)."""
        from pathlib import Path

        from orchestratord.channels.models import ChannelType

        if cfg.type is ChannelType.WECHAT:
            try:
                from orchestratord.channels.transport import UrllibChannelTransport
                from orchestratord.channels.wechat_ilink import (
                    WeChatIlinkAuthStore,
                    WeChatIlinkChannelAdapter,
                )
            except ImportError as exc:
                logger.warning(
                    "gateway: channel type %r requires the gateway-wechat extras; "
                    "the WeChat iLink adapter module is not installed: %s",
                    cfg.type.value,
                    exc,
                )
                return None
            extra = cfg.extra or {}
            state_dir = Path(self.config.state_dir).expanduser()
            auth_path = _wechat_state_file(state_dir, cfg.name, "auth")
            adapter = WeChatIlinkChannelAdapter(
                cfg,
                auth_store=WeChatIlinkAuthStore(
                    auth_path,
                    secret_env=self.config.reliability.secret_encryption_env,
                ),
                store=self.store,
                transport=UrllibChannelTransport(),
                allowed_users=extra.get("allowed_users"),
                account_id=extra.get("account_id", "default"),
                base_url=extra.get("base_url", "https://ilinkai.weixin.qq.com"),
                long_poll_timeout_ms=extra.get("long_poll_timeout_ms", 35000),
                max_consecutive_failures=extra.get("max_consecutive_failures", 10),
            )
            adapter.load_credentials()
            return adapter
        if cfg.type is ChannelType.FEISHU:
            extra = cfg.extra or {}
            mode = str(extra.get("connection_mode") or "").strip().lower()
            if mode == "websocket":
                try:
                    from orchestratord.channels.feishu_app import (
                        FeishuAppChannelAdapter,
                    )
                except ImportError as exc:
                    logger.warning(
                        "gateway: channel type %r with connection_mode=websocket requires "
                        "the gateway-feishu extras; the Feishu app adapter module is not "
                        "installed: %s",
                        cfg.type.value,
                        exc,
                    )
                    return None
                return FeishuAppChannelAdapter(cfg, sender_store=self.store)
        try:
            return self.registry.create(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gateway: failed to build adapter for channel %s (%s): %s",
                cfg.name,
                cfg.type.value,
                exc,
            )
            return None

    def _attach_inbound(self, adapter) -> None:
        if adapter is not None and adapter.capabilities.has(ChannelCapability.INBOUND_POLLING):
            adapter.set_inbound_handler(self._on_inbound)
            self._inbound_adapters.append(adapter)

    def _load_channels(self) -> None:
        """Build adapters from config; WeChat needs gateway-owned deps."""
        for cfg in self.config.channels:
            if not cfg.enabled:
                continue
            adapter = self._build_adapter(cfg)
            if adapter is not None:
                self.registry.register(adapter)
                self._attach_inbound(adapter)
                logger.info("gateway loaded channel: %s (%s)", cfg.name, cfg.type.value)
            else:
                logger.warning("gateway failed to build adapter for channel: %s", cfg.name)

    async def _on_inbound(self, message) -> AckReceipt:
        """Inbound hook called by channel adapters (WeChat poller)."""
        ack = await self.inbound.process(message)
        await self._notify_sender_for_ack(message, ack)
        return ack

    async def _notify_sender_for_ack(self, message: InboundMessage, ack: AckReceipt) -> None:
        if not ack.notify_user or not ack.message:
            return
        channel = message.channel
        target = message.from_user_id or message.context_token
        if not channel or not target:
            logger.warning(
                "gateway ack notify skipped: channel=%r target=%r origin=%s",
                channel,
                target,
                message.origin[:32],
            )
            return
        try:
            result = await self.outbound.send(
                OutboundMessage(
                    text=ack.message,
                    channel=channel,
                    target=target,
                    context_token=message.context_token,
                    markdown=False,
                    idempotency_key=f"inbound-ack:{ack.delivery_id}",
                    semantic_tags=["gateway_notice"],
                    metadata={"source": "inbound_ack", "ack_layer": ack.layer.value},
                )
            )
        except Exception:
            logger.exception("gateway ack notify failed origin=%s", message.origin[:32])
            return
        if result is not None and getattr(result, "ok", True) is False:
            logger.warning(
                "gateway ack notify send rejected channel=%s origin=%s message=%s",
                channel,
                message.origin[:32],
                getattr(result, "message", "") or "",
            )

    # -- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            logger.debug("gateway already running")
            return
        self._running = True
        logger.info(
            "gateway starting: %d inbound adapter(s), %d registered channel(s)",
            len(self._inbound_adapters),
            len(self.registry.names()),
        )
        # Start only adapters that declare inbound_polling (P2 wires WeChat).
        for adapter in self._inbound_adapters:
            await adapter.start()
        # Recover the durable outbox: re-send records a previous process
        # left without a terminal status (P4 outbox recovery). Best-effort —
        # a replay failure must never block startup.
        try:
            await self._replay_pending_outbox()
        except Exception:
            logger.exception("gateway outbox replay failed during startup")
        logger.info("gateway started")

    async def stop(self) -> None:
        async with self._stop_lock:
            if not self._running:
                return
            self._running = False
            logger.info("gateway stopping")
            for adapter in self._inbound_adapters:
                try:
                    await adapter.stop()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "gateway: adapter stop failed for %s: %s",
                        getattr(adapter, "channel_id", "?"),
                        exc,
                    )
            self.outbound.clear_deferred()
            logger.info("gateway stopped")

    @property
    def running(self) -> bool:
        return self._running

    async def wait_channels_ready(self, timeout: float = 15.0) -> dict[str, str]:
        """Wait for inbound adapters to report a connected/healthy state.

        Polls each inbound adapter's ``health_check()`` until its
        ``account_status`` indicates the channel is live (or degraded-but-up),
        or ``timeout`` seconds elapse. Returns a ``{channel_id: status}`` map.
        Adapters that start non-blocking (e.g. Feishu WS connect loop) flip to
        ``connected`` asynchronously; this gives ``serve()`` a bounded window to
        confirm connectivity before declaring startup done, so a stuck connect
        surfaces as a degraded status rather than a silent invisible daemon.
        """
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout
        result: dict[str, str] = {}
        while True:
            result = {}
            all_ready = True
            for adapter in self._inbound_adapters:
                cid = getattr(adapter, "channel_id", "?")
                try:
                    health = await adapter.health_check()
                    status = str(getattr(health, "account_status", "") or "")
                except Exception as exc:  # noqa: BLE001
                    status = f"health_error:{exc!s}"
                result[cid] = status
                # "connected"/"logged_in" = ready; anything else (reconnecting,
                # credentials_missing, etc.) keeps waiting.
                if not any(
                    marker in status for marker in ("connected", "logged_in", "websocket:connected")
                ):
                    all_ready = False
            if all_ready or asyncio.get_running_loop().time() >= deadline:
                return result
            await asyncio.sleep(0.5)

    # -- inbound ---------------------------------------------------------
    def register_inbound(self, adapter) -> None:
        self._inbound_adapters.append(adapter)

    def set_handler(self, handler: InboundHandler) -> None:
        self.inbound.set_handler(handler)

    def set_push_handler(self, handler) -> None:
        """Register the IPC push callback for opt-in origins (REPL/orchestrator)."""
        self.inbound.set_push_handler(handler)

    async def receive(self, message: InboundMessage):
        """Convenience: publish to the bus and process synchronously."""
        return await self.inbound.process(message)

    # -- outbound --------------------------------------------------------
    async def send(self, message: OutboundMessage):
        return await self.outbound.send(message)

    async def broadcast(self, message: OutboundMessage, *, channels: list[str] | None = None):
        return await self.outbound.broadcast(message, channels=channels)

    # -- channel management ---------------------------------------------
    async def reload_channel(self, name: str, *, ready_timeout: float = 15.0) -> bool:
        """Rebuild one channel from the on-disk config as an async transaction.

        Steps (P4 live reload, transactional):

        1. Reload ``channels.yaml`` from disk (the ``config_path`` captured
           at construction — the same file ``serve()`` booted from). An
           invalid config, or one that no longer contains the channel,
           fails the reload with an audit record and leaves everything
           untouched.
        2. Build the new adapter via ``_build_adapter``. A failed build
           keeps the old adapter and the in-memory config as-is.
        3. If the gateway is running, start the new adapter and wait for it
           to report a connected/healthy state (bounded by ``ready_timeout``;
           a timeout is treated as degraded-but-usable, mirroring the
           daemon's startup health semantics — the adapter keeps retrying in
           the background). An exception from ``start()`` aborts the reload:
           the half-started replacement is stopped, the old adapter stays.
        4. Only after a successful build+start, atomically swap in the new
           adapter (registry entry, inbound list, and the gateway's config
           object) and stop the old adapter asynchronously — a slow stop
           never blocks the reload return.

        In-flight message safety: both adapters may briefly poll inbound
        concurrently between the new adapter's start and the old adapter's
        stop; the store's inbound dedupe absorbs any double delivery. The
        outbox preserves pending outbound sends across the swap.
        """
        from .config import load_config

        old = self.registry.get(name)
        try:
            disk_config = load_config(self._config_path)
        except Exception as exc:  # noqa: BLE001 — invalid YAML must fail the reload
            logger.warning("gateway reload: config reload failed for %r: %s", name, exc)
            self.store.audit("channel_reload_failed", channel=name, reason=f"config_load_error: {exc}")
            return False
        # serve() overrides state_dir after load_config (the YAML default
        # points at ~/.orchestratord/gateway). The runtime state dir hosts
        # the reliability store — a reload must never move it.
        disk_config.state_dir = self.config.state_dir
        disk_config = _normalize_config_channels(disk_config)
        channel_cfg = disk_config.get_channel(name)
        if channel_cfg is None:
            logger.warning("gateway reload: channel %r not found in on-disk config", name)
            self.store.audit("channel_reload_failed", channel=name, reason="channel_not_in_config")
            return False

        new_adapter = self._build_adapter(channel_cfg)
        if new_adapter is None:
            self._restore_old_adapter(name, old)
            self.store.audit("channel_reload_failed", channel=name, reason="adapter_build_failed")
            return False

        try:
            if self._running and hasattr(new_adapter, "start"):
                await new_adapter.start()
                ready = await self._wait_adapter_ready(new_adapter, ready_timeout)
                if not ready:
                    logger.warning(
                        "gateway reload: channel %s not ready after %.1fs — "
                        "degraded but usable (keeps retrying in background)",
                        name,
                        ready_timeout,
                    )
        except Exception as exc:  # noqa: BLE001 — start failure aborts the swap
            logger.warning("gateway reload: new adapter start failed for %r: %s", name, exc)
            stop = getattr(new_adapter, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except Exception:
                    logger.debug("gateway reload: cleanup stop failed for %r", name, exc_info=True)
            self._restore_old_adapter(name, old)
            self.store.audit(
                "channel_reload_failed", channel=name, reason=f"adapter_start_error: {exc}"
            )
            return False

        # -- commit: atomic replacement ----------------------------------
        self.registry.register(new_adapter)  # same channel_id → overwrite
        if old is not None and old in self._inbound_adapters:
            self._inbound_adapters.remove(old)
        self._attach_inbound(new_adapter)
        self.config = disk_config
        if old is not None:
            _schedule_adapter_stop(old)  # non-blocking; swap already committed
        self.store.audit("channel_reload", channel=name)
        logger.info("gateway channel reloaded: %s", name)
        return True

    def _restore_old_adapter(self, name: str, old: ChannelAdapter | None) -> None:
        """Undo a build-time registration so a failed reload keeps the old adapter.

        ``_build_adapter`` builds non-gateway-owned channels through
        ``registry.create()``, which registers the new adapter under the
        channel name before it is started. If the reload aborts afterwards,
        re-registering the old adapter (or removing the orphan replacement
        when no old one existed) restores the pre-reload registry state.
        """
        if old is not None:
            self.registry.register(old)
        else:
            self.registry.remove(name)

    async def _wait_adapter_ready(self, adapter, timeout: float) -> bool:
        """Wait until ``adapter`` reports a connected/healthy state.

        Mirrors :meth:`wait_channels_ready`: account statuses containing
        ``connected``/``logged_in`` are ready. A healthy adapter without an
        account status (simple outbound/webhook adapters and test fakes)
        counts as ready immediately. Anything else is polled until
        ``timeout`` elapses; the caller then treats the channel as degraded
        but usable, matching the daemon's startup health semantics.
        """
        if timeout <= 0:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            try:
                health = await adapter.health_check()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "gateway reload: health check error for %s: %s",
                    getattr(adapter, "channel_id", "?"),
                    exc,
                )
                health = None
            status = str(getattr(health, "account_status", "") or "")
            if getattr(health, "healthy", False) and not status:
                return True
            if any(marker in status for marker in ("connected", "logged_in")):
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.2)

    async def _replay_pending_outbox(self) -> None:
        """Re-send outbox records left pending by a previous process.

        Scans the durable outbox for entries without a terminal status,
        rebuilds the original :class:`OutboundMessage` from the parameters
        persisted alongside the chunk text, and re-sends it through the
        normal outbound path under the original idempotency key. A record
        that ends non-terminal again (e.g. another crash mid-replay) stays
        pending and is retried on the next startup. Single-record failures
        are marked dead and never block the rest of the backlog.

        This is at-least-once delivery: a send that succeeded right before
        a crash is re-sent, so the IM side may see a duplicate message —
        the standard outbox trade-off for recoverability.
        """
        pending = self.store.outbox_replayable()
        if not pending:
            return
        logger.info(
            "gateway outbox replay: %d pending record(s) from previous process",
            len(pending),
        )
        for entry in pending:
            key = str(entry.get("idempotency_key") or "")
            channel = str(entry.get("channel") or "")
            text = entry.get("text")
            if not key or not channel or text is None:
                # Legacy pending records (pre-recovery schema) carry only
                # payload_size — they cannot be rebuilt. Mark them dead so
                # they stop counting as recoverable.
                self.store.append_outbox(
                    {
                        "idempotency_key": key or "unknown",
                        "channel": channel,
                        "status": "dead",
                        "error": "replay_payload_missing",
                        "at": time.time(),
                    }
                )
                logger.warning(
                    "gateway outbox replay: record %s has no payload — marked dead",
                    key[:16],
                )
                continue
            try:
                message = OutboundMessage(
                    text=str(text),
                    channel=channel,
                    target=entry.get("target"),
                    context_token=entry.get("context_token"),
                    title=entry.get("title"),
                    markdown=bool(entry.get("markdown", True)),
                    metadata=entry.get("metadata"),
                    semantic_tags=list(entry.get("semantic_tags") or []),
                    idempotency_key=key,
                )
                result = await self.outbound.send(message)
            except Exception as exc:
                logger.exception("gateway outbox replay: send failed for %s", key[:16])
                self.store.append_outbox(
                    {
                        "idempotency_key": key,
                        "channel": channel,
                        "status": "dead",
                        "error": f"replay_error: {exc}",
                        "at": time.time(),
                    }
                )
                continue
            ok = getattr(result, "ok", False)
            logger.info(
                "gateway outbox replay: %s channel=%s idem=%s",
                "delivered" if ok else "failed",
                channel,
                key[:16],
            )

    # -- audit -----------------------------------------------------------
    def _audit_binding(
        self, action: str, entry: BindingEntry, previous: BindingEntry | None
    ) -> None:
        self.store.audit(
            action,
            origin=entry.origin,
            session_id=entry.target.session_id,
            host_type=entry.target.host_type,
        )
        # Schedule best-effort connection notification (async, never blocks
        # the binding transition). The auditor is called from the IPC server's
        # async handlers, so a running event loop is expected.
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._notify_connection_change(action, entry, previous))
        except RuntimeError:
            pass

    async def _notify_connection_change(
        self, action: str, entry: BindingEntry, previous: BindingEntry | None
    ) -> None:
        """Send a best-effort connection notification to all connected IM channels.

        When REPL or orchestrator connects/disconnects, every operator who has
        messaged the gateway is notified via the outbound dispatcher (e.g.
        ``"REPL已连接"`` / ``"orchestrator已断开"``). The notification is
        broadcast to every outbound channel that knows a recent sender — so
        operators on WeChat, Feishu, or any other connected channel all hear
        the connection event. All failures are logged and swallowed —
        notifications must never block binding transitions or crash the gateway.
        """
        # Broadcast to every outbound channel with a known recent sender.
        # This supersedes the origin-only routing (which hard-coded WeChat and
        # silently skipped when the WeChat adapter had no last_known_sender).
        targets = _collect_broadcast_targets(self.registry)
        if not targets:
            logger.debug(
                "connection notify: no broadcast targets for origin %s — skipping",
                (entry.origin or "")[:32],
            )
            return

        host_label = _host_label(entry.target.host_type)
        # Orchestrator 绑定目前仅能通过命令交互驱动,连接通知里附加提示,
        # 避免运营侧误以为可以像 REPL 一样自由对话。
        connected_note = (
            "，当前Orchestrator仅支持命令交互" if entry.target.host_type == "orchestrator" else ""
        )
        messages: list[str] = []
        if action == "binding_created":
            messages.append(f"{host_label}已连接{connected_note}")
        elif action == "binding_override" and previous is not None:
            if previous.connection_state == "active":
                messages.append(f"{_host_label(previous.target.host_type)}已断开")
            messages.append(f"{host_label}已连接{connected_note}")
        elif action in ("binding_offline", "binding_terminated"):
            messages.append(f"{host_label}已断开")

        for out_channel, out_target in targets:
            for text in messages:
                try:
                    result = await self.outbound.send(
                        OutboundMessage(
                            text=text, channel=out_channel, target=out_target, markdown=False
                        )
                    )
                    if result is not None and getattr(result, "ok", True) is False:
                        error_category = getattr(result, "error_category", "")
                        category_value = getattr(error_category, "value", "") or str(
                            error_category or ""
                        )
                        logger.error(
                            "connection notify: send failed for %r channel=%s category=%s message=%s",
                            text,
                            out_channel,
                            category_value,
                            getattr(result, "message", "") or "",
                        )
                        continue
                    if (
                        result is not None
                        and getattr(result, "status", None) is SendStatus.ENQUEUED
                    ):
                        raw = getattr(result, "raw", None) or {}
                        logger.info(
                            "connection notify: enqueued %r channel=%s target=%s message=%s retry_after=%s",
                            text,
                            out_channel,
                            (out_target or "")[:16] + "…"
                            if len(out_target or "") > 16
                            else out_target,
                            getattr(result, "message", "") or "",
                            raw.get("retry_after_seconds") or raw.get("retry_after") or "",
                        )
                        continue
                    logger.info(
                        "connection notify: sent %r channel=%s target=%s",
                        text,
                        out_channel,
                        (out_target or "")[:16] + "…" if len(out_target or "") > 16 else out_target,
                    )
                except Exception:  # noqa: BLE001
                    logger.error(
                        "connection notify: send failed for %r channel=%s — best-effort",
                        text,
                        out_channel,
                    )

    # -- health ----------------------------------------------------------
    async def health(self) -> dict:
        channel_health: list[dict] = []
        for adapter in self.registry.all_adapters():
            health_check = getattr(adapter, "health_check", None)
            if not callable(health_check):
                continue
            try:
                health = await health_check()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gateway: health check failed for %s: %s",
                    getattr(adapter, "channel_id", "?"),
                    exc,
                )
                channel_health.append(
                    {
                        "channel_id": getattr(adapter, "channel_id", ""),
                        "healthy": False,
                        "last_error": str(exc),
                    }
                )
                continue
            to_dict = getattr(health, "to_dict", None)
            channel_health.append(to_dict() if callable(to_dict) else dict(health))
        return {
            "running": self._running,
            "channels": self.registry.names(),
            "channel_health": channel_health,
            "inbound_adapters": len(self._inbound_adapters),
            "outbox_pending": len(self.store.outbox_pending()),
            "deferred_outbound": self.outbound.deferred_outbound_count(),
            "dead_letter": len(self.store.dead_letter_entries()),
            "bindings": [
                {
                    "origin": entry.origin,
                    "session_id": entry.target.session_id,
                    "host_type": entry.target.host_type,
                    "connection_state": entry.connection_state,
                }
                for entry in self.binding.all_bindings()
            ],
        }


def _schedule_adapter_stop(adapter) -> None:
    """Stop an adapter's inbound loop on the running loop (best-effort)."""
    import asyncio

    if not hasattr(adapter, "stop"):
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if loop.is_running():
        loop.create_task(adapter.stop())


def _normalize_config_channels(config: GatewayConfig) -> GatewayConfig:
    """Ensure direct GatewayConfig construction cannot bypass type uniqueness."""
    normalized = replace(config, channels=[])
    for channel in config.channels:
        normalized.replace_channel(channel)
    return normalized


def _wechat_state_file(state_dir, channel_name: str, suffix: str):
    path = state_dir / "wechat" / f"{channel_name}_{suffix}.json"
    if path.exists() or channel_name != "wechat":
        return path
    legacy = state_dir / "wechat" / f"wechat-main_{suffix}.json"
    if legacy.exists():
        return legacy
    matches = sorted((state_dir / "wechat").glob(f"*_{suffix}.json"))
    return matches[0] if matches else path


__all__ = ["MessageGateway"]


_HOST_LABELS = {
    "repl": "orchestratord-REPL",
    "orchestrator": "orchestratord-orchestrator",
    "opt_in": "opt-in",
}


def _host_label(host_type: str) -> str:
    """Map a binding host_type to a user-facing label for notifications."""
    return _HOST_LABELS.get(host_type, host_type)


def _collect_broadcast_targets(registry: ChannelAdapterRegistry) -> list[tuple[str, str]]:
    """Collect (channel_id, target) pairs for every outbound channel with a known sender.

    Used as the fallback when an origin (e.g. ``wechat:direct:*:*``) cannot be
    resolved to a concrete target. Each adapter that declares OUTBOUND_TEXT and
    exposes a non-None ``last_known_sender()`` contributes one target, so the
    connection notification reaches operators on every channel that has seen
    inbound traffic this gateway lifetime.
    """
    targets: list[tuple[str, str]] = []
    for adapter in registry.all_adapters():
        caps = getattr(adapter, "capabilities", None)
        if caps is None or not caps.has(ChannelCapability.OUTBOUND_TEXT):
            continue
        last_known = getattr(adapter, "last_known_sender", None)
        if not callable(last_known):
            continue
        sender = last_known()
        if not sender:
            continue
        channel_id = getattr(adapter, "channel_id", None)
        if channel_id:
            targets.append((channel_id, sender))
    return targets
