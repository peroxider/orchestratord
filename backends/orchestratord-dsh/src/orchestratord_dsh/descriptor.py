"""dsh backend — descriptor 声明。

dsh 自描述为 SdkProcess，但 SPI 启发式按 capability 位（缺 interrupt +
approval_hooks + streaming_deltas）将其归类为 ``Cli``，drift 守护在
历史 ``test_capability_drift.py:67-74`` 注释中明确"mirrors the SPI
classification so the detector agrees with the registry"。本 descriptor
沿用此约定：family=CLI，capabilities 不含 SdkProcess 三件套。
"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

DSH_DESCRIPTOR = BackendDescriptor(
    name="dsh",
    display_name="DeepSeek Harness (SdkProcess/Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-dsh",
    capabilities=frozenset({
        "resumable",
        "parallel_sessions",
        "cost_reporting",
    }),
    cli_command=None,
    env_prefix="DSH_",
    launch_header="DeepSeek Harness SDK (per-session subprocess)",
    model_discovery="user",
)