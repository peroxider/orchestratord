"""ACP backend — descriptor 声明（grok / codebuddy / qwenpaw / qodercli / qoderclicn / deveco）。

Six descriptors share one ``backend_package="orchestratord-acp"`` and one
:class:`AcpBackend` implementation. Each carries ``extra_metadata["prefer"]``
so :func:`resolve_backend` constructs the backend for the right runtime —
mirroring codex's ``codex-cli`` / ``codex-app-server`` split.
"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

_ACP_CAPABILITIES = frozenset({
    "streaming_deltas",
    "interrupt",
    "approval_hooks",
    "parallel_sessions",
})

GROK_DESCRIPTOR = BackendDescriptor(
    name="grok",
    display_name="Grok (ACP)",
    family=BackendFamily.PROTOCOL,
    protocol_family="acp",
    backend_package="orchestratord-acp",
    capabilities=_ACP_CAPABILITIES,
    cli_command="grok",
    cli_args_probe=("agent", "--always-approve", "stdio"),
    env_prefix="GROK_",
    launch_header="grok agent --always-approve stdio (ACP)",
    model_discovery="user",
    extra_metadata={"prefer": "grok"},
)

CODEBUDDY_DESCRIPTOR = BackendDescriptor(
    name="codebuddy",
    display_name="CodeBuddy (ACP)",
    family=BackendFamily.PROTOCOL,
    protocol_family="acp",
    backend_package="orchestratord-acp",
    capabilities=_ACP_CAPABILITIES,
    cli_command="codebuddy",
    env_prefix="CODEBUDDY_",
    launch_header="codebuddy (ACP stdio; per-id args unverified)",
    model_discovery="user",
    extra_metadata={"prefer": "codebuddy"},
)

QWENPAW_DESCRIPTOR = BackendDescriptor(
    name="qwenpaw",
    display_name="QwenPaw (ACP)",
    family=BackendFamily.PROTOCOL,
    protocol_family="acp",
    backend_package="orchestratord-acp",
    capabilities=_ACP_CAPABILITIES,
    cli_command="qwenpaw",
    env_prefix="QWENPAW_",
    launch_header="qwenpaw (ACP stdio; per-task workspace)",
    model_discovery="user",
    extra_metadata={"prefer": "qwenpaw"},
)

QODERCLI_DESCRIPTOR = BackendDescriptor(
    name="qodercli",
    display_name="Qoder CLI (ACP)",
    family=BackendFamily.PROTOCOL,
    protocol_family="acp",
    backend_package="orchestratord-acp",
    capabilities=_ACP_CAPABILITIES,
    cli_command="qodercli",
    env_prefix="QODERCLI_",
    launch_header="qodercli (ACP stdio; per-id args unverified)",
    model_discovery="user",
    extra_metadata={"prefer": "qodercli"},
)

QODERCLICN_DESCRIPTOR = BackendDescriptor(
    name="qoderclicn",
    display_name="Qoder CLI CN (ACP)",
    family=BackendFamily.PROTOCOL,
    protocol_family="acp",
    backend_package="orchestratord-acp",
    capabilities=_ACP_CAPABILITIES,
    cli_command="qoderclicn",
    env_prefix="QODERCLICN_",
    launch_header="qoderclicn (ACP stdio; per-id args unverified)",
    model_discovery="user",
    extra_metadata={"prefer": "qoderclicn"},
)

DEVECO_DESCRIPTOR = BackendDescriptor(
    name="deveco",
    display_name="DevEco (ACP)",
    family=BackendFamily.PROTOCOL,
    protocol_family="acp",
    backend_package="orchestratord-acp",
    capabilities=_ACP_CAPABILITIES,
    cli_command="deveco",
    env_prefix="DEVECO_",
    launch_header="deveco (ACP stdio; per-id args unverified)",
    model_discovery="user",
    extra_metadata={"prefer": "deveco"},
)
