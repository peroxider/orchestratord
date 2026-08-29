"""成本追踪与预算控制。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import CostExceededError


@dataclass
class CostBudget:
    """成本预算配置。"""

    max_total_usd: float = 50.0
    max_per_stage_usd: float = 10.0
    warn_threshold_pct: float = 0.8  # 80% 时预警

    @property
    def warn_threshold_usd(self) -> float:
        return self.max_total_usd * self.warn_threshold_pct


@dataclass
class CostTracker:
    """成本追踪器 — 阶段级 + 全局预算 + 预警阈值。"""

    budget: CostBudget = field(default_factory=CostBudget)
    _total_usd: float = 0.0
    _stage_usd: float = 0.0
    _warned_total: bool = False
    _warned_stage: bool = False

    def reset_stage(self) -> None:
        """重置阶段级计数器。"""
        self._stage_usd = 0.0
        self._warned_stage = False

    def add(self, usd: float) -> None:
        """添加成本。"""
        self._total_usd += usd
        self._stage_usd += usd

    def check_budget(self) -> list[str]:
        """检查预算，返回预警列表。

        Raises:
            CostExceededError: 总预算超额。
        """
        warnings: list[str] = []

        if self._total_usd > self.budget.max_total_usd:
            raise CostExceededError(
                f"Total cost ${self._total_usd:.2f} exceeds budget ${self.budget.max_total_usd:.2f}"
            )

        if not self._warned_total and self._total_usd >= self.budget.warn_threshold_usd:
            self._warned_total = True
            warnings.append(
                f"Cost warning: ${self._total_usd:.2f} / ${self.budget.max_total_usd:.2f} "
                f"({self.budget.warn_threshold_pct:.0%} threshold reached)"
            )

        if (
            not self._warned_stage
            and self._stage_usd >= self.budget.max_per_stage_usd * self.budget.warn_threshold_pct
        ):
            self._warned_stage = True
            warnings.append(
                f"Stage cost warning: ${self._stage_usd:.2f} / ${self.budget.max_per_stage_usd:.2f}"
            )

        if self._stage_usd > self.budget.max_per_stage_usd:
            raise CostExceededError(
                f"Stage cost ${self._stage_usd:.2f} exceeds per-stage budget ${self.budget.max_per_stage_usd:.2f}"
            )

        return warnings

    @property
    def total_usd(self) -> float:
        return self._total_usd

    @property
    def stage_usd(self) -> float:
        return self._stage_usd

    def load_state(
        self,
        total_usd: float,
        stage_usd: float = 0.0,
        warned_total: bool = False,
        warned_stage: bool = False,
    ) -> None:
        """从外部状态加载成本累计值（用于检查点恢复）。"""
        self._total_usd = float(total_usd)
        self._stage_usd = float(stage_usd)
        self._warned_total = bool(warned_total)
        self._warned_stage = bool(warned_stage)
