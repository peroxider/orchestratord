"""File-based reliability store.

Persists the gateway's durable state under ``state_dir`` using the
project's existing file-based conventions (small state → JSON, append
logs → NDJSON). Each file has a single-writer lock; JSON state files
are written via tmp + atomic ``os.replace``; NDJSON logs are
append-only. v1 keeps this single-process/single-account; the backend
interface is reserved so SQLite/Postgres can slot in later (P6+).

P1 ships functional basics (dedupe, outbox, dead-letter, context
tokens, audit). P4 adds compaction/rotation, the retry loop,
storm aggregation, and cross-process audit/redaction hardening.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .config import ReliabilityConfig

logger = logging.getLogger(__name__)


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _append_ndjson(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _rewrite_ndjson(path: Path, entries: list[dict[str, Any]]) -> None:
    """原子重写 NDJSON:写 tmp + os.replace。调用方需持锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _rotate_ndjson(path: Path, max_bytes: int, backup_count: int) -> None:
    """日志式轮转:若 path 大小超 max_bytes,则滚动备份。

    仿 logging.handlers.RotatingFileHandler:
    - 删除最旧备份 path.{backup_count}
    - 从 {backup_count-1} 到 1 依次重命名为下一个编号
    - 当前文件重命名为 path.1
    """
    if not path.exists() or path.stat().st_size < max_bytes:
        return
    oldest = path.with_suffix(path.suffix + f".{backup_count}")
    if oldest.exists():
        oldest.unlink()
    for i in range(backup_count - 1, 0, -1):
        src = path.with_suffix(path.suffix + f".{i}")
        dst = path.with_suffix(path.suffix + f".{i + 1}")
        if src.exists():
            src.rename(dst)
    path.rename(path.with_suffix(path.suffix + ".1"))


class ReliabilityStore:
    def __init__(
        self,
        state_dir: str | Path,
        reliability: ReliabilityConfig | None = None,
    ) -> None:
        self._dir = Path(state_dir).expanduser()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._reliability = reliability or ReliabilityConfig()
        self._lock = threading.RLock()
        # in-memory dedupe index: key -> first_seen_ts
        self._dedupe: dict[str, float] = {}
        self._load_dedupe()

    # -- paths -----------------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self._dir

    def _p(self, name: str) -> Path:
        return self._dir / name

    # -- inbound dedupe --------------------------------------------------
    def _load_dedupe(self) -> None:
        cutoff = time.time() - self._reliability.inbound_dedupe_ttl_seconds
        for entry in _read_ndjson(self._p("processed_inbound.ndjson")):
            key = entry.get("key")
            ts = entry.get("seen_at", 0)
            if key and ts >= cutoff:
                self._dedupe[key] = ts

    def is_duplicate(self, key: str) -> bool:
        with self._lock:
            self._purge_expired()
            return key in self._dedupe

    def record_processed(self, key: str, *, message_id: str | None = None) -> None:
        with self._lock:
            ts = time.time()
            self._dedupe[key] = ts
            _append_ndjson(
                self._p("processed_inbound.ndjson"),
                {"key": key, "message_id": message_id, "seen_at": ts},
            )

    def check_and_record(self, key: str, *, message_id: str | None = None) -> bool:
        """Return True if newly seen (and recorded); False if duplicate."""
        with self._lock:
            self._purge_expired()
            if key in self._dedupe:
                logger.debug("im_gateway dedupe hit: key=%s", key[:32])
                return False
            self.record_processed(key, message_id=message_id)
            return True

    def _purge_expired(self) -> None:
        cutoff = time.time() - self._reliability.inbound_dedupe_ttl_seconds
        self._dedupe = {k: v for k, v in self._dedupe.items() if v >= cutoff}

    # -- outbox ----------------------------------------------------------
    def append_outbox(self, entry: dict[str, Any]) -> None:
        with self._lock:
            _append_ndjson(self._p("outbox.ndjson"), entry)

    def outbox_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return _read_ndjson(self._p("outbox.ndjson"))

    def outbox_pending(self) -> list[dict[str, Any]]:
        """Latest status per idempotency_key where status != delivered."""
        with self._lock:
            latest: dict[str, dict[str, Any]] = {}
            for e in _read_ndjson(self._p("outbox.ndjson")):
                key = e.get("idempotency_key")
                if not key:
                    continue
                latest[key] = e
            terminal = {"delivered", "dead", "failed"}
            return [e for e in latest.values() if e.get("status") not in terminal]

    # -- dead letter -----------------------------------------------------
    def append_dead_letter(self, entry: dict[str, Any]) -> None:
        with self._lock:
            path = self._p("dead_letter.ndjson")
            _rotate_ndjson(
                path,
                self._reliability.dead_letter_max_bytes,
                self._reliability.dead_letter_backup_count,
            )
            _append_ndjson(path, entry)
        logger.warning(
            "im_gateway dead-letter appended: channel=%s idem=%s category=%s",
            entry.get("channel"),
            str(entry.get("idempotency_key"))[:16],
            entry.get("error_category"),
        )

    def dead_letter_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return _read_ndjson(self._p("dead_letter.ndjson"))

    # -- context tokens --------------------------------------------------
    def get_context_token(self, account_id: str, user_id: str) -> str | None:
        data = _read_json(self._p("wechat_context_tokens.json"), {})
        return data.get(f"{account_id}:{user_id}")

    def set_context_token(self, account_id: str, user_id: str, token: str | None) -> None:
        with self._lock:
            data = _read_json(self._p("wechat_context_tokens.json"), {})
            key = f"{account_id}:{user_id}"
            if token is None:
                data.pop(key, None)
            else:
                data[key] = token
            _atomic_write_json(self._p("wechat_context_tokens.json"), data)

    def wechat_context_users(self, account_id: str) -> list[str]:
        """Return user_ids that have a persisted context token for ``account_id``.

        Used to resolve a wildcard WeChat OUTBOUND origin right after a
        gateway restart, before any new inbound arrives: the context-token
        store already survives restarts (it backs the ``context_reply``
        capability), so it doubles as the durable record of known senders
        without a separate persistence file.
        """
        data = _read_json(self._p("wechat_context_tokens.json"), {})
        prefix = f"{account_id}:"
        return [k[len(prefix) :] for k in data if isinstance(k, str) and k.startswith(prefix)]

    def get_feishu_last_sender(self, channel_id: str) -> str | None:
        data = _read_json(self._p("feishu_last_senders.json"), {})
        sender = data.get(channel_id)
        return str(sender) if sender else None

    def set_feishu_last_sender(self, channel_id: str, sender: str | None) -> None:
        with self._lock:
            data = _read_json(self._p("feishu_last_senders.json"), {})
            if sender:
                data[channel_id] = sender
            else:
                data.pop(channel_id, None)
            _atomic_write_json(self._p("feishu_last_senders.json"), data)

    def get_wechat_cursor(self, account_id: str) -> str:
        """Return the saved iLink ``get_updates_buf`` cursor, or ``""``.

        iLink expects the cursor as a string on every ``getupdates`` POST;
        sending JSON ``null`` (the previous default) can cause the server to
        never deliver messages even though the session is valid. The two
        reference clients (hermes-agent, AstrBot) both default to ``""``.
        """
        data = _read_json(self._p("wechat_accounts.json"), {})
        entry = data.get(account_id)
        if not isinstance(entry, dict):
            return ""
        cursor = entry.get("get_updates_buf")
        return str(cursor) if cursor else ""

    def set_wechat_cursor(self, account_id: str, get_updates_buf: str | None) -> None:
        with self._lock:
            data = _read_json(self._p("wechat_accounts.json"), {})
            entry = data.get(account_id)
            if not isinstance(entry, dict):
                entry = {}
            entry["get_updates_buf"] = get_updates_buf
            entry["updated_at"] = time.time()
            data[account_id] = entry
            _atomic_write_json(self._p("wechat_accounts.json"), data)

    # -- unsupported inbound (P2) ----------------------------------------
    def record_unsupported_media(self, entry: dict[str, Any]) -> None:
        with self._lock:
            _append_ndjson(self._p("unsupported_inbound.ndjson"), entry)

    # -- audit -----------------------------------------------------------
    def audit(self, event_type: str, **fields: Any) -> None:
        from .audit import redact

        entry = {
            "timestamp": time.time(),
            "event_type": event_type,
            **redact(fields),
        }
        with self._lock:
            path = self._p("audit.ndjson")
            _rotate_ndjson(
                path,
                self._reliability.audit_max_bytes,
                self._reliability.audit_backup_count,
            )
            _append_ndjson(path, entry)

    def audit_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            return _read_ndjson(self._p("audit.ndjson"))

    # -- cron retention --------------------------------------------------
    def purge_processed_inbound(self, ttl_seconds: int, max_entries: int) -> int:
        """删过期 + 截断到 max_entries(保留最新)。返回清理条数。"""
        return self._purge_ndjson_ttl_cap(
            "processed_inbound.ndjson", ttl_seconds, max_entries, ts_fields=("seen_at",)
        )

    def purge_outbox(self, ttl_seconds: int, max_entries: int) -> int:
        return self._purge_ndjson_ttl_cap(
            "outbox.ndjson", ttl_seconds, max_entries, ts_fields=("at", "timestamp")
        )

    def purge_unsupported_inbound(self, ttl_seconds: int, max_entries: int) -> int:
        return self._purge_ndjson_ttl_cap(
            "unsupported_inbound.ndjson",
            ttl_seconds,
            max_entries,
            ts_fields=("received_at", "at", "timestamp"),
        )

    def purge_all(self, reliability: ReliabilityConfig) -> dict[str, int]:
        """Return cron cleanup counts for bounded append-style files only."""
        return {
            "processed_inbound.ndjson": self.purge_processed_inbound(
                reliability.retention_processed_inbound_ttl_seconds,
                reliability.retention_processed_inbound_max_entries,
            ),
            "outbox.ndjson": self.purge_outbox(
                reliability.retention_outbox_ttl_seconds,
                reliability.retention_outbox_max_entries,
            ),
            "unsupported_inbound.ndjson": self.purge_unsupported_inbound(
                reliability.retention_unsupported_inbound_ttl_seconds,
                reliability.retention_unsupported_inbound_max_entries,
            ),
        }

    # -- internal helpers ------------------------------------------------
    def _purge_ndjson_ttl_cap(
        self, name: str, ttl_seconds: int, max_entries: int, ts_fields: tuple[str, ...]
    ) -> int:
        cutoff = time.time() - ttl_seconds
        with self._lock:
            path = self._p(name)
            entries = _read_ndjson(path)
            if not entries:
                return 0
            indexed = list(enumerate(entries))
            survivors = [
                (index, entry)
                for index, entry in indexed
                if (ts := self._entry_timestamp(entry, ts_fields)) is None or ts >= cutoff
            ]
            if len(survivors) > max_entries:
                survivors = sorted(
                    survivors,
                    key=lambda item: self._entry_timestamp(item[1], ts_fields) or item[0],
                    reverse=True,
                )[:max_entries]
                survivors.sort(key=lambda item: item[0])
            kept = [entry for _, entry in survivors]
            removed = len(entries) - len(kept)
            if removed:
                _rewrite_ndjson(path, kept)
            return removed

    @staticmethod
    def _entry_timestamp(entry: dict[str, Any], fields: tuple[str, ...]) -> float | None:
        for field in fields:
            value = entry.get(field)
            if isinstance(value, (int, float)):
                return float(value)
        return None


__all__ = ["ReliabilityStore"]
