"""dsh backend — descriptor 声明。

通知泵改造后 dsh 以 ``streaming_deltas=True`` 提供真实增量
(``assistant/chunk`` text/reasoning-delta → TEXT_DELTA)。SPI 启发式
仍按 capability 位(require interrupt + approval_hooks +
streaming_deltas 三者齐备)将其归类为 ``Cli`` —— 本 descriptor 沿用
该约定:family=CLI,capabilities 只含实际为真的位。
"""

from __future__ import annotations

import os

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

DSH_DESCRIPTOR = BackendDescriptor(
    name="dsh",
    display_name="DeepSeek Harness (SdkProcess/Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-dsh",
    capabilities=frozenset({
        "streaming_deltas",
        "parallel_sessions",
        "cost_reporting",
    } | ({"pausable"} if os.name == "posix" else set())),
    cli_command=None,
    env_prefix="DSH_",
    launch_header="DeepSeek Harness SDK (per-session subprocess)",
    model_discovery="user",
)
