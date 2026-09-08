"""RunContext — per-run carrier bridging Kernel and Application (DESIGN §4.3).

RunSession 收缩为纯机制会话；业务数据通过 RunContext 传递。Kernel 与
Layer 1 对 ``business`` 的内容完全透明：Application 在 prepare_run 时放入，
interpret_result 时取回。P3 阶段 RunContext 承担 RunSubject/Workspace 的
统一组装（原 backend_runner.run_task 内联逻辑上收 Kernel 侧）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..conversation_store import ensure_conversation_id
from ..session_state import RunSubject

if TYPE_CHECKING:
    from ..agent.task import AgentTask
    from ..workspace import Workspace


@dataclass
class RunContext:
    """机制会话的业务无关快照 + 业务私有载荷。

    ``business`` 是应用自由 dict——Kernel 不读取、不解释其键值；
    业务字段（clarification/conflict_files/attempt 类）在 P3 起迁移到
    RunSession.business（同构 dict），P4 Application 接管后经
    RunContext.business 在 prepare_run/interpret_result 之间传递。
    """

    run_id: str
    conversation_id: str | None
    task: AgentTask
    subject: RunSubject
    workspace: Workspace
    business: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_task(cls, task: AgentTask) -> RunContext:
        """从 AgentTask 统一组装 RunSubject/Workspace（原 backend_runner 内联）。

        组装规则与拆分前逐字段一致：
        - workspace 路径取 task.workspace_path，缺省回退 ``Path(".")``；
        - subject 的 id/identifier 回退 task.id；
        - run_id 在确定性 stage 前缀上追加熵，避免同 workspace 二次运行
          与持久化 session 碰撞（dsh "id collision"）。

        注意：本方法不修改 ``task.conversation_id``——调用方需要时自行
        赋值（backend_runner.run_task 在构造 RunSession 前回写）。
        """
        from pathlib import Path

        from ..workspace import Workspace

        workspace = Workspace(
            path=Path(task.workspace_path) if task.workspace_path else Path("."),
            issue_identifier=task.context.get("issue_identifier", task.id),
            issue_id=task.context.get("issue_id", task.id),
        )
        subject = RunSubject(
            id=task.context.get("issue_id", task.id),
            identifier=task.context.get("issue_identifier"),
            title=task.title,
            description=task.description,
            labels=task.labels,
            url=task.context.get("issue_url"),
            state=task.context.get("issue_state"),
            author_login=task.context.get("issue_author_login"),
            branch_name=task.context.get("issue_branch_name"),
            python_executable=task.context.get("issue_python_executable", ""),
            priority=task.priority,
        )
        conversation_id = ensure_conversation_id(task.conversation_id)
        # The task id is deterministic per workflow stage ("stage-01"),
        # but the run_id doubles as the backend session id — entropy is
        # appended while the stage prefix keeps runs human-correlatable.
        run_id = f"{task.id}-{uuid.uuid4().hex[:8]}"
        return cls(
            run_id=run_id,
            conversation_id=conversation_id,
            task=task,
            subject=subject,
            workspace=workspace,
        )
