"""BackendDescriptor — 后端的声明式元数据（Backend Factory Protocol: descriptor layer）。

每个 backend 包通过 entry_point ``orchestratord.backend_descriptors``
注册一个或多个 :class:`BackendDescriptor`。每个 descriptor 描述一个具体的
runtime 身份（如 ``example-backend``）以及它所属的协议家族。

与 :class:`orchestratord.spi.backend.AgentBackend` 的区别：

- :class:`AgentBackend` 是 **行为**（``capabilities()`` / ``create_session()`` /
  ``dispose()``），启动时伴随可能失败的 I/O。
- :class:`BackendDescriptor` 是 **声明**（``name`` / ``family`` / ``cli_command`` /
  ``env_prefix`` / ``model_discovery``），加载永不失败。

参考 multica :class:`BuiltinRuntime` （``server/pkg/agent/builtin_runtimes.go:18-70``）。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal


class BackendFamily(enum.Enum):
    """协议家族 — 取代 ``_classify_family`` 启发式。

    值字符串必须与 ``backend_registry.list_backends()`` 历史输出兼容
    （``InProcess`` / ``SdkProcess`` / ``Protocol`` / ``Cli``）。
    """

    IN_PROCESS = "InProcess"
    SDK_PROCESS = "SdkProcess"
    PROTOCOL = "Protocol"
    CLI = "Cli"


@dataclass(frozen=True)
class BackendDescriptor:
    """一个后端 runtime 的声明式元数据。

    Frozen 不可变 — 描述符应当是无副作用的纯声明，所有"做 I/O"的部分都
    在对应的 :class:`AgentBackend` 实现上。
    """

    name: str
    """全局唯一标识（descriptor key）。如 ``"example-backend"``。"""
    display_name: str
    """用户可见名。如 ``"Example Backend"``。"""
    family: BackendFamily
    """协议家族 — 取代 `_classify_family` 启发式。"""
    backend_package: str
    """拥有此 runtime 的 orchestratord-* 包名（带连字符）。"""
    capabilities: frozenset[str]
    """声明的 capability 位名（不含 ``None`` 默认）。如 ``frozenset({"streaming_deltas"})``。"""
    cli_command: str | None = None
    """若走 CLI：``subprocess.Popen`` 第一参数；``InProcess`` 留空。"""
    cli_args_probe: tuple[str, ...] = ()
    """启动探针参数（如 ``("app-server", "--help")``）。"""
    env_prefix: str | None = None
    """配置前缀（如 ``"BACKEND_"``），用于该 backend 的 env 命名空间。"""
    launch_header: str | None = None
    """启动 banner（日志诊断用）。"""
    model_discovery: Literal["static", "probe", "user"] = "user"
    """如何获取模型列表：

    - ``static`` — 硬编码于 descriptor（未来扩展字段）
    - ``probe`` — 启动时调 CLI/API 探针
    - ``user`` — 用户通过 env / spec 提供
    """
    extra_metadata: dict[str, str] = field(default_factory=dict)
    """自由扩展字段（如版本号 / homepage），供诊断工具读取。"""

    def __hash__(self) -> int:
        """Hash over the identity fields only — ``extra_metadata`` is a
        mutable dict and cannot participate; two descriptors with the
        same identity + capabilities hash equal regardless of extras.
        """
        return hash(
            (
                self.name,
                self.display_name,
                self.family,
                self.backend_package,
                self.capabilities,
            )
        )
