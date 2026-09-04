"""Gateway configuration: load/save ``channels.yaml`` atomically.

The YAML file is both the hand-editable file-state base and the CLI
wizard's persistence target — the wizard reads/writes the same file.
Saves are atomic (tmp file + ``os.replace``) under a single-writer
``fcntl`` lock so concurrent processes cannot interleave writes.

Channel entries are stored as :class:`ChannelConfig` objects; WeChat
platform-specific fields (``base_url``, ``account_id``, ``allowed_users``,
…) live in ``ChannelConfig.extra`` so the existing ``to_dict``/``from_dict``
round-trip is reused unchanged.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TextIO

import yaml

from orchestratord.channels.models import ChannelConfig, ChannelType
from orchestratord.utils.file_lock import HAS_FLOCK, flock_exclusive, flock_unlock

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "~/.orchestratord/gateway"
DEFAULT_CHANNELS_YAML = "~/.orchestratord/gateway/channels.yaml"
# Pre-rename state dir kept so :func:`migrate_legacy_state_dir` can move an
# existing install forward the first time the new path is used.
LEGACY_STATE_DIR = "~/.orchestratord/im-gateway"


def config_path_for_state_dir(state_dir: str | Path | None) -> Path | None:
    """Resolve ``<state-dir>/channels.yaml`` for a gateway state directory.

    Returns ``None`` when ``state_dir`` is ``None`` so callers keep
    :func:`load_config`'s default-path behavior (``DEFAULT_CHANNELS_YAML``
    plus legacy-state-dir migration).
    """
    if state_dir is None:
        return None
    return Path(state_dir).expanduser() / "channels.yaml"


def _chmod_private(path: Path, mode: int) -> None:
    """Best-effort ``chmod``; skipped on Windows and unsupported filesystems."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def ensure_private_dir(path: str | Path) -> Path:
    """Create ``path`` as a private (0700) directory, tightening existing ones.

    ``Path.mkdir(mode=...)`` applies the mode only to the leaf directory and
    is ignored on Windows, so the mode is re-asserted with ``chmod`` to also
    tighten pre-existing directories (gateway state may hold credentials).
    """
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    _chmod_private(p, 0o700)
    return p


def open_private_writer(path: str | Path, *, append: bool = False) -> TextIO:
    """Open a UTF-8 text file for writing, created with owner-only 0600.

    ``os.open`` applies the mode at creation (a plain ``open()`` would honor
    the process umask, typically 0644 under ``umask 022``); the follow-up
    ``chmod`` also tightens a pre-existing file because ``O_CREAT`` never
    changes the mode of an existing inode. Mode changes are no-ops on
    Windows.
    """
    p = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(str(p), flags, 0o600)
    fh = os.fdopen(fd, "a" if append else "w", encoding="utf-8")
    _chmod_private(p, 0o600)
    return fh


DEFAULT_REPL_COMMAND_ALLOWLIST: tuple[str, ...] = (
    "/stop",
    "/clear",
    "/reset",
    "/new",
    "/goal",
    "/help",
    "/?",
    "/cost",
    "/history",
    "/context",
    "/recap",
    "/btw",
    "/cron-list",
    "/cron-status",
    "/cron-runs",
    "/tools",
    "/skills",
    "/diff",
    "/mcp",
    "/tasks",
    "/idle",
    "/doctor",
    "/release-notes",
)

DEFAULT_ORCHESTRATOR_COMMAND_ALLOWLIST: tuple[str, ...] = (
    "/server status",
    "/issue list",
    "/issue show",
    "/issue tail",
    "/issue stop",
    "/issue pause",
    "/issue resume",
    "/issue clarify",
    "/issue inject",
    "/issue feedback",
    "/issue review",
    "/issue retry",
    "/issue workspace",
    "/issue rebase",
)


def _normalize_command_allowlist(
    commands: Any,
    *,
    path: str,
    max_parts: int,
) -> tuple[str, ...]:
    if not isinstance(commands, (list, tuple)):
        raise ValueError(f"{path}: expected a YAML list of slash commands")  # noqa: TRY004
    normalized: list[str] = []
    seen: set[str] = set()
    for item in commands:
        if not isinstance(item, str):
            raise ValueError(f"{path}: every command must be a string")  # noqa: TRY004
        command = " ".join(item.strip().lower().split())
        parts = command.split()
        if not command.startswith("/") or not parts or len(parts) > max_parts:
            raise ValueError(
                f"{path}: invalid command {item!r}; expected at most {max_parts} slash token(s)"
            )
        if command not in seen:
            seen.add(command)
            normalized.append(command)
    return tuple(normalized)


@dataclass(frozen=True)
class CommandAllowlistConfig:
    """Runtime slash-command allowlists persisted in ``channels.yaml``."""

    repl: tuple[str, ...] = DEFAULT_REPL_COMMAND_ALLOWLIST
    orchestrator: tuple[str, ...] = DEFAULT_ORCHESTRATOR_COMMAND_ALLOWLIST

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "repl",
            _normalize_command_allowlist(
                self.repl,
                path="command_allowlists.repl",
                max_parts=1,
            ),
        )
        object.__setattr__(
            self,
            "orchestrator",
            _normalize_command_allowlist(
                self.orchestrator,
                path="command_allowlists.orchestrator",
                max_parts=2,
            ),
        )

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "repl": list(self.repl),
            "orchestrator": list(self.orchestrator),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CommandAllowlistConfig:
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ValueError("command_allowlists: expected a YAML mapping")  # noqa: TRY004
        return cls(
            repl=data.get("repl", DEFAULT_REPL_COMMAND_ALLOWLIST),
            orchestrator=data.get("orchestrator", DEFAULT_ORCHESTRATOR_COMMAND_ALLOWLIST),
        )


def migrate_legacy_state_dir(target: str | Path | None = None) -> Path:
    """One-way migration ``~/.orchestratord/im-gateway`` → ``~/.orchestratord/gateway``.

    Idempotent and safe to call from any entry point that resolves the state
    directory. If ``target`` already exists, it is returned unchanged. If the
    legacy directory exists, it is moved to ``target`` (``Path.rename``, with a
    ``shutil.copytree`` + ``rmtree`` fallback for cross-filesystem renames or
    platforms where ``rename`` refuses). If neither exists, ``target`` is
    returned without being created — the caller owns ``mkdir``.

    Only the *default* location is migrated; an explicit ``target`` override is
    honored as-is. Returns the resolved target :class:`~pathlib.Path`.
    """
    new = Path(target or DEFAULT_STATE_DIR).expanduser()
    if new.exists():
        return new
    legacy = Path(LEGACY_STATE_DIR).expanduser()
    if not legacy.exists():
        return new
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy.rename(new)
    except OSError:
        # Cross-device link or permission quirk — fall back to a copy.
        shutil.copytree(legacy, new)
        shutil.rmtree(legacy, ignore_errors=True)
    # The moved directory keeps the legacy install's mode; tighten it.
    _chmod_private(new, 0o700)
    logger.info("migrated gateway state dir %s -> %s", legacy, new)
    return new


@dataclass
class ReliabilityConfig:
    outbox_max_attempts: int = 5
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 60.0
    inbound_dedupe_ttl_seconds: int = 600
    storm_window_seconds: int = 60
    secret_encryption_env: str = "ORCHESTRATORD_IM_SECRET"
    markdown_fallback: bool = True
    long_message_threshold_chunks: int = 4
    deferred_outbox_limit: int = 500
    # Persistence retention for bounded append-style files.
    retention_enabled: bool = True
    retention_cron_interval_seconds: int = 24 * 3600
    retention_processed_inbound_ttl_seconds: int = 7 * 86400
    retention_processed_inbound_max_entries: int = 10000
    retention_outbox_ttl_seconds: int = 30 * 86400
    retention_outbox_max_entries: int = 50000
    retention_unsupported_inbound_ttl_seconds: int = 7 * 86400
    retention_unsupported_inbound_max_entries: int = 10000
    # Append-time rotation for audit/dead-letter logs.
    dead_letter_max_bytes: int = 10 * 1024 * 1024  # 10 MiB
    dead_letter_backup_count: int = 3
    audit_max_bytes: int = 10 * 1024 * 1024  # 10 MiB
    audit_backup_count: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "outbox_max_attempts": self.outbox_max_attempts,
            "retry_base_seconds": self.retry_base_seconds,
            "retry_max_seconds": self.retry_max_seconds,
            "inbound_dedupe_ttl_seconds": self.inbound_dedupe_ttl_seconds,
            "storm_window_seconds": self.storm_window_seconds,
            "secret_encryption_env": self.secret_encryption_env,
            "markdown_fallback": self.markdown_fallback,
            "long_message_threshold_chunks": self.long_message_threshold_chunks,
            "deferred_outbox_limit": self.deferred_outbox_limit,
            "retention_enabled": self.retention_enabled,
            "retention_cron_interval_seconds": self.retention_cron_interval_seconds,
            "retention_processed_inbound_ttl_seconds": self.retention_processed_inbound_ttl_seconds,
            "retention_processed_inbound_max_entries": self.retention_processed_inbound_max_entries,
            "retention_outbox_ttl_seconds": self.retention_outbox_ttl_seconds,
            "retention_outbox_max_entries": self.retention_outbox_max_entries,
            "retention_unsupported_inbound_ttl_seconds": self.retention_unsupported_inbound_ttl_seconds,
            "retention_unsupported_inbound_max_entries": self.retention_unsupported_inbound_max_entries,
            "dead_letter_max_bytes": self.dead_letter_max_bytes,
            "dead_letter_backup_count": self.dead_letter_backup_count,
            "audit_max_bytes": self.audit_max_bytes,
            "audit_backup_count": self.audit_backup_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReliabilityConfig:
        if not data:
            return cls()
        known = {
            k: v
            for k, v in data.items()
            if k
            in {
                "outbox_max_attempts",
                "retry_base_seconds",
                "retry_max_seconds",
                "inbound_dedupe_ttl_seconds",
                "storm_window_seconds",
                "secret_encryption_env",
                "markdown_fallback",
                "long_message_threshold_chunks",
                "deferred_outbox_limit",
                "retention_enabled",
                "retention_cron_interval_seconds",
                "retention_processed_inbound_ttl_seconds",
                "retention_processed_inbound_max_entries",
                "retention_outbox_ttl_seconds",
                "retention_outbox_max_entries",
                "retention_unsupported_inbound_ttl_seconds",
                "retention_unsupported_inbound_max_entries",
                "dead_letter_max_bytes",
                "dead_letter_backup_count",
                "audit_max_bytes",
                "audit_backup_count",
            }
        }
        return cls(**known)


@dataclass
class GatewayConfig:
    enabled: bool = True
    default_targets: list[str] = field(default_factory=list)
    state_dir: str = DEFAULT_STATE_DIR
    storage_backend: str = "files"
    command_allowlists: CommandAllowlistConfig = field(default_factory=CommandAllowlistConfig)
    reliability: ReliabilityConfig = field(default_factory=ReliabilityConfig)
    channels: list[ChannelConfig] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "default_targets": _normalize_default_targets(self.default_targets),
            "state_dir": self.state_dir,
            "storage_backend": self.storage_backend,
            "command_allowlists": self.command_allowlists.to_dict(),
            "reliability": self.reliability.to_dict(),
            "channels": [c.to_dict() for c in _unique_channels_by_type(self.channels)],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> GatewayConfig:
        if not data:
            return cls()
        cfg = cls(
            enabled=bool(data.get("enabled", True)),
            default_targets=list(data.get("default_targets") or []),
            state_dir=str(data.get("state_dir", DEFAULT_STATE_DIR)),
            storage_backend=str(data.get("storage_backend", "files")),
            command_allowlists=CommandAllowlistConfig.from_dict(data.get("command_allowlists")),
            reliability=ReliabilityConfig.from_dict(data.get("reliability")),
        )
        channels_raw = data.get("channels") or []
        for entry in channels_raw:
            if not isinstance(entry, dict):
                continue
            cfg.replace_channel(ChannelConfig.from_dict(entry))
        cfg.default_targets = _normalize_default_targets(cfg.default_targets)
        return cfg

    def get_channel(self, name: str) -> ChannelConfig | None:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    def get_channel_by_type(self, channel_type: ChannelType | str) -> ChannelConfig | None:
        key = channel_type.value if isinstance(channel_type, ChannelType) else str(channel_type)
        for c in self.channels:
            if c.type.value == key:
                return c
        return None

    def replace_channel(self, channel: ChannelConfig) -> None:
        """Replace an existing channel entry by type, or append.

        IM Message Gateway v1 intentionally keeps one configured channel per
        channel type. Names remain useful for status/restart display, but they
        are not a second axis of multiplicity.
        """
        channel = _normalize_channel(channel)
        self.default_targets = _normalize_default_targets(self.default_targets)
        replaced = False
        next_channels: list[ChannelConfig] = []
        for c in self.channels:
            c = _normalize_channel(c)
            if c.type == channel.type:
                if not replaced:
                    if c.name != channel.name:
                        self.default_targets = [
                            channel.name if target == c.name else target
                            for target in self.default_targets
                        ]
                    next_channels.append(channel)
                    replaced = True
                continue
            next_channels.append(c)
        if not replaced:
            next_channels.append(channel)
        self.channels = next_channels

    def remove_channel(self, name: str) -> bool:
        before = len(self.channels)
        self.channels = [c for c in self.channels if c.name != name]
        return len(self.channels) < before


def _unique_channels_by_type(channels: list[ChannelConfig]) -> list[ChannelConfig]:
    cfg = GatewayConfig()
    for channel in channels:
        cfg.replace_channel(channel)
    return cfg.channels


def _normalize_channel(channel: ChannelConfig) -> ChannelConfig:
    if channel.type is ChannelType.WECHAT and channel.name != "wechat":
        return replace(channel, name="wechat")
    return channel


def _normalize_default_targets(default_targets: list[str]) -> list[str]:
    return ["wechat" if target == "wechat-main" else target for target in default_targets]


_LOCK = threading.Lock()


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """Single-writer advisory lock (POSIX fcntl)."""
    ensure_private_dir(lock_path.parent)
    lock_path.touch(exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR)
    try:
        if HAS_FLOCK:
            flock_exclusive(fd)
        yield
    finally:
        if HAS_FLOCK:
            flock_unlock(fd)
        os.close(fd)


def load_config(path: str | Path | None = None) -> GatewayConfig:
    """Load :class:`GatewayConfig` from ``path`` (default ``channels.yaml``)."""
    if path is None:
        # Move a pre-rename ~/.orchestratord/im-gateway install forward the
        # first time the default path is resolved, so existing channels.yaml
        # survives.
        migrate_legacy_state_dir()
    p = Path(path or DEFAULT_CHANNELS_YAML).expanduser()
    if not p.exists():
        logger.warning("gateway config not found at %s; using defaults", p)
        return GatewayConfig()
    with _file_lock(_lock_path(p)):
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        logger.error("%s: top-level YAML is not a mapping", p)
        raise ValueError(f"{p}: expected a YAML mapping at the top level")  # noqa: TRY004
    cfg = GatewayConfig.from_dict(data)
    logger.info("gateway config loaded: %s (%d channel(s))", p, len(cfg.channels))
    return cfg


def save_config(config: GatewayConfig, path: str | Path | None = None) -> Path:
    """Atomically save ``config`` to ``path`` under a single-writer lock."""
    if path is None:
        migrate_legacy_state_dir()
    p = Path(path or DEFAULT_CHANNELS_YAML).expanduser()
    ensure_private_dir(p.parent)
    payload = yaml.safe_dump(config.to_dict(), allow_unicode=True, sort_keys=False)
    with _file_lock(_lock_path(p)):
        tmp = p.with_suffix(p.suffix + ".tmp")
        # The tmp file is created 0600; os.replace keeps that mode on the
        # final channels.yaml, which persists webhook URLs and app secrets.
        with open_private_writer(tmp) as fh:
            fh.write(payload)
        os.replace(tmp, p)
    logger.info("gateway config saved: %s", p)
    return p


def _lock_path(yaml_path: Path) -> Path:
    return yaml_path.with_suffix(yaml_path.suffix + ".lock")


__all__ = [
    "DEFAULT_CHANNELS_YAML",
    "DEFAULT_ORCHESTRATOR_COMMAND_ALLOWLIST",
    "DEFAULT_REPL_COMMAND_ALLOWLIST",
    "DEFAULT_STATE_DIR",
    "LEGACY_STATE_DIR",
    "CommandAllowlistConfig",
    "GatewayConfig",
    "ReliabilityConfig",
    "config_path_for_state_dir",
    "ensure_private_dir",
    "load_config",
    "migrate_legacy_state_dir",
    "open_private_writer",
    "save_config",
]
