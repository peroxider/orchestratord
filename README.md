# orchestratord

Agent-agnostic orchestration daemon — a multi-backend workflow engine with issue tracking, PR automation, and multi-agent modes.

The orchestration core is business-neutral. Issue-to-PR is the first bundled
application; action plugins can also implement training, inference, evaluation,
publication, or agent-evolution stages.

## CLI resource model

The canonical CLI separates runtime resources from business applications:

```bash
orchestratord daemon start --workflow WORKFLOW.md --backend codex
orchestratord workflow validate workflow.yaml
orchestratord workflow show workflow.yaml
orchestratord run start workflow.yaml --backend codex --input-file task.json
orchestratord run logs --id RUN_ID
orchestratord backend list
orchestratord backend doctor codex
orchestratord app list
orchestratord app issue-pr review --id ISSUE_ID --approve
```

`server` and `issue` remain compatibility command groups. New integrations
should use `daemon`, `run`, and `app issue-pr`.

Declarative stages may use the built-in `agent`, `gate`, and `decision` kinds,
or a namespaced action registered through the `orchestratord.actions` Python
entry-point group:

```yaml
stages:
  - id: 1
    name: train
    uses: ml.train
    with:
      gpu: 4
  - id: 2
    name: evaluate
    uses: ml.evaluate
    depends_on: [1]
```

The core daemon coordinates agent sessions through a small SPI (Service Provider Interface). Concrete backends (ClawCodex, OpenAI Codex, DeepSeek Harness, Hermes, OpenCode…) ship as independent PyPI plugins and are discovered at runtime via Python entry points.

## Why

Different agent runtimes have very different capabilities — streaming deltas, session resume, tool-call approval, cost reporting, terminal takeover. Rather than force a lowest-common-denominator interface, orchestratord advertises a [capability matrix](#backend-capability-matrix) per backend and lets the **core** enforce uniform degradation paths (`src/orchestratord/spi/capabilities.py:21`). Backends never self-degrade; they report what they have.

## Naming

**`orchestratord` = `orchestrator` + `d`** — the trailing `d` is a Unix/Python convention for **daemon** (a long-running background process), not a typo. The pyproject description literally says "orchestration daemon", matching the `systemd` / `httpd` / `sshd` / `named` / `crond` / `dockerd` family. See [`ONBOARDING.md`](ONBOARDING.md#1-naming--project-layout) for the full rationale and the `src/orchestratord/` src-layout decision.

## Install

The core daemon and the backends are separate PyPI packages. Pick one (or more) backends:

```bash
# Core daemon only — no backends, nothing will run.
pip install orchestratord

# Add one backend (transitively pulls in orchestratord).
pip install orchestratord orchestratord-clawcodex     # InProcess, reference impl
pip install orchestratord orchestratord-codex         # Codex CLI
pip install orchestratord orchestratord-dsh           # DeepSeek Harness SDK
pip install orchestratord orchestratord-hermes        # Hermes CLI
pip install orchestratord orchestratord-opencode      # OpenCode serve
```

> The core package now ships `backends` as an extra that pulls in all five PyPI plugins, plus `dev` includes them as well so `pip install -e .[dev]` is enough to run the drift detector (`pyproject.toml:33`). Users who want a single backend still install it explicitly as shown above.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                orchestratord (core)                     │
│  ┌──────────┐  ┌────────────┐  ┌────────────────────┐   │
│  │ CLI      │  │ Workflow   │  │ Backend registry   │   │
│  │ (Typer)  │  │ engine     │  │ (entry_points)     │   │
│  └──────────┘  └────────────┘  └────────┬───────────┘   │
└─────────────────────────────────────────┼──────────────┘
                                          │ discovers
        ┌─────────────────────────────────┼───────────────────┐
        │                                 │                   │
   ┌────▼─────┐  ┌───────┐  ┌──────┐  ┌──────┐  ┌────────────┐
   │clawcodex │  │ codex │  │ dsh  │  │hermes│  │ opencode   │
   │InProcess │  │  Cli  │  │ Sdk  │  │ Cli  │  │ Protocol   │
   └──────────┘  └───────┘  └──────┘  └──────┘  └────────────┘
        each is its own PyPI package, registers via
        [project.entry-points."orchestratord.backends"]
```

The core never imports backend packages directly — discovery is exclusively through `importlib.metadata` entry points in the `orchestratord.backends` group (`src/orchestratord/backend_registry.py:18`). This is CI-enforced.

The session SPI exposes only an async event iterator (`events()`) plus a `send()` for input — no `await run()` API. This split is deliberate so multi-agent modes (debate round-robin, pipeline) can inject a user message without waiting for completion (`src/orchestratord/spi/session.py:2`).

## Backend capability matrix

Each backend advertises a `BackendCapabilities` dataclass. A checkmark means the backend supports that capability; the orchestration core handles the corresponding degradation path when the bit is off.

| Capability bit        | clawcodex | codex (Cli / As)¹ | dsh | hermes | opencode |
| --------------------- | :-------: | :---------------: | :-: | :----: | :------: |
| Family (heuristic)    | InProcess |   Cli / SdkProcess | Cli²|  Cli   | Protocol |
| `streaming_deltas`    |     ✓     |      ✓ / ✓        |     |        |    ✓     |
| `resumable`           |           |      ✓ /          |  ✓  |   ✓    |          |
| `interrupt`           |           |        / ✓        |     |        |          |
| `approval_hooks`      |     ✓     |        / ✓        |     |        |    ✓     |
| `parallel_sessions`   |           |      ✓ / ✓        |  ✓  |   ✓    |    ✓     |
| `cost_reporting`      |     ✓     |                   |  ✓  |        |          |
| `tool_filtering`      |     ✓     |                   |     |        |          |
| `takeover`            |     ✓     |                   |     |        |          |
| **Score**             |  **6/8**  |   **2/8** / **4/8** |**3/8**|**2/8**|  **3/8** |

¹ `codex` advertises two runtime modes. At construction time the backend probes `codex app-server --help`: a 0 exit means the AppServer JSON-RPC path is available and the backend lights up `streaming_deltas + interrupt + approval_hooks` (SdkProcess, 4/8). If the probe fails (older `codex` binaries, missing `codex` on PATH, or `--yolo`-style Cli-only installs), the backend falls back to `codex exec --json` and reports the historical 2/8 Cli bits. `backend_registry._classify_family()` returns `SdkProcess` in the first case and `Cli` in the second.

² `dsh` documents itself as `SdkProcess` but its capability bits do not satisfy the SdkProcess heuristic (which requires `interrupt + approval_hooks + streaming_deltas`), so `backend_registry._classify_family()` classifies it as `Cli`. A mismatch between self-description and reported capabilities.

**Capability degradation is enforced by the core, not by backends** (`src/orchestratord/spi/capabilities.py:21`):

| Bit off              | Core fallback                                                        |
| -------------------- | -------------------------------------------------------------------- |
| `streaming_deltas`   | Splits whole `text` into pseudo-deltas                              |
| `resumable`          | Replays session log as the next prompt                               |
| `interrupt`          | Marks the turn "abandoned", discards on turn-complete (best-effort) |
| `approval_hooks`     | Pre-filters dangerous tools via `tool_filtering` + post-hoc audit    |
| `tool_filtering`     | DENYs unauthorized tools in the approval callback                   |
| `cost_reporting`     | Falls back to a token estimator                                     |
| `parallel_sessions`  | Serializes sessions                                                  |
| `takeover`           | Disables terminal-takeover mode                                     |

## Event stream comparison

The session SPI exposes a single async iterator of `EventEnvelope` events. Backends translate their native streams into this normalized form (`src/orchestratord/spi/events.py:14-23`). The table below records which `EventKind` values each backend actually emits today — declared capability bits and emitted events are not the same thing.

| EventKind         | clawcodex | codex (Cli / As) | dsh | hermes | opencode |
| ----------------- | :-------: | :-------------: | :-: | :----: | :------: |
| `TEXT_DELTA`      |     ✓     |       / ✓       |     |        |    ✓     |
| `TEXT`            |           |       ✓         |  ✓  |   ✓    |    ✓ (fallback) |
| `TOOL_CALL`       |     ✓     |                 |  ✓  |        |    ✓     |
| `TOOL_RESULT`     |     ✓     |                 |  ✓  |        |    ✓     |
| `TURN_COMPLETE`   |     ✓     |       ✓         |  ✓  |   ✓    |    ✓     |
| `PHASE_COMPLETE`  |     ✓     |                 |     |        |          |
| `SESSION_COMPLETE`|     ✓     |       ✓         |  ✓  |   ✓    |    ✓     |
| `ERROR`           |     ✓     |       ✓         |  ✓  |   ✓    |    ✓     |

Observations from reading the session modules:

- **`clawcodex`** is the only backend that emits the full tool lifecycle (`TOOL_CALL` + `TOOL_RESULT`) and the only one that emits `TEXT_DELTA` and `PHASE_COMPLETE`. Its `interrupt()` and `approve()` are no-ops by design — clawcodex handles approval natively in its tool system, so the bits are advertised but the SPI calls intentionally do nothing (`backends/orchestratord-clawcodex/src/orchestratord_clawcodex/session.py:156`).
- **`codex`** ships two implementations selected at runtime. The backend probes `codex app-server --help` (`backends/orchestratord-codex/src/orchestratord_codex/backend.py`); a 0 exit wires up `CodexAppServerSession` (`backends/orchestratord-codex/src/orchestratord_codex/app_server_session.py`) with `streaming_deltas + interrupt + approval_hooks` (4/8, SdkProcess). A non-0 exit or missing binary falls back to `CodexSession` (2/8, Cli, `codex exec --json`).
- **`dsh`** wraps `deepseek-harness-sdk`. The SDK is synchronous, so all harness calls are dispatched via `asyncio.to_thread` (`backends/orchestratord-dsh/src/orchestratord_dsh/session.py:67`). It translates `assistant/message`, `tool/call`, `tool/result`, `turn/end`. As of `DESIGN_backends_hardening.md` Scheme C it also emits an `ERROR` branch when the SDK raises or `finish_reason` is non-success (`dsh_init_error` / `dsh_error` / `dsh_finish`), and advertises `cost_reporting=True` because the SDK surfaces token usage.
- **`hermes`** is the simplest CLI backend. It always passes `--yolo` (auto-approve) and `--pass-session-id` for resume. Tool events are not translated; only `TEXT/ERROR` are emitted. The docstring notes an upgrade path: "if hermes gateway protocol opens, migrate to Protocol family".
- **`opencode`** spawns `opencode serve --port 0` and discovers the listening line via stderr regex. As of `DESIGN_backends_hardening.md` Scheme B, `send()` opens `POST /v1/chat` with `Accept: text/event-stream` and translates each `data:` frame into `TEXT_DELTA / TOOL_CALL / TOOL_RESULT / TURN_COMPLETE / ERROR`; `approval.request` frames are cached in `_pending_approvals` and forwarded through `session.approve()`. Old opencode binaries that return a non-SSE response degrade to a single `TEXT` event so consumers still see the bytes.

## Agent-callable skills

Skills are first-class objects the agent can consult at runtime (`DESIGN_agent_callable_skills.md`). Each skill lives in `src/orchestratord/skills/builtin/<name>/`:

- `SKILL.md` — YAML frontmatter (`name` / `description` required) + agent-visible Markdown documentation.
- `references/source-map.md` — verifiable references: each claim is pinned to a source file, line range, and SHA256 hash prefix. The loader verifies these at load time; a mismatch marks the skill **stale**.
- `tools.py` — optional Python helpers exposed with the skill.

At session start `BackendRunner` injects a one-line-per-skill index into the system prompt (best-effort — injection failure never aborts a run); the agent fetches a full body via the `load_skill(name)` tool.

Manage skills from the CLI:

```bash
orchestratord skills list     # name / stale status / description
orchestratord skills show <name>
orchestratord skills verify   # exit 1 if any source-map reference is stale (CI guard)
```

If a pinned reference drifts after editing sources, refresh the hashes with:

```bash
python scripts/regen_source_map.py
```

## Development

```bash
git clone https://github.com/<org>/orchestratord
cd orchestratord
pip install -e ".[dev]"

pytest              # unit tests
ruff check .        # lint
```

Requires Python ≥ 3.11 and < 3.14.

### Test-time CLI guard

`pytest` automatically prepends a directory of stub executables to `$PATH` so that any accidental `subprocess.run(["codex", …])` (or `clawcodex-dev` / `hermes` / `opencode` / `dsh`) inside a test fails fast with **exit 126** and a `[backend-cli-guard]` line on stderr — instead of silently shelling out to a real agent binary on a developer's machine or in CI.

The guard is wired by [`tests/conftest.py`](tests/conftest.py) (autouse fixture `_backend_cli_guard_path`) and reads the binary list from [`orchestratord._backend_cli_registry`](src/orchestratord/_backend_cli_registry.py). Both are covered by [`tests/test_backend_cli_guard.py`](tests/test_backend_cli_guard.py); CI drift between the registry and what backend source actually spawns is enforced by three tests in [`tests/test_capability_drift.py`](tests/test_capability_drift.py) (`test_backend_cli_registry_covers_subprocess_invocations`, `test_backend_cli_registry_no_orphaned_packages`, `test_backend_cli_registry_warns_on_unused_binaries`). See [`DESIGN_backend_cli_test_guard.md`](DESIGN_backend_cli_test_guard.md) for the full design.

Three ways to opt out of the guard, when a test intentionally needs the real CLI:

| Escape hatch | Use case |
| --- | --- |
| Test lives under `tests/manual_e2e_*.py` | Hand-driven end-to-end suites that ship their own `pytest.mark.uses_real_cli` |
| `@pytest.mark.uses_real_cli` on a test | Any test that genuinely must invoke a backend binary |
| `ORCHESTRATORD_SKIP_CLI_GUARD=1` env var | CI debugging, or running the drift-detector job on a host that *does* have the binaries |

```bash
# Run the full suite with the guard enabled (default).
pytest

# Disable the guard entirely (e.g. for the drift-detector CI job).
ORCHESTRATORD_SKIP_CLI_GUARD=1 pytest

# Run a hand-driven manual E2E — the guard is off by marker.
pytest tests/manual_e2e_opencode_sse.py -v -s
```

The five guarded binaries today:

| Binary | Backend package | Notes |
| --- | --- | --- |
| `clawcodex-dev` | `orchestratord-clawcodex` | in-process SDK; capability probe |
| `codex` | `orchestratord-codex` | dual-path: `codex app-server` (SdkProcess) or `codex exec --json` (Cli) |
| `hermes` | `orchestratord-hermes` | spawn-per-turn Cli |
| `opencode` | `orchestratord-opencode` | `opencode serve --port 0` + SSE |
| `dsh` | `orchestratord-dsh` | `deepseek-harness-sdk` subprocess |

New to the project? Start with [`ONBOARDING.md`](ONBOARDING.md) for the project layout, the `d` suffix rationale, the SPI contract, and where to make your first change.

## License

MIT.
