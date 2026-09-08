"""Single source of truth for backend CLI binary names.

Every backend package that shells out to an external CLI must register
the binary here. The test-time CLI guard (see
``scripts/install_cli_shims.py`` and ``tests/conftest.py``) and the
CI drift detector (``tests/test_capability_drift.py``) both read this
module so the list of guarded binaries cannot drift across surfaces.

Importing this module has no side effects: no subprocess calls, no
network I/O, no logging configuration. The ``BackendCLI`` dataclass
is frozen so accidental mutation at runtime raises.

Per DESIGN_backend_cli_test_guard.md §1.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackendCLI:
    """One external CLI binary invoked by a backend package.

    Attributes:
        binary: Command name as it appears on ``$PATH`` (first argv to
            ``subprocess.Popen`` / ``subprocess.run``).
        backend_package: Dotted name of the ``orchestratord-*`` package
            that owns this CLI (used for diagnostic output).
        notes: Free-form documentation — probe arguments, default
            timeout, spawn-per-turn vs long-running, etc.
    """

    binary: str
    backend_package: str
    notes: str = ""


#: Canonical list of backend CLIs that the test guard intercepts.
#: Order is stable (matches the design doc). Append-only — never
#: reorder, never delete; add new entries at the end with a note.
KNOWN_BACKEND_CLIS: tuple[BackendCLI, ...] = (
    BackendCLI(
        "clawcodex-dev",
        "orchestratord-clawcodex",
        "isolated Python SDK worker; 探针仅做 capability 探测",
    ),
    BackendCLI(
        "codex",
        "orchestratord-codex",
        "双路径：codex app-server (SdkProcess) 或 codex exec --json (Cli)",
    ),
    BackendCLI(
        "hermes",
        "orchestratord-hermes",
        "spawn-per-turn Cli",
    ),
    BackendCLI(
        "opencode",
        "orchestratord-opencode",
        "opencode serve --port 0 + SSE",
    ),
    BackendCLI(
        "dsh",
        "orchestratord-dsh",
        "deepseek-harness-sdk 子进程",
    ),
    BackendCLI(
        "cursor-agent",
        "orchestratord-cursor",
        "spawn-per-turn Cli; event stream shape not yet exercised",
    ),
    BackendCLI(
        "copilot",
        "orchestratord-copilot",
        "spawn-per-turn Cli; 事件流需实验",
    ),
    BackendCLI(
        "kimi",
        "orchestratord-kimi",
        "spawn-per-turn Cli; 中文 prompt 友好",
    ),
    BackendCLI(
        "qwen",
        "orchestratord-qwen",
        "spawn-per-turn Cli; qwen -p --output-format stream-json 真正流式",
    ),
    BackendCLI(
        "kiro",
        "orchestratord-kiro-cli",
        "spawn-per-turn Cli; event stream shape not yet exercised",
    ),
    BackendCLI(
        "openclaw",
        "orchestratord-openclaw",
        "spawn-per-turn Cli; §8.1 HTTP/Gateway path deferred",
    ),
    BackendCLI(
        "reasonix",
        "orchestratord-reasonix",
        "spawn-per-turn Cli; event stream shape not yet exercised",
    ),
    BackendCLI(
        "zeroclaw",
        "orchestratord-zeroclaw",
        "spawn-per-turn Cli; event stream shape not yet exercised",
    ),
    # §8.2.1 ACP protocol family — six runtime identities sharing the
    # JSON-RPC 2.0 stdio adapter in orchestratord-acp. Registered so the
    # test-time CLI guard intercepts them too (§9.3: CI must never
    # execute user-installed agent CLIs).
    BackendCLI("grok", "orchestratord-acp", "ACP 家族；JSON-RPC 2.0 stdio"),
    BackendCLI("codebuddy", "orchestratord-acp", "ACP 家族；JSON-RPC 2.0 stdio"),
    BackendCLI("qwenpaw", "orchestratord-acp", "ACP 家族；JSON-RPC 2.0 stdio"),
    BackendCLI("qodercli", "orchestratord-acp", "ACP 家族；JSON-RPC 2.0 stdio"),
    BackendCLI("qoderclicn", "orchestratord-acp", "ACP 家族；JSON-RPC 2.0 stdio"),
    BackendCLI("deveco", "orchestratord-acp", "ACP 家族；JSON-RPC 2.0 stdio"),
)


def binary_names() -> tuple[str, ...]:
    """Return the bare binary names guarded at test time."""
    return tuple(c.binary for c in KNOWN_BACKEND_CLIS)


def lookup(binary: str) -> BackendCLI | None:
    """Return the ``BackendCLI`` entry for ``binary`` or ``None``."""
    for c in KNOWN_BACKEND_CLIS:
        if c.binary == binary:
            return c
    return None
