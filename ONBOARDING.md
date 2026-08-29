# Onboarding

> **Audience:** new contributors, reviewers doing code archaeology, or anyone wondering "why is the project named `orchestratord` and not `orchestrator`?"
> **Goal:** explain the current package layout, SPI boundary, backend selection, and the checks required for a safe change.

> **Architecture rule:** the core orchestrator is backend-neutral. The supported dependency direction is `orchestrator core → SPI → backend entry point`; core code must not import a concrete backend or its vendor runtime.

## 1. Naming — `orchestratord`, not `orchestrator`

### 1.1 The `d` suffix

**`orchestratord` = `orchestrator` + `d`**, where `d` is the Unix/Python convention for **daemon** — a long-running background process.

This is the same convention used by:

| Daemon | Domain |
|---|---|
| `systemd` | system / service manager |
| `httpd` (Apache) | HTTP |
| `sshd` (OpenSSH) | SSH |
| `named` (BIND) | DNS |
| `crond` | cron scheduling |
| `cupsd` | printing |
| `snmpd` | SNMP |
| `dockerd` / `containerd` | containers |

The intent matches the package description in `pyproject.toml`:

> "Agent-agnostic **orchestration daemon** — multi-backend workflow engine …"

The trailing `d` is therefore **intentional**, not a typo. If you grep for `orchestratord` and feel like it's a typo, please don't open a PR to "fix" it without reading this section.

### 1.2 Why Python CLI tools often skip the `d`

Python ecosystem is split:

- **With `d`:** `supervisord` (process manager), and now `orchestratord`.
- **Without `d`:** `prefect`, `airflow`, `dagster`, `celery`, `uvicorn`, `gunicorn`.

The `d` suffix is more common for C/system services than for Python CLI tools, but it's not wrong for Python. We keep it because:
1. The product is a daemon (long-running), not a CLI invoker.
2. The `d` makes the deployment story self-documenting — a Dockerfile or systemd unit reading `orchestratord` immediately signals "this runs in the background".

### 1.3 Could we rename to `orchestrator`?

The package name is part of the public CLI and distribution contract. Do not rename it as a cleanup change; record any proposed rename in an ADR first.

**Verdict: not planned.** If you want to revisit, open an ADR rather than a silent rename PR.

## 2. Project layout — why `src/orchestratord/`?

### 2.1 The `src/` nesting

The repository uses the **src-layout** (PEP 517 recommended), not the flat-layout:

```
src-layout (what we use):    src/orchestratord/__init__.py   ← current
flat-layout (legacy):        orchestratord/__init__.py       ← alternative
```

The src-layout is the modern Python best practice, recommended by PyPA, Hynek Schlawack, and the official Python packaging guide. Reasons:

| Benefit | Why it matters |
|---|---|
| **Forces install-before-test** | `pytest` cannot accidentally import from `cwd/orchestratord/`; the source must be installed first. Catches "works on my machine" bugs. |
| **Avoids cwd shadowing** | A stray `orchestratord.py` in the working directory cannot shadow the installed package. |
| **Single import boundary** | Tests, mypy, and IDEs all see the same packaged code, not in-progress source. |
| **PEP 517 / setuptools default** | New projects should default to src-layout per the official packaging tutorial. |

### 2.2 Why we do NOT flatten it

Common "flatten it" arguments, all rebutted:

- *"Looks cleaner"* — aesthetic; tooling handles both.
- *"Tiny project, src is overkill"* — already 86 source files + 110+ tests, not tiny.
- *"Other languages put src at the top level"* — Rust/Go use different build systems; irrelevant to Python.
- *"We could use PEP 420 namespace packages"* — that's for multi-package monorepos, not applicable here.

If you want to remove `src/`, you'll need a stronger argument than aesthetics.

### 2.3 The full layout

```
orchestratord/                      ← repo root (NOT a Python package)
├── pyproject.toml                  ← build config + entry_points
├── src/
│   └── orchestratord/              ← the core Python package (this IS the package)
│       ├── __init__.py
│       ├── _version.py
│       ├── cli/                    ← Typer CLI commands
│       ├── spi/                    ← Service Provider Interface (AgentBackend etc.)
│       ├── backend_runner.py       ← backend-neutral session/event runner
│       ├── orchestration_subsystem.py ← top-level dependency wiring
│       ├── workflow_engine/
│       ├── linear/, local_tracker/ ← tracker adapters
│       └── …                       ← ~60 more modules
├── backends/                       ← each backend is its own PyPI package
│   ├── orchestratord-clawcodex/
│   │   └── src/orchestratord_clawcodex/
│   ├── orchestratord-codex/
│   ├── orchestratord-dsh/
│   ├── orchestratord-hermes/
│   └── orchestratord-opencode/
├── tests/                          ← core tests; each backend also has its own tests
├── COUPLING_audit.md               ← current coupling state (clawcodex etc.)
├── DESIGN_backends_hardening.md    ← in-flight backend improvements
├── ONBOARDING.md                   ← this file
└── README.md                       ← entry point
```

### 2.4 Where to find what

| You want to … | Look at |
|---|---|
| Add a new CLI subcommand | `src/orchestratord/cli/`, register in `cli/main.py` |
| Add a new SPI event type | `src/orchestratord/spi/events.py` (core) + every backend |
| Add a new backend | `backends/orchestratord-<name>/`, implement the SPI, add descriptor and entry points |
| Wire a backend into the orchestrator | `src/orchestratord/backend_registry.py` — resolve by descriptor; inject the result into `OrchestrationSubsystem` |
| Understand capability negotiation | `src/orchestratord/spi/capabilities.py:21` |
| Audit coupling to clawcodex | `COUPLING_audit.md` |
| See planned backend improvements | `DESIGN_backends_hardening.md` |

## 3. The SPI in 60 seconds

The orchestration core talks to backends through three protocols:

1. **`AgentBackend`** (`src/orchestratord/spi/backend.py`) — factory with `capabilities()` and `create_session(spec)`.
2. **`AgentSession`** (`src/orchestratord/spi/session.py`) — single conversation; output flows through `events()` and input via `send()`. The session protocol is backend-neutral.
3. **`BackendCapabilities`** (`src/orchestratord/spi/capabilities.py`) — declared capability bits. The core selects degradation behavior from these bits; backends do not silently self-degrade.
4. **`BackendDescriptor`** (`src/orchestratord/spi/backend_descriptor.py`) — the metadata source of truth for backend family, capabilities, and runtime identity.

Backends are discovered via Python entry points in the `orchestratord.backends` and `orchestratord.backend_descriptors` groups. The core resolves them through `backend_registry.py`; it must not import backend packages directly. This boundary is checked by `scripts/check-coupling.sh` and CI.

## 4. First-change walkthrough

Most onboarding PRs fall into one of these shapes:

### 4.1 "Add a new CLI flag"

```
src/orchestratord/cli/<command>.py   ← add the Typer option
src/orchestratord/cli/main.py        ← register the command (if new)
tests/test_orchestrator_<area>.py    ← add a test
```

Run `pytest tests/test_orchestrator_<area>.py` to verify.

### 4.2 "Add a new backend capability bit"

This is a **breaking** change because every backend must opt in. Procedure:

1. Add the bit to `BackendCapabilities` in `src/orchestratord/spi/capabilities.py`.
2. Add a fallback row to the "Core fallback" table in `README.md`.
3. Update each backend's `backend.py` to declare the new bit honestly.
4. Update the backend descriptor and its implementation together. `tests/test_capability_drift.py` verifies descriptor/implementation parity; there is no hand-maintained `EXPECTED` table.
5. If the core needs to react to the bit, add the branch to the orchestrator's degradation logic.

### 4.3 "Fix a coupling audit finding"

Start at `COUPLING_audit.md` and confirm the current mechanical checks before changing code. The fix is usually one of:

- Replace the vendor type with the SPI equivalent (for example, `QueryRunner` → `backend.create_session(spec)`).
- Move vendor-specific behavior into the corresponding backend package.
- Use dependency injection for optional integrations; a lazy import alone does not make a core dependency backend-neutral.

### 4.4 "Update a backend"

```
backends/orchestratord-<name>/
├── pyproject.toml
└── src/orchestratord_<name>/
    ├── __init__.py
    ├── backend.py     ← capabilities() + create_session() + dispose()
    └── session.py     ← AgentSession implementation; event translation
```

Backends are independent PyPI packages; their tests live alongside them in `backends/orchestratord-<name>/tests/` (or wherever the package author puts them).

## 5. CI gates you should know about

| Gate | What it checks | Where it lives |
|---|---|---|
| Capability drift detector | Each backend implementation matches its registered descriptor | `tests/test_capability_drift.py` |
| Coupling boundary check | No core/test imports of vendor extension modules, relative penetration imports, external absolute paths, or legacy product names | `scripts/check-coupling.sh` |
| SPI/layer tests | Core behavior is exercised with injected backend stubs | `tests/` and `tests/contracts/` |

Before opening a PR, run locally:

```bash
python -m pip install -e ".[dev]"
bash scripts/check-coupling.sh
python -m pytest -k capability_drift   # if you touched a backend's capabilities
ruff check .
python -m pytest
```

## 6. Where to ask questions

- **Naming / structure questions** — this file. If it's not here, add it.
- **Architecture decisions** — write an ADR (`ADR-NNN-<topic>.md` at the repo root).
- **Coupling state and required boundary checks** — `COUPLING_audit.md` and `scripts/check-coupling.sh`.
- **In-flight work** — `DESIGN_backends_hardening.md`.

If you're about to do a rename, refactor, or "cleanup" PR, skim `COUPLING_audit.md` first — most of what looks like "obvious cleanup" has already been audited and consciously deferred.
