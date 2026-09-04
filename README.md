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

### Local Web console security

`orchestratord serve` binds to `127.0.0.1` by default. The Web console is a
single-operator control surface with no browser login and can approve tools or
stop running agent processes. Do not expose it directly to a LAN or the public
internet. If remote access is required, keep the daemon behind a trusted VPN or
an authenticating reverse proxy with TLS; daemon/runtime credentials remain
separate from the browser session.

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

## Visualization boundary

The dashboard consumes common history messages (`role` and `content` blocks)
and core SSE frames (`TextDelta`, `ToolCallEvent`, `ToolResultEvent`, and
lifecycle events). It does not decode an agent's native wire protocol.
Backend adapters translate native output to `EventEnvelope`; the core handles
persistence and browser delivery. New integrations must use that contract,
not send native JSONL disguised as `TextDelta`.

New text transcript rows retain `type: TextDelta`, so JSON examples remain
literal text both live and on replay. Old untyped Codex wire transcripts are
normalized read-only by `transcript_compat.py` before history reaches the
browser; no backend package is needed and stored files are not rewritten.
Historical format detection is best-effort because those old rows did not
record their format. Unsupported or malformed content remains visible.

This display boundary does not add backend control capabilities: pause,
resume, stop, and follow-up support still depend on the adapter and the core's
capability checks.

## Why

Different agent runtimes have very different capabilities — streaming deltas, session resume, tool-call approval, cost reporting, terminal takeover. Rather than force a lowest-common-denominator interface, orchestratord advertises a [capability matrix](#backend-capability-matrix) per backend and lets the **core** enforce uniform degradation paths (`src/orchestratord/spi/capabilities.py:21`). Backends never self-degrade; they report what they have.

## Naming

**`orchestratord` = `orchestrator` + `d`** — the trailing `d` is a Unix/Python convention for **daemon** (a long-running background process), not a typo. The pyproject description literally says "orchestration daemon", matching the `systemd` / `httpd` / `sshd` / `named` / `crond` / `dockerd` family. See [`ONBOARDING.md`](ONBOARDING.md#1-naming--project-layout) for the full rationale and the `src/orchestratord/` src-layout decision.

## Install

### One-click deploy (recommended)

For new users, the bundled `install.sh` script clones the repo, creates an isolated virtualenv at `~/.orchestratord/venv`, installs the core daemon, and optionally installs any backend plugins — including auto-detecting locally-built agent runtimes (e.g. `clawcodex` source) before installing their wrapper package.

```bash
git clone https://github.com/<org>/orchestratord
cd orchestratord
./install.sh                              # interactive: prompts to pick backends
./install.sh --backends clawcodex,codex   # non-interactive: specific backends
./install.sh --no-backends                # core daemon only
./install.sh --all-backends               # all backends
./install.sh --dry-run                    # preview without changes
```

The script:

1. Verifies prerequisites — Python ≥ 3.11, `git`, `pip`; uses `uv` automatically when available.
2. Creates `${ORCHESTRATORD_INSTALL_PREFIX:-~/.orchestratord}/venv` (use `--no-venv` to install into the current interpreter instead).
3. Installs orchestratord from the local checkout (editable) — or clones from `ORCHESTRATORD_REPO` / branch when invoked outside a source tree.
4. Probes each selected backend for its native runtime:

   | Backend     | Runtime check                                                   | Source / install hint                                                                  |
   | ----------- | --------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
   | `clawcodex` | `extensions/api/query.py` importable from `CLAWCODEX_SOURCE`     | local checkout (auto-detects `~/clawcodex`, `~/clawcodex-ascend`, `/opt/clawcodex`)    |
   | `claude`    | `claude` or `ccb` on `PATH`                                     | `npm i -g @anthropic-ai/claude-code`                                                   |
   | `codex`     | `codex` on `PATH`                                               | `npm i -g @openai/codex`                                                               |
   | `dsh`       | `deepseek-harness-sdk>=0.1.2rc1,<0.2` with its bundled runtime | install the `orchestratord-dsh` adapter (global `dsh` is not required) |
   | `hermes`    | `hermes` on `PATH`                                              | upstream repository                                                                    |
   | `opencode`  | `opencode` on `PATH`                                            | `npm i -g opencode`                                                                    |
   | `cursor`    | `cursor-agent` on `PATH`                                        | see https://cursor.com/cli — event stream shape not yet exercised, buffered as TEXT    |
   | `copilot`   | `copilot` on `PATH`                                             | `gh extension install github/gh-copilot` — event stream needs experimentation         |
   | `kimi`      | `kimi` on `PATH`                                                | https://platform.moonshot.cn — Chinese-language prompts are first-class                 |
   | `qwen`      | `qwen` on `PATH` (stream-json via `qwen -p --output-format stream-json`) | https://help.aliyun.com/zh/dashscope — only P1 backend with `streaming_deltas=True` |
   | `kiro-cli`  | `kiro` on `PATH`                                                | AWS Kiro CLI — event stream shape not yet exercised, output buffered as a single TEXT event |
   | `openclaw`  | `openclaw` on `PATH`                                            | OpenClaw CLI — §8.1 HTTP/Gateway path deferred, output buffered as TEXT |
   | `reasonix`  | `reasonix` on `PATH`                                            | Reasonix CLI — event stream shape not yet exercised |
   | `zeroclaw`  | `zeroclaw` on `PATH`                                            | ZeroClaw CLI — event stream shape not yet exercised |
   | `acp`       | any of `grok` / `codebuddy` / `qwenpaw` / `qodercli` / `qoderclicn` / `deveco` on `PATH` | generic stdio ACP backend (six vendor runtimes share `orchestratord-acp`) |

   > **Migration note (provider default):** `agent.provider` no longer
   > defaults to `"anthropic"` — the default is now empty and each backend
   > applies its own (dsh falls back to its stock `deepseek-official`
   > adapter). **clawcodex workflows MUST declare `agent.provider`
   > explicitly**; an unset provider fails preflight with
   > `agent.provider must be configured for clawcodex.`

   If a runtime is missing, the script prints the install hint and (in interactive mode) asks whether to install the wrapper package anyway.

5. Installs each backend wrapper from `backends/orchestratord-<name>/` (editable) or from PyPI.
6. Runs `orchestratord --help` and lists discovered backends / skills.

Activate the venv afterwards:

```bash
source ~/.orchestratord/activate.sh
# or, equivalently:
export PATH="$HOME/.orchestratord/venv/bin:$PATH"
```

Useful environment overrides:

```bash
ORCHESTRATORD_REPO=https://github.com/<org>/orchestratord   # clone URL when no local source
ORCHESTRATORD_BRANCH=main                                  # branch to checkout
ORCHESTRATORD_INSTALL_PREFIX=/opt/orchestratord            # override ~/.orchestratord
CLAWCODEX_SOURCE=/path/to/clawcodex                        # skip clawcodex auto-detection
NO_COLOR=1                                                 # disable colored output
```

See `./install.sh --help` for the full option list (custom prefix, specific Python interpreter, dry-run, etc.).

### Manual install (PyPI)

The core daemon and the backends are separate PyPI packages. Pick one (or more) backends:

```bash
# Core daemon only — no backends, nothing will run.
pip install orchestratord

# Add one backend (transitively pulls in orchestratord).
pip install orchestratord orchestratord-clawcodex     # isolated SDK worker
pip install orchestratord orchestratord-codex         # Codex CLI
pip install orchestratord orchestratord-dsh           # DeepSeek Harness SDK
pip install orchestratord orchestratord-hermes        # Hermes CLI
pip install orchestratord orchestratord-opencode      # OpenCode serve
pip install orchestratord orchestratord-cursor        # Cursor CLI (cursor-agent)
pip install orchestratord orchestratord-copilot       # GitHub Copilot CLI
pip install orchestratord orchestratord-kimi          # Kimi CLI (Moonshot AI)
pip install orchestratord orchestratord-qwen          # Qwen / DashScope CLI (stream-json)
pip install orchestratord orchestratord-kiro-cli      # AWS Kiro CLI
pip install orchestratord orchestratord-openclaw      # OpenClaw CLI
pip install orchestratord orchestratord-reasonix      # Reasonix CLI
pip install orchestratord orchestratord-zeroclaw      # ZeroClaw CLI
pip install orchestratord orchestratord-acp           # generic ACP stdio backend (grok / codebuddy / qwenpaw / qodercli / qoderclicn / deveco)
```

> The core package now ships `backends` as an extra that pulls in all six PyPI plugins, plus `dev` includes them as well so `pip install -e .[dev]` is enough to run the drift detector (`pyproject.toml:33`). Users who want a single backend still install it explicitly as shown above.

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
   │SdkProcess│  │  Cli  │  │ Sdk  │  │ Cli  │  │ Protocol   │
   └──────────┘  └───────┘  └──────┘  └──────┘  └────────────┘
        each is its own PyPI package, registers via
        [project.entry-points."orchestratord.backends"]
```

The core never imports backend packages directly — discovery is exclusively through `importlib.metadata` entry points in the `orchestratord.backends` group (`src/orchestratord/backend_registry.py:18`). This is CI-enforced.

The session SPI exposes only an async event iterator (`events()`) plus a `send()` for input — no `await run()` API. This split is deliberate so multi-agent modes (debate round-robin, pipeline) can inject a user message without waiting for completion (`src/orchestratord/spi/session.py:2`).

## Backend capability matrix

Each backend advertises a `BackendCapabilities` dataclass. A checkmark means the backend supports that capability; the orchestration core handles the corresponding degradation path when the bit is off.

| Capability bit        | clawcodex | codex (Cli / As)¹ | dsh | hermes | opencode | cursor | copilot | kimi | qwen | kiro-cli | openclaw | reasonix | zeroclaw | acp |
| --------------------- | :-------: | :---------------: | :-: | :----: | :------: | :----: | :-----: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| Family                | SdkProcess |   Cli / SdkProcess | Cli²|  Cli   | Protocol |  Cli   |   Cli   | Cli  | Cli  | Cli | Cli | Cli | Cli | Protocol |
| `streaming_deltas`    |     ✓     |      ✓ / ✓        |  ✓  |        |    ✓     |        |         |      |  ✓   |      |      |      |      |  ✓   |
| `resumable`           |     ✓     |      ✓ /          |     |   ✓    |          |        |         |      |      |      |      |      |      |      |
| `interrupt`           |           |        / ✓        |     |        |          |        |         |      |      |      |      |      |      |  ✓   |
| `approval_hooks`      |     ✓     |        / ✓        |     |        |    ✓     |        |         |      |      |      |      |      |      |  ✓   |
| `parallel_sessions`   |           |      ✓ / ✓        |  ✓  |   ✓    |    ✓     |   ✓    |    ✓    |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |
| `cost_reporting`      |     ✓     |                   |  ✓³ |        |          |        |         |      |      |      |      |      |      |      |
| `tool_filtering`      |     ✓     |                   |     |        |          |        |         |      |      |      |      |      |      |      |
| `takeover`            |     ✓     |                   |     |        |          |        |         |      |      |      |      |      |      |      |
| **Score**             |  **6/8**  |   **2/8** / **4/8** |**3/8**|**2/8**|  **3/8** |**1/8** | **1/8** |**1/8**|**2/8**| **1/8** | **1/8** | **1/8** | **1/8** | **4/8** |

¹ `codex` advertises two runtime modes. At construction time the backend probes `codex app-server --help`: a 0 exit means the AppServer JSON-RPC path is available and the backend lights up `streaming_deltas + interrupt + approval_hooks` (SdkProcess, 4/8). If the probe fails (older `codex` binaries, missing `codex` on PATH, or `--yolo`-style Cli-only installs), the backend falls back to `codex exec --json` and reports the historical 2/8 Cli bits. `backend_registry._classify_family()` returns `SdkProcess` in the first case and `Cli` in the second.

² `dsh` streams real deltas via its notification pump, but the SdkProcess heuristic requires `interrupt + approval_hooks + streaming_deltas` together, so `backend_registry._classify_family()` still classifies it as `Cli`. The descriptor mirrors this classification. Cross-process `resume` is off: the harness runtime has no remount protocol for persisted sessions (id collision).

³ DSH and the AgentSDK ClawCodex bridge report **token usage** on the `SESSION_COMPLETE` payload as `usage`; they do not fabricate USD. Other runtimes may supply `total_cost_usd`; the core extracts either form.

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

| EventKind         | clawcodex | codex (Cli / As) | dsh | hermes | opencode | cursor | copilot | kimi | qwen | kiro-cli | openclaw | reasonix | zeroclaw | acp |
| ----------------- | :-------: | :-------------: | :-: | :----: | :------: | :----: | :-----: | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| `TEXT_DELTA`      |     ✓     |       / ✓       |  ✓  |        |    ✓     |        |         |      |  ✓   |      |      |      |      |  ✓   |
| `TEXT`            |           |       ✓         |  ✓  |   ✓    |    ✓ (fallback) |  ✓ |    ✓    |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |      |
| `TOOL_CALL`       |     ✓     |                 |  ✓  |        |    ✓     |        |         |      |      |      |      |      |      |  ✓   |
| `TOOL_RESULT`     |     ✓     |                 |  ✓  |        |    ✓     |        |         |      |      |      |      |      |      |  ✓   |
| `APPROVAL_REQUEST`|     ✓     |       / ✓       |     |        |    ✓     |        |         |      |      |      |      |      |      |  ✓   |
| `TURN_COMPLETE`   |     ✓     |       ✓         |  ✓  |   ✓    |    ✓     |   ✓    |    ✓    |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |
| `PHASE_COMPLETE`  |     ✓     |                 |     |        |          |        |         |      |      |      |      |      |      |      |
| `SESSION_COMPLETE`|     ✓     |       ✓         |  ✓  |   ✓    |    ✓     |   ✓    |    ✓    |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |
| `ERROR`           |     ✓     |       ✓         |  ✓  |   ✓    |    ✓     |   ✓    |    ✓    |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |  ✓   |

Observations from reading the session modules:

- **`clawcodex`** wraps `QueryRunner` from the configured `CLAWCODEX_SOURCE` and translates its native text, tool and lifecycle events. Native permission waits are translated to `APPROVAL_REQUEST`; the core evaluates its approval policy and calls `approve()` to release the matching waiter. Preflight requires the source tree's query bridge to expose `ApprovalRequestEvent` and `QueryRunner.approve/cancel_pending_approvals`; an incomplete bridge fails before provider startup. The AgentSDK bridge enables approval callbacks explicitly for this adapter, while ordinary headless callers retain their non-interactive default. `interrupt()` remains unsupported and is therefore not advertised.
  With the AgentSDK structured query bridge, native message boundaries keep
  intermediate text and final snapshots from being duplicated; a model turn
  with multiple tools completes only after all its tool results. Native
  session IDs are reused for later `send()` calls, and terminal events carry
  actual token usage and errors. Native wire parsing stays in AgentSDK, not
  the browser or core. Authentication preflight checks saved OAuth status
  for `openai-codex`, and native API-key/keychain lookup for other providers;
  this local check does not refresh credentials or prove provider access.
  Each conversation runs in an owned Python worker rather than the daemon's
  interpreter. On POSIX, `pause()`/`resume()` suspend its local process tree;
  `close()` stops active execution and waits for the worker to exit. This
  does not suspend remote inference or provider billing. Daemon pipe loss
  terminates the worker, and queued followups use the core's common `send()`
  path. Saved native session IDs can resume in a replacement worker.
- **`codex`** ships two implementations selected at runtime. The backend probes `codex app-server --help` (`backends/orchestratord-codex/src/orchestratord_codex/backend.py`); a 0 exit wires up `CodexAppServerSession` (`backends/orchestratord-codex/src/orchestratord_codex/app_server_session.py`) with `streaming_deltas + interrupt + approval_hooks` (4/8, SdkProcess). A non-0 exit or missing binary falls back to `CodexSession` (2/8, Cli, `codex exec --json`).
- **`dsh`** wraps `deepseek-harness-sdk`. The SDK is synchronous, so each turn runs on a worker thread while a notification pump forwards `session.event` notifications into an asyncio queue — `send()` returns immediately and events flow incrementally (`backends/orchestratord-dsh/src/orchestratord_dsh/session.py`). It translates `assistant/chunk` (text/reasoning deltas → `TEXT_DELTA`), `assistant/message`, `tool/call`, `tool/result`, `turn/end`. As of `DESIGN_backends_hardening.md` Scheme C it also emits an `ERROR` branch when the SDK raises or `finish_reason` is non-success (`dsh_init_error` / `dsh_error` / `dsh_finish`, the latter carrying the real error message), and advertises `cost_reporting=True` backed by the token usage it accumulates onto the `SESSION_COMPLETE` payload.
  The adapter requires SDK `>=0.1.2rc1,<0.2` and uses the runtime's `sdk`
  profile; generated route/approval files are ID-merged patches, not copies
  of the runtime configuration. An explicit `agent.cordis` patch can mount
  a provider plugin with its own authentication; the generated approval patch
  is appended without replacing it or changing the selected sandbox mode.
  Native JSON tool arguments are normalized, and the Bash exit-code footer
  determines command failure even when the native transport says `isError=false`.
  Each `send()` reports only its own captured token usage, not cumulative
  usage from earlier requests or an invented dollar cost. SDK persistence defaults to
  `<workspace>/.reports/dsh-home`; an explicit `DSH_HOME` overrides it.
  POSIX pause/resume controls the owned runtime and tool process tree,
  including tools that create a new process session. This does not pause
  remote inference or provider billing. Stop kills active local execution
  and waits for the SDK worker to exit; normal completion retains the SDK's
  bounded shutdown flush before residual tool cleanup. Local process tests
  use the real SDK with a Python protocol stub, not a real model provider.
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

## Peer federation (Phase 1)

Two or more `orchestratord` daemons can federate: every daemon publishes
an Agent Card at `/.well-known/agent.json`, a remote daemon applies to
join a workspace, the operator accepts, and the accepted peer may then
append messages, create sessions, and stream `peer.*` events.
Normative sources: [`DESIGN_PEER_FEDERATION.md`](./DESIGN_PEER_FEDERATION.md)
and [`ADR-001-peer-federation.md`](./ADR-001-peer-federation.md).

### Quickstart: A1 (local) ⇄ B1 (remote)

```bash
# --- B1 (the remote daemon) --------------------------------------------
# Sharing one Redis across daemons gives cross-daemon event fan-out
# (AC7); without it each daemon stays single-process (AC13 unchanged).
ORCHESTRATORD_REDIS_URL=redis://localhost:6379/0 orchestratord serve --port 9000

# --- A1 (the local daemon) ---------------------------------------------
ORCHESTRATORD_REDIS_URL=redis://localhost:6379/0 orchestratord serve --port 9100
```

Find A1's workspace id (single-user mode needs no login):

```bash
curl -s http://127.0.0.1:9100/api/workspaces/by-slug/default | jq -r .workspace_id
```

B1 applies to join that workspace (run on B1):

```bash
orchestratord peer invite \
  --url http://127.0.0.1:9100 \
  --workspace-id <A1-workspace-uuid>
```

A1's operator accepts (run on A1) — the per-peer token is printed **once**
(D15):

```bash
orchestratord peer list --workspace-id <A1-workspace-uuid> --status pending
orchestratord peer accept --peer-id <peer-row-uuid>   # → token (shown once)
```

Short-cut for trusted LANs: put B1's `orch_id` in A1's
`ORCHESTRATORD_PEER_TRUST` (comma-separated allowlist, R10) **before** the
invite — the handshake then completes immediately and returns the token in
the invite response (HTTP 200 instead of 202).

The accepted peer calls A1's API with its token and identity header:

```bash
curl -X POST http://127.0.0.1:9100/api/peer/peers/<B1-orch_id>/invoke \
  -H "Authorization: Bearer <token>" \
  -H "X-Peer-Orchestrator-Id: <B1-orch_id>" \
  -d '{"method": "GET /api/workspaces/{workspace_id}/sessions",
       "body": {"workspace_id": "<A1-workspace-uuid>"}}'
```

Redis is only needed for the event fan-out. A minimal compose service:

```yaml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
```

### Token rotation SOP (manual, D11 / AC17)

Peer tokens never expire and are shown exactly once at accept time; there
is deliberately no automatic rotation. To rotate (compromise suspected,
operator churn, periodic hygiene):

1. Remove the peer — its access dies with the registry row (AC8):
   `orchestratord peer remove --orch-id <B1-orch_id> --workspace-id <A1-workspace-uuid>`.
   The old bearer token stops working immediately even though its
   `auth_tokens` row lingers (authentication also requires an `accepted`
   peer row).
2. B1 re-applies (`orchestratord peer invite ...`), A1 accepts again and
   receives a fresh one-time token.
3. Store the new token wherever B1 keeps its credentials (secret manager
   or B1's environment), and optionally delete the stale `auth_tokens`
   row named `peer:<B1-orch_id>` from A1's database.

Phase-1 boundary: `serve` does not yet bind the peer/1 frame listener;
cross-daemon traffic uses the HTTP endpoints above, and
`orchestratord peer group …` manages decentralized groups (D26) whose
membership is local state.

## IM Message Gateway

An optional gateway daemon that bridges orchestratord to IM channels (Feishu,
WeChat, Slack, Discord): chat messages become orchestrator input, and
orchestrator events flow back to the chat. v1 is POSIX/WSL only (UDS socket).

### Architecture

```
┌────────────────┐  UDS JSONL   ┌──────────────────────┐
│ orchestratord   │◄────────────►│  gateway daemon     │◄──► feishu / wechat /
│ (opt-in peer)  │  DELIVER /   │ ~/.orchestratord/    │     slack / discord
│                │  OUTBOUND    │ gateway/gateway.sock │     (channel adapters)
└────────────────┘              └──────────────────────┘
```

- The gateway runs as its own daemon (`orchestratord gateway start`); an
  orchestrator opts in with `server start --gateway` and registers as an IPC
  peer for the origins it binds (`im:direct:*:*` by default).
- Inbound: channel adapter → dedupe → classify → route → command allowlist →
  DELIVER push to the registered orchestrator.
- Outbound: the orchestrator event sink (events carry `issue_id`,
  `event_type`, `level`, and `markdown` metadata) → OUTBOUND frame → the
  gateway resolves the origin and delivers to the channel, with NACK
  backoff retry, outbox, and dead-letter handling.

### Install

The base install already covers the webhook channels (Feishu/Slack/Discord
webhooks) — `import orchestratord` never fails without extras. App-based
modes pull their SDKs through extras:

| Extra            | Adds                   | Needed for                              |
| ---------------- | ---------------------- | --------------------------------------- |
| `gateway-feishu` | `lark-oapi`, `qrcode`  | Feishu websocket mode + QR setup        |
| `gateway-wechat` | `cryptography`         | WeChat iLink channel                    |
| `gateway-all`    | both extras above      | everything                              |

```bash
pip install 'orchestratord[gateway-feishu]'
pip install 'orchestratord[gateway-wechat]'
pip install 'orchestratord[gateway-all]'
```

### Quick start (WSL)

```bash
# 1. Configure channels — interactive wizard (add/edit/remove; feishu scan
#    or manual, wechat QR login, slack/discord webhook fields)
orchestratord gateway setup

# 2. Start the daemon and check its health
orchestratord gateway start
orchestratord gateway status

# 3. Attach an orchestrator
orchestratord server start --workflow WORKFLOW.md --backend codex --gateway
```

`--gateway` opts the orchestrator into all supported direct/private messages;
`--gateway-origin` narrows the binding and `--gateway-sock` relocates the
socket (env equivalents: `ORCHESTRATORD_GATEWAY_ORIGIN` /
`ORCHESTRATORD_GATEWAY_SOCK`; the same flags work on `daemon start`). A
running orchestrator can attach or detach without a restart — these write
control files that the daemon picks up:

```bash
orchestratord server connect-gateway
orchestratord server disconnect-gateway
```

Per-channel operations:

```bash
orchestratord gateway login wechat        # WeChat iLink QR login
orchestratord gateway restart feishu      # rebuild one channel adapter
orchestratord gateway disconnect feishu   # drop that channel connection
orchestratord gateway stop                # stop the daemon
```

### IM command surface

Inbound messages are routed by semantics:

| Semantics     | Meaning                                                             |
| ------------- | ------------------------------------------------------------------- |
| `newPrompt`   | plain text while idle → a new prompt                                 |
| `command`     | whitelisted slash command                                            |
| `followUp`    | plain text while a session is busy → queued follow-up                |
| `approval`    | structured approval reply (bound wait-point; a bare "yes" is not)     |
| `interrupt`   | structured interrupt — never guessed from natural language            |
| `contextOnly` | context recorded for the operator without triggering a run           |

Slash commands recognized for the orchestrator host:

```
/server status
/issue list|show|tail|stop|pause|resume|clarify|inject|feedback|review|retry|workspace|rebase
/agent retry|follow-up|unblock
/pause  /resume  /stop  /takeover  /inject  /detach  /clarify  /review  /feedback
```

Commands outside the allowlist are not pushed to the orchestrator; the sender
gets a bounded notice instead.

### Configuration reference

Channels are configured in `~/.orchestratord/gateway/channels.yaml` (normally
maintained by `gateway setup`). Key fields:

```yaml
enabled: true
state_dir: ~/.orchestratord/gateway
channels:
  - name: slack-main
    type: slack
    webhook_url: https://hooks.slack.com/services/...
    enabled: true
```

State-dir layout (`~/.orchestratord/gateway`):

| File                        | Purpose                                     |
| --------------------------- | ------------------------------------------- |
| `channels.yaml`             | channel + reliability configuration         |
| `gateway.pid`, `gateway.lock` | daemon PID and single-instance lock        |
| `gateway.sock`              | UDS JSONL IPC socket                        |
| `health.json`               | daemon health snapshot                      |
| `gateway.log`               | rotating daemon log                         |
| `processed_inbound.ndjson`, `outbox.ndjson`, `dead_letter.ndjson` | dedupe / deferred outbound / exhausted retries |
| `audit.ndjson`              | redacted audit trail (secrets never logged)  |

Environment variables:

| Variable                          | Purpose                                            |
| --------------------------------- | -------------------------------------------------- |
| `ORCHESTRATORD_IM_SECRET`         | Fernet key encrypting WeChat credentials at rest   |
| `ORCHESTRATORD_GATEWAY_ORIGIN`    | default gateway origin for `--gateway` opt-in      |
| `ORCHESTRATORD_GATEWAY_SOCK`      | gateway socket override (client and daemon)       |
| `ORCHESTRATORD_GATEWAY_LOG_LEVEL` | pin the daemon log level (`INFO`, `DEBUG`, …)     |
| `ORCHESTRATORD_DEBUG`             | set to `1` for DEBUG logging                       |

### FAQ

**What if the gateway is not running when I start `server ... --gateway`?**
The orchestrator does not crash — it keeps retrying by heartbeat and attaches
as soon as the gateway daemon appears.

**Why is a timed-out outbound message not retried?**
A timeout is ambiguous: the message may already be in the chat, and retrying
could duplicate it. Timed-out sends stay pending without automatic resend,
while explicit NACK failures retry with backoff (dead-lettered when exhausted).

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

The thirteen guarded binaries today:

| Binary | Backend package | Notes |
| --- | --- | --- |
| `clawcodex-dev` | `orchestratord-clawcodex` | isolated Python SDK worker; capability probe |
| `codex` | `orchestratord-codex` | dual-path: `codex app-server` (SdkProcess) or `codex exec --json` (Cli) |
| `hermes` | `orchestratord-hermes` | spawn-per-turn Cli |
| `opencode` | `orchestratord-opencode` | `opencode serve --port 0` + SSE |
| `dsh` | `orchestratord-dsh` | `deepseek-harness-sdk` subprocess |
| `cursor-agent` | `orchestratord-cursor` | spawn-per-turn Cli; event stream shape not yet exercised |
| `copilot` | `orchestratord-copilot` | spawn-per-turn Cli; 事件流需实验 |
| `kimi` | `orchestratord-kimi` | spawn-per-turn Cli; 中文 prompt 友好 |
| `qwen` | `orchestratord-qwen` | spawn-per-turn Cli; `qwen -p --output-format stream-json` 真正流式 |
| `kiro` | `orchestratord-kiro-cli` | spawn-per-turn Cli; event stream shape not yet exercised |
| `openclaw` | `orchestratord-openclaw` | spawn-per-turn Cli; §8.1 HTTP/Gateway path deferred |
| `reasonix` | `orchestratord-reasonix` | spawn-per-turn Cli; event stream shape not yet exercised |
| `zeroclaw` | `orchestratord-zeroclaw` | spawn-per-turn Cli; event stream shape not yet exercised |

New to the project? Start with [`ONBOARDING.md`](ONBOARDING.md) for the project layout, the `d` suffix rationale, the SPI contract, and where to make your first change.

## License

MIT.
