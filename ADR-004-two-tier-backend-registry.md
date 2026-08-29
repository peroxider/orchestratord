# ADR-004 — Two-Tier Backend Registry (Descriptor + Implementation)

> **Status:** Accepted (2026-08-28)
> **Scope:** Core SPI (`orchestratord.spi.backend_descriptor`), the
> `backend_registry` module, all five backend packages
> (`orchestratord-{clawcodex,codex,dsh,hermes,opencode}`), and the CI drift
> detector (`tests/test_capability_drift.py`).
> **Source design:** [`DESIGN_two_tier_backend_registry.md`](./DESIGN_two_tier_backend_registry.md)
> **Supersedes:** §4.7 of ADR-001 (the hard-coded `EXPECTED` dict).

## Context

`backend_registry.py` (post-ADR-001) and the drift detector
(`tests/test_capability_drift.py`) carried backend information in two
parallel sources that drifted independently:

1. **Each backend's `backend.py`** had a `Family:` / `Capabilities:`
   docstring pair the drift detector parsed via `inspect.getsource`.
3. **`tests/test_capability_drift.py::EXPECTED`** held the same data as a
   hand-maintained `dict[str, dict[str, object]]`.

ADR-001 §4.7 described the dict as "the single source of truth", but in
practice every backend change required touching both files in the same PR —
exactly the synchronization hazard the dict was supposed to prevent.

A second, related smell: `backend_registry._classify_family` used a
capability-bit heuristic to derive `family` (`takeover → InProcess`,
`streaming_deltas + interrupt + approval_hooks → SdkProcess`, etc.).
`dsh` documented itself as `SdkProcess` but the heuristic classified it
as `Cli` because the SDK doesn't light up the trio — so the backend
docstring deliberately lied to make the drift detector agree. The
heuristic was wrong about the world; the docstring was wrong about
itself.

`DESIGN_two_tier_backend_registry.md` proposed four schemes (A, B,
C, D). We accepted all four.

## Decisions

### 1. `BackendDescriptor` is the single source of truth (Scheme A + B)

**Decision:** Every backend package exports a frozen
`orchestratord.spi.backend_descriptor.BackendDescriptor` constant via a new
`orchestratord.backend_descriptors` entry-point group. The descriptor
holds all metadata: `family` / `display_name` / `capabilities` /
`cli_command` / `cli_args_probe` / `env_prefix` / `launch_header` /
`model_discovery` / `extra_metadata`. The implementation class on the
existing `orchestratord.backends` group continues to expose `name` /
`capabilities()` / `create_session()` / `dispose()` behavior.

**Why:**

- **One source, one update path.** A backend author changing a
  capability bit edits only their `descriptor.py` — the drift detector
  picks up the change automatically.
- **Family becomes explicit.** No more heuristic guessing; no more dsh
  declaring one thing and the registry classifying it as another. The
  descriptor's `BackendFamily` enum value is what
  `list_backends()` reports and what downstream dispatchers key on.
- **Descriptors load without I/O.** Module-level constants — no
  network, no subprocess. Drift detection and `list_backends()` work
  even on a machine with zero backends installed.
- **Codex's dynamic runtime is expressible.** Two descriptors
  (`codex-cli` / `codex-app-server`) share one `CodexBackend`
  implementation; the probe picks which runtime the backend
  materializes, and the drift test accepts either bit set as the
  "active" descriptor for that package.

**Trade-off accepted:** Two entry-point groups per backend package
(five descriptors × two groups = ten registrations) is more
boilerplate than the old single-group setup. We accept the cost because
the synchronization hazard it eliminates is real and ongoing — every
backend PR was already touching two places.

### 2. `resolve_backend()` is the single production entry (Scheme C)

**Decision:** `orchestratord.backend_registry.resolve_backend(identifier,
*, strict=False)` is the canonical way to obtain an `AgentBackend`
instance. It:

1. Looks up the descriptor by `identifier`.
2. Resolves the implementation class via
   `_resolve_implementation_class(desc.backend_package)`.
3. If `strict=True`, asserts `backend.capabilities()` matches
   `descriptor.capabilities` exactly and raises
   `BackendMismatchError` on drift.
4. Wraps the result in `DegradingBackend` and returns it.

The old `discover_backends()` is kept as a deprecated alias for
backward compatibility (returns `{backend.name: backend}`); existing
callers will migrate in follow-up PRs.

**Why:**

- One function owns "find the descriptor" + "find the implementation"
  + "wire them together" + "enforce the drift contract".
- `strict=False` is the default for runtime use (drift is an
  alarm, not a hard failure); `strict=True` is opt-in for
  diagnostics / CI.

**Trade-off accepted:** `resolve_backend()` raises
`BackendNotFoundError` (a `LookupError`) for both "no such descriptor"
and "descriptor present but implementation missing". A finer-grained
"implementation missing" exception would help debugging but is not
worth the API surface yet — the error message carries the package
name in both cases.

### 3. Drift detector compares descriptors to implementations, not strings (Scheme D)

**Decision:** `tests/test_capability_drift.py` is rewritten to:

   * iterate every registered descriptor and assert
     `backend.capabilities() == descriptor.capabilities` (with a
     same-package "any-of" carve-out for codex's dual descriptors),
   * cross-check `every descriptor has an implementation` and
     `every backend has a descriptor`,
   * re-use `dataclasses.fields(BackendCapabilities)` so adding a new
     capability bit extends the test automatically.

The hard-coded `EXPECTED` dict, the docstring regex parser, and
`_extract_docstring_lines` are deleted. Each backend's
`backend.py` no longer carries the `Family:` / `Capabilities:`
contract markers — its docstring now points readers to the descriptor.

**Why:**

- The dict was the synchronization hazard we set out to remove. Now
  there is no dict.
- Adding a new capability bit to `BackendCapabilities` no longer
  requires updating `CAPABILITY_FIELD_NAMES` — the dataclass
  iteration picks it up.
- Removing docstring contract markers frees the module docstring
  to describe behavior (e.g. codex's runtime probe sequence) rather
  than re-state metadata that lives elsewhere.

**Trade-off accepted:** Codex's parametrized test now has a
"skip when sibling matches" branch because only one of its two
  descriptors is "live" at any given moment. The skip message names the
  matching sibling so a reviewer can still see which descriptor
  asserted the match.

### 4. `_classify_family` heuristic is removed

**Decision:** The `backend_registry._classify_family` capability-bit
heuristic is deleted from the codebase. `list_backends()` reads
`descriptor.family.value` directly.

**Why:**

- The heuristic was the original source of the dsh family
  disagreement. Removing it eliminates the false-positive
  drift signal that dsh was silencing by lying in its docstring.
- `grep _classify_backend` returns zero hits in the new code path;
  the migration is complete.

**Trade-off accepted:** Any code that imported
`_classify_family` from `backend_registry` would now fail. A
repository-wide grep confirms no such imports exist (the function
was always internal).

## Field reference

`BackendDescriptor` (frozen, declared in
`src/orchestratord/spi/backend_descriptor.py`):

| Field             | Type                       | Notes                                          |
|-------------------|----------------------------|------------------------------------------------|
| `name`            | `str`                      | Global descriptor key (e.g. `"clawcodex-dev"`) |
| `display_name`    | `str`                      | User-facing name                               |
| `family`          | `BackendFamily`            | `InProcess` / `SdkProcess` / `Protocol` / `Cli` |
| `backend_package` | `str`                      | Hyphenated distro name (e.g. `"orchestratord-clawcodex"`) |
| `capabilities`    | `frozenset[str]`           | Declared capability bits                       |
| `cli_command`     | `str \| None`              | `subprocess.Popen[0]`; `None` for InProcess    |
| `cli_args_probe`  | `tuple[str, ...]`          | Default `()`                                   |
| `env_prefix`      | `str \| None`              | Backend-specific env namespace                 |
| `launch_header`   | `str \| None`              | Banner logged at startup                       |
| `model_discovery` | `"static" \| "probe" \| "user"` | Default `"user"`                          |
| `extra_metadata`  | `dict[str, str]`           | Default `{}`                                   |

## Entry-point registration per backend

| Package                       | `orchestratord.backends` (impl) | `orchestratord.backend_descriptors` (descriptor)                            |
|-------------------------------|---------------------------------|-----------------------------------------------------------------------------|
| `orchestratord-clawcodex`     | `clawcodex → ClawcodexBackend`  | `clawcodex-dev → CLAWCODEX_DEV_DESCRIPTOR`                                   |
| `orchestratord-codex`         | `codex → CodexBackend`          | `codex-cli → CODEX_CLI_DESCRIPTOR`, `codex-app-server → CODEX_APP_SERVER_DESCRIPTOR` |
| `orchestratord-dsh`           | `dsh → DshBackend`              | `dsh → DSH_DESCRIPTOR`                                                        |
| `orchestratord-hermes`        | `hermes → HermesBackend`        | `hermes → HERMES_DESCRIPTOR`                                                  |
| `orchestratord-opencode`      | `opencode → OpenCodeBackend`    | `opencode → OPENCODE_DESCRIPTOR`                                              |

## Consequences

- `tests/test_capability_drift.py` now has no `EXPECTED` dict, no
  docstring parser, and no `CAPABILITY_FIELD_NAMES` tuple.
- The drift test reads `BackendCapabilities.__dataclass_fields__`
  directly via `dataclasses.fields(caps)`, so a future capability bit
  (e.g. ADR-003's `resume_detection`, which is now in the
  `clawcodex-dev` / `codex-app-server` / `opencode` descriptors) is
  covered automatically.
- `orchestratord backends list` continues to emit
  `name` / `display_name` / `family` strings; `list_backends()` now
  also returns `backend_package`. Downstream dispatchers do not need
  changes.
- The deprecated `discover_backends()` alias is intentionally not
  removed yet — `cli/server.py` and other call sites migrate in
  follow-up PRs (mirrors the gradual migration pattern from
  ADR-001).

## Future work

- Merge `orchestratord._backend_cli_registry.KNOWN_BACKEND_CLIS` into
  the descriptor (each descriptor's `cli_command` becomes the single
  source of the PATH guard registry).
- Add a `resume_detection` capability bit to the remaining three
  descriptors (`codex-cli`, `dsh`, `hermes`) once ADR-003 is fully
  shipped; today those backends report `resume_detection=False`.
- `orchestratord backends show <name>` CLI subcommand to print the
  full descriptor as YAML for diagnostics.
- Consider a `static` `model_list: tuple[str, ...]` field for
  `model_discovery="static"` descriptors.