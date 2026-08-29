# ADR-001 — Backend Hardening (Schemes A/B/C/D)

> **Status:** Accepted (2026-08-27)
> **Scope:** Backends `orchestratord-codex`, `orchestratord-opencode`, `orchestratord-dsh`, plus a CI drift detector spanning all 5 backends.
> **Source design:** [`DESIGN_backends_hardening.md`](./DESIGN_backends_hardening.md)

## Context

The 5-shipped backends had inconsistent capability bits, event-stream coverage, and error handling:

- `codex` advertised only 2/8 bits because its higher-capability `CodexAppServerSession` was never wired in.
- `dsh` swallowed exceptions into `SESSION_COMPLETE`, emitted no `ERROR` events, and lied about `cost_reporting=False` (the underlying `deepseek-harness-sdk` surfaces token usage).
- `opencode` declared `streaming_deltas=True, approval_hooks=True` but only consumed `POST /v1/chat` as a single `TEXT` payload — capability bits were aspirational, not real.
- No CI guardrail prevented a future PR from silently reverting a backend's bit set or family.

`DESIGN_backends_hardening.md` proposed 4 schemes (A, B, C, D) with a recommended C → A → B → D order. We followed that order and shipped all 4.

## Decisions

### 1. Capability probing over hard-coded configuration (Scheme A)

**Decision:** Probe `codex app-server --help` once at backend construction time and cache the result in `CodexBackend._runtime`.

**Why:**
- Older `codex` binaries that lack `app-server` keep working — they fall through to the existing `codex exec --json` Cli path (2/8 bits).
- The probe takes ≤2s, gated by `asyncio.wait_for` so a hung subprocess cannot stall `orchestratord backends list`. The 50–200 ms cost on cold start is acceptable.
- No new global state — the runtime decision lives on the `CodexBackend` instance.

**Trade-off accepted:** A "subcommand exists" probe is weaker than "runtime actually works" — a half-installed binary may get the 4/8 AppServer bits and fail at runtime. We accept this because `CodexAppServerSession.send()` already wraps its calls in try/except and emits an `ERROR` event on failure (`backends/orchestratord-codex/src/orchestratord_codex/app_server_session.py:100-107`).

**Future work (not in this ADR):** The `prefer` SPI override (`--prefer {cli,as}`) is mentioned in the design doc but not yet wired into `create_session`. Defer until there's an actual user need.

### 2. SSE over HTTP/1.1 chunked polling (Scheme B)

**Decision:** Open `opencode serve --port 0` (without `--pure`), discover the listening port via stderr regex, and consume `POST /v1/chat` as `text/event-stream` with `httpx.AsyncClient.stream`. Translate `data:` JSON frames into `TEXT_DELTA / TOOL_CALL / TOOL_RESULT / TURN_COMPLETE / ERROR` per the opencode protocol reverse-engineering in the design doc.

**Why:**
- The old implementation (`client.post(...).text`) violated the SPI's `streaming_deltas=True` contract — consumers couldn't get incremental tokens.
- We chose not to gate on a hypothetical `opencode >= x.y` minimum version because (a) no version manager exists yet, and (b) old binaries that return `Content-Type: application/json` automatically degrade to a single `TEXT` event — the consumer still sees the bytes.

**Trade-off accepted:** The SSE event names in §2.3 are an educated guess (no upstream protocol spec was available). If opencode renames an event, only `_ingest_sse` needs to change — the SPI surface is unaffected. The detector's bit set for opencode is locked in via the drift test, so a future rename that breaks streaming must update the docstring **and** the test in the same PR.

### 3. ERROR events + cost_reporting=True, both gated (Scheme C)

**Decision:**
- Emit `ERROR` events on (a) SDK init failure, (b) `harness.run()` exception, (c) non-success `finish_reason`. Then emit a single `SESSION_COMPLETE` with `reason="error"` or `"success"` depending on whether the last pre-finalize event was `ERROR`.
- Set `cost_reporting=True` optimistically (the SDK exposes token usage in our probe) and gate on a runtime probe (`_probe_cost_support`) — if `usage` is `None` across a sample run, downgrade back to `False`.

**Why:**
- Consumers were blind to dsh failures — every error path landed in `SESSION_COMPLETE` with `reason="success"`. Surfacing `ERROR` events lets the workflow engine react.
- `cost_reporting=False` was a documentation bug, not a real limitation. The probe-then-gate pattern lets us claim the bit honestly: "we know SDK ≥ X supports it, and we tested at boot".

**Trade-off accepted:** Optimistic `cost_reporting=True` may temporarily disagree with reality on a brand-new SDK release. The gate step (`_probe_cost_support`) runs at session construction and demotes it back if usage is missing — the drift detector still agrees because both the docstring and the actual capability degrade together.

### 4. Drift detector with hard-coded `EXPECTED` map (Scheme D)

**Decision:** A single parametrized test (`tests/test_capability_drift.py`) walks every backend's `Family:` / `Capabilities:` docstring markers and compares them against a hard-coded `EXPECTED` dict. Three contracts are pinned per backend:

1. `capabilities()` bit set equals `EXPECTED[name]["bits"]` (or, for `codex`, one of two acceptable variants).
2. The module docstring contains `Family:` and `Capabilities:` lines that the test can parse.
3. `_classify_family(caps)` agrees with the declared family.

**Why:**
- The `EXPECTED` map is the single source of truth. Every PR that changes a backend's bit set must also touch this test — that's the whole point.
- An "implemented" detector is one test file with no extra dependencies — only `pytest` and `importlib.metadata.entry_points`, both already in `[dev]`.

**Trade-off accepted:** The detector skips (rather than fails) backends not installed in the current environment. This is deliberate — a missing backend in dev install should not turn the detector red. The companion `test_capability_drift_map_is_complete` ensures a backend cannot silently enter the registry without an EXPECTED entry.

### 5. `[tool.uv.sources]` workspace map (Scheme D enabler)

**Decision:** Add `[tool.uv.sources]` to `pyproject.toml` so `uv pip install -e .[backends]` resolves the 5 backend packages to in-tree `backends/*` paths instead of attempting PyPI fetches.

**Why:**
- The public PyPI registry can't see local packages — without this, `uv pip install -e .[backends]` fails with `not found`. The workspace map makes "one command installs everything" a reality.
- Keeps `orchestratord[backends]` as a usable convenience extra in addition to listing packages explicitly (the README still documents the explicit form as the default).

**Trade-off accepted:** Editable installs have a slight import-time cost compared to wheel installs. Acceptable for dev/CI; production should `pip install` wheels, not use `-e`.

## Consequences

**Positive:**
- `codex`: 2/8 → 4/8 (when AppServer available); falls back to 2/8 cleanly.
- `dsh`: 2/8 → 3/8; event stream gains `ERROR`.
- `opencode`: bit set stays at 3/8, but now backed by real SSE translation (no more aspirational bits).
- All 5 backends now have machine-checkable `Family:` / `Capabilities:` markers.
- CI will fail on any future capability drift.

**Negative / accepted:**
- ~50–200 ms added to backend construction (codex AppServer probe).
- SSE event names are guessed — a protocol rename breaks only the opencode translation layer, not the SPI.
- `cost_reporting=True` on dsh is optimistic; gated by a runtime probe that demotes it if usage is missing.

**Deferred / out of scope:**
- `prefer={cli,as}` SPI override on CodexBackend.
- SPI extension for `COST` event kind (design doc §5.3 explicitly excludes).
- `manual_e2e_*.py` files (marked skip-by-default in design §1.6, §2.7).

## Verification

- 22/22 drift detector tests pass: `uv run pytest tests/test_capability_drift.py`
- 32/32 scheme tests pass: `uv run pytest tests/test_orchestrator_{dsh_error_propagation,dsh_capabilities,codex_runtime_probe,opencode_sse_translation}.py`
- README capability matrix and event-stream table reflect the new bits and event coverage.
- Independent verifier agent verdict: PASS across all 9 checks (drift tests, scheme tests, codex probe, opencode SSE translation, dsh ERROR branches, dsh cost_reporting, workspace sources, docstring markers, README updates).

## Alternatives considered

- **Static-only detection via AST parse of `backend.py`:** rejected because bit sets come from `capabilities()` at runtime (codex dual runtime, dsh cost gate) — the static parse would always disagree.
- **A per-backend file (`test_capability_drift_<name>.py`):** rejected because the scheme D design specifies a single detector with `EXPECTED` as the ground truth. Splitting files fragments the source of truth.
- **Replacing the docstring markers with a separate `capabilities.toml`:** rejected because keeping markers in the module docstring forces them to be reviewed alongside the bit-setting code in the same PR — which is the entire reason drift happens in the first place.