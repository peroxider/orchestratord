"""持久化文件生命周期 cron 清理任务。

将 ReliabilityStore.purge_all 注册为定期任务,默认每 24 小时执行一次。
可通过 ReliabilityConfig.retention_enabled=False 禁用。
dead_letter.ndjson 与 audit.ndjson 不由此模块处理(由 store 内的
日志式轮转机制在 append 时覆盖清除)。
"""

from __future__ import annotations

import logging

from .config import ReliabilityConfig
from .store import ReliabilityStore

logger = logging.getLogger(__name__)


def run_retention_sweep(
    state_dir: str,
    reliability: ReliabilityConfig,
) -> dict[str, int]:
    """执行一次 cron 清理扫描。返回 {文件名: 清理条数}。

    幂等、线程安全(store 内部有锁)。失败时记录日志并返回空 dict,
    不抛异常(定时任务不应阻塞调度器)。
    """
    if not reliability.retention_enabled:
        return {}
    try:
        store = ReliabilityStore(state_dir, reliability=reliability)
        removed = store.purge_all(reliability)
        if any(removed.values()):
            logger.info("im_gateway retention sweep: %s", removed)
        return removed
    except Exception:
        logger.exception("im_gateway retention sweep failed")
        return {}


__all__ = ["run_retention_sweep"]
