"""qwen backend — descriptor 声明。

qwen 走 ``-p --output-format stream-json`` 模式（FEATURE_GAP §8.1），
可真正产出 ``TEXT_DELTA`` 事件，因此声明 ``streaming_deltas=True``。
"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

QWEN_DESCRIPTOR = BackendDescriptor(
    name="qwen",
    display_name="Qwen (Cli/stream-json)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-qwen",
    capabilities=frozenset({
        "streaming_deltas",
        "parallel_sessions",
    }),
    cli_command="qwen",
    cli_args_probe=("-p", "--output-format", "stream-json"),
    env_prefix="QWEN_",
    launch_header="qwen -p --output-format stream-json (spawn-per-turn)",
    model_discovery="user",
)