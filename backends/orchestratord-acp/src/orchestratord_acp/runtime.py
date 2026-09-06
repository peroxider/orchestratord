"""Per-runtime identity for the generic ACP backend.

ACP (Agent Client Protocol) is a JSON-RPC 2.0 stdio protocol with no
built-in notion of "which agent". Each vendor ships a binary that speaks
the same wire methods but may differ in CLI args and env namespace. This
module centralizes those per-id defaults so adding a new ACP backend is a
descriptor + one :class:`AcpRuntime` entry (FEATURE_GAP_VS_MULTICA.md §8.3
"per-id 默认值").
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AcpRuntime:
    """Declarative identity for one ACP-speaking agent runtime.

    ``cli_args`` are the fixed argv that follow the binary to put it into
    ACP stdio mode. For runtimes whose exact ACP invocation is not yet
    exercised in-tree, the tuple is empty (bare stdio) and the
    :attr:`launch_header` records the caveat. The binary is overridable at
    runtime via ``<env_prefix>PATH`` (multica-style path override) or
    ``SessionSpec.runtime_bin``.
    """

    id: str
    display_name: str
    binary: str
    cli_args: tuple[str, ...] = ()
    env_prefix: str | None = None
    launch_header: str | None = None


#: Registered ACP runtimes, keyed by descriptor identity. Append-only:
#: add a new entry + a descriptor to expose a new ACP backend.
RUNTIMES: dict[str, AcpRuntime] = {
    "grok": AcpRuntime(
        id="grok",
        display_name="Grok (ACP)",
        binary="grok",
        cli_args=("agent", "--always-approve", "stdio"),
        env_prefix="GROK_",
        launch_header="grok agent --always-approve stdio (ACP)",
    ),
    "codebuddy": AcpRuntime(
        id="codebuddy",
        display_name="CodeBuddy (ACP)",
        binary="codebuddy",
        # Exact ACP stdio args not yet exercised in-tree (FEATURE_GAP §8.1
        # leaves the command blank); bare stdio is ACP's default transport.
        cli_args=(),
        env_prefix="CODEBUDDY_",
        launch_header="codebuddy (ACP stdio; per-id args unverified)",
    ),
    "qwenpaw": AcpRuntime(
        id="qwenpaw",
        display_name="QwenPaw (ACP)",
        binary="qwenpaw",
        # §8.1 marks qwenpaw "per-task workspace"; the ACP entrypoint args
        # are per-install and left bare here.
        cli_args=(),
        env_prefix="QWENPAW_",
        launch_header="qwenpaw (ACP stdio; per-task workspace)",
    ),
    "qodercli": AcpRuntime(
        id="qodercli",
        display_name="Qoder CLI (ACP)",
        binary="qodercli",
        # Exact ACP stdio args not yet exercised in-tree (FEATURE_GAP §8.1
        # row 10); bare stdio is ACP's default transport.
        cli_args=(),
        env_prefix="QODERCLI_",
        launch_header="qodercli (ACP stdio; per-id args unverified)",
    ),
    "qoderclicn": AcpRuntime(
        id="qoderclicn",
        display_name="Qoder CLI CN (ACP)",
        binary="qoderclicn",
        # §8.3 references the multica-style ``MULTICA_QODERCLICN_PATH``
        # override; the ACP entrypoint args are per-install and left bare.
        cli_args=(),
        env_prefix="QODERCLICN_",
        launch_header="qoderclicn (ACP stdio; per-id args unverified)",
    ),
    "deveco": AcpRuntime(
        id="deveco",
        display_name="DevEco (ACP)",
        binary="deveco",
        # Exact ACP stdio args not yet exercised in-tree (FEATURE_GAP §8.1
        # row 11); bare stdio is ACP's default transport.
        cli_args=(),
        env_prefix="DEVECO_",
        launch_header="deveco (ACP stdio; per-id args unverified)",
    ),
}

DEFAULT_RUNTIME_ID = "grok"


def resolve_runtime(runtime_id: str | None) -> AcpRuntime:
    """Resolve *runtime_id* to an :class:`AcpRuntime`; ``None`` → default."""
    if runtime_id is None:
        runtime_id = DEFAULT_RUNTIME_ID
    try:
        return RUNTIMES[runtime_id]
    except KeyError:
        raise ValueError(
            f"unknown ACP runtime {runtime_id!r}; available: {sorted(RUNTIMES)}"
        ) from None


def resolve_binary(runtime: AcpRuntime, runtime_bin: str | None) -> str:
    """Resolve the subprocess binary for *runtime*.

    Precedence: explicit ``runtime_bin`` (from ``SessionSpec.runtime_bin``)
    > ``<env_prefix>PATH`` env override > the descriptor default.
    """
    if runtime_bin:
        return runtime_bin
    if runtime.env_prefix:
        override = os.environ.get(f"{runtime.env_prefix}PATH")
        if override:
            return override
    return runtime.binary
