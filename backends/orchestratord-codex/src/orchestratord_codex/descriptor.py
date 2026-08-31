"""codex backend — descriptor 声明。

Codex 是 dynamic runtime：探测 ``codex app-server --help`` 退出码决定走
:class:`CodexSession` （Cli，仅 resumable + parallel_sessions）还是
:class:`CodexAppServerSession` （SdkProcess，加 streaming_deltas + interrupt
+ approval_hooks）。两个 runtime 各声明一个 descriptor；运行时实际启用
哪个由 ``backend._detect_runtime()`` 决定。
"""

from __future__ import annotations

from orchestratord.spi.backend_descriptor import BackendDescriptor, BackendFamily

CODEX_CLI_DESCRIPTOR = BackendDescriptor(
    name="codex-cli",
    display_name="Codex (Cli)",
    family=BackendFamily.CLI,
    backend_package="orchestratord-codex",
    capabilities=frozenset({
        "resumable",
        "parallel_sessions",
    }),
    cli_command="codex",
    cli_args_probe=(),
    env_prefix="CODEX_",
    launch_header="Codex CLI (legacy fallback path)",
    model_discovery="user",
    # DESIGN_backends_hardening.md §1.2: resolving this descriptor forces
    # the Cli runtime (bypasses the `codex app-server --help` probe).
    extra_metadata={"prefer": "cli"},
)

CODEX_APP_SERVER_DESCRIPTOR = BackendDescriptor(
    name="codex-app-server",
    display_name="Codex (AppServer)",
    family=BackendFamily.SDK_PROCESS,
    backend_package="orchestratord-codex",
    capabilities=frozenset({
        "streaming_deltas",
        "interrupt",
        "approval_hooks",
        "parallel_sessions",
        # AppServer backend exposes session/load MCP probe
        # (see app_server_session.py:probe_resume).
        "resume_detection",
    }),
    cli_command="codex",
    cli_args_probe=("app-server", "--help"),
    env_prefix="CODEX_",
    launch_header="Codex AppServer (JSON-RPC over stdio)",
    model_discovery="user",
    # DESIGN_backends_hardening.md §1.2: resolving this descriptor forces
    # the AppServer runtime (bypasses the probe).
    extra_metadata={"prefer": "as"},
)