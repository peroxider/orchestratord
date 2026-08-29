# ADR-002 — Goal Mode (Ralph Loop) Integration for orchestratord-clawcodex

> **Status:** Proposed (2026-08-27)
> **Scope:** SPI extension + `orchestratord-clawcodex` backend wiring + `ModeRouter` keyword mapping. Single backend in scope; other backends get `goal_mode=False` until they ship a `GoalManager`-equivalent.
> **Source design:** [`DESIGN_agent_task_abstraction.md`](./DESIGN_agent_task_abstraction.md) + this ADR.

## Context

Today the orchestrator handles "complex tasks" exclusively through **multi-agent decomposition** (`modes/swarm.py`, `modes/coordinator.py`, `modes/pipeline.py`, `modes/debate.py`) — i.e. multiple backend sessions coordinated by an LLM planner (`task_decomposition/planner.py:124`). This works but adds latency, cost, and a planning failure mode that the user has to debug separately.

The agents we wrap **already ship their own single-session long-running goal mechanisms**:

- **`clawcodex`** (the InProcess backend we use as the reference impl) has a complete **Ralph-loop** `/goal` command at `clawcodex/src/goals/goals.py` (ported from Claude Code's `docs/en/goal`, donor `reference_projects/hermes-agent/hermes_cli/goals.py`). It is wired into:
  - `clawcodex/src/server/agent_server.py:2305-2716` — interactive server (`/goal`, `/subgoal`, `run_goal_command`)
  - `clawcodex/src/upstream/398b44f/entrypoints/headless.py:328-370` — headless `-p` mode (in-process Ralph loop)
- **Codex** has `thread/goal/{set,get,clear}` over the AppServer JSON-RPC schema (`codex app-server generate-json-schema` → `ClientRequest.json:5711-5720`, `ServerNotification.json:4117-4205`). Out of scope for this ADR.
- Anthropic Cloud `beta.sessions` / `beta.agents` (managed agents) — out of scope per product decision.

The current `orchestratord-clawcodex` backend (`backends/orchestratord-clawcodex/src/orchestratord_clawcodex/session.py:115`) imports `extensions.api.query.QueryRunner` and runs **one turn per `send()`** — it never instantiates `GoalManager`. We leave the loop's intelligence on the floor.

**Goal of this ADR:** let the orchestrator say "run this issue as a single long-running session that auto-continues toward a completion condition", and have `orchestratord-clawcodex` honor it by wrapping the existing `GoalManager` around the per-turn `QueryRunner` calls. The `ModeRouter` decides when to set the flag; the backend implements the loop; the SPI stays backend-agnostic so future backends (Codex AppServer thread/goal) can opt in without a second SPI redesign.

## Decisions

### 1. Add a `goal_mode` capability bit to the SPI

**Decision:** Extend `BackendCapabilities` and `SessionSpec` with three new fields; extend `EventKind` with five new event kinds. All defaults preserve current behavior.

```python
# src/orchestratord/spi/capabilities.py
@dataclass(frozen=True)
class BackendCapabilities:
    # ... existing 8 bits ...
    goal_mode: bool = False  # NEW — backend can drive an internal Ralph loop

# src/orchestratord/spi/backend.py
@dataclass
class SessionSpec:
    # ... existing fields ...
    goal_condition: str | None = None      # NEW — completion condition text
    goal_max_turns: int | None = None      # NEW — backstop, defaults to 20 in backend
    goal_subgoals: list[str] | None = None # NEW — additional criteria

# src/orchestratord/spi/events.py
class EventKind(Enum):
    # ... existing 8 kinds ...
    GOAL_SET        = "goal_set"         # NEW — condition accepted, Ralph loop started
    GOAL_STATUS     = "goal_status"      # NEW — periodic snapshot (turns used, tokens)
    GOAL_CONTINUE   = "goal_continue"    # NEW — judge said "continue", next turn injected
    GOAL_DONE       = "goal_done"        # NEW — judge said "done", goal achieved
    GOAL_CLEARED    = "goal_cleared"     # NEW — user / system cleared the goal
    GOAL_PAUSED     = "goal_paused"      # NEW — budget exhausted or parse-fail backstop
```

**Why:**

- The capability bit is the SPI contract that lets `ModeSelector` decide whether to set the flag. A backend that can't drive a loop advertises `goal_mode=False` and the orchestrator's existing swarm/coordinator path kicks in instead.
- `SessionSpec.goal_condition` is the only field the core ever has to write; the rest are tuning knobs that survive with their defaults.
- New event kinds give downstream consumers (dashboard, state_journal, rule_learner, audit) a hook into loop progression without reverse-engineering the `_run_turn` cycle.
- Defaults (`None` / `False`) mean **zero behavior change** for existing call sites that don't set them.

**Trade-off accepted:** five new event kinds inflate `EventKind`'s surface area. Acceptable because the kinds are orthogonal to the existing 8 (no kind is a generalization of an existing one) and downstream consumers are matched against enum values, not count.

**Future work (not in this ADR):** Codex AppServer's `thread/goal/*` and `thread/goal/{updated,cleared}` notifications should map to the same five new kinds. A separate ADR will own that mapping; the kinds chosen here are deliberately abstract enough to fit both.

### 2. `orchestratord-clawcodex` wraps `GoalManager` around per-turn `QueryRunner`

**Decision:** In `ClawcodexSession.__init__`, when `spec.goal_condition` is set, instantiate a `GoalManager` with a judge callable that delegates to the same provider as the main agent. In `_run_turn`, after the turn finishes, run the evaluate-continue loop inline until `GoalManager` reports `done` / `paused` / `inactive`.

```python
# backends/orchestratord-clawcodex/src/orchestratord_clawcodex/session.py (sketch)

def __init__(self, spec: SessionSpec) -> None:
    self._spec = spec
    self._goal_mgr: GoalManager | None = None
    if spec.goal_condition:
        from src.goals import GoalManager, build_judge_callable
        judge = build_judge_callable(spec.provider or "deepseek")
        self._goal_mgr = GoalManager(
            session_id=self.session_id,
            default_max_turns=spec.goal_max_turns or 20,
            judge=judge,
        )
        self._goal_mgr.set(
            spec.goal_condition,
            max_turns=spec.goal_max_turns,
            subgoals=spec.goal_subgoals or [],
        )
        self._add_event(EventKind.GOAL_SET, {
            "condition": spec.goal_condition[:4000],
            "max_turns": self._goal_mgr.state.max_turns,
            "subgoals": spec.goal_subgoals or [],
        })

async def _run_turn(self, text: str) -> None:
    # ... existing QueryRunner setup + event translation ...
    # After TURN_COMPLETE arrives:
    if self._goal_mgr and self._goal_mgr.is_active():
        evidence = collect_turn_evidence(self._events[-50:])  # last 50 envelopes
        decision = self._goal_mgr.evaluate_after_turn(
            evidence,
            tokens_now=self._cumulative_tokens,
            cost_now_usd=self._cumulative_cost_usd,
        )
        self._add_event(EventKind.GOAL_STATUS, {
            "status": decision["status"],
            "turns_used": self._goal_mgr.state.turns_used,
            "tokens_used": self._goal_mgr.state.spent_tokens,
        })
        if decision["should_continue"] and decision["continuation_prompt"]:
            self._add_event(EventKind.GOAL_CONTINUE, {
                "reason": decision["reason"],
                "next_turn_id": ...,  # seq of upcoming CONTINUATION_PROMPT turn
            })
            # schedule next turn with continuation prompt as user message
            await self._run_turn(decision["continuation_prompt"])
            return
        if decision["verdict"] == "done":
            self._add_event(EventKind.GOAL_DONE, {"reason": decision["reason"]})
        elif decision["verdict"] in ("timeout", "skipped"):
            self._add_event(EventKind.GOAL_PAUSED, {
                "reason": decision["reason"],
                "resume_hint": "/goal resume",
            })
```

**Why:**

- Reuses the existing, tested `GoalManager` (already wired in `agent_server.py:2317` and `headless.py:357`). Zero new state-machine logic in the orchestrator backend.
- Continuation prompt is enqueued as a normal user message (`CONTINUATION_PROMPT_TEMPLATE`) — preserves prompt caching per `goals.py:16-18` invariants.
- Reuses `apply_verdict`'s double-checked-locking semantics (`goals.py:871-...`) so concurrent `/goal clear` doesn't wedge the loop.
- The judge callable is built via `build_judge_callable(provider)` — same call site as `agent_server.py:2323`, so any provider clawcodex supports works (deepseek, anthropic, openai-codex, etc.).

**Trade-off accepted:** recursing into `_run_turn` from inside itself grows the asyncio task tree. Bound by `goal_max_turns` (default 20) so the worst case is 20 levels deep — well within any reasonable stack.

### 3. `ModeRouter` keyword → `goal_mode` mapping

**Decision:** Extend `ModeDecision` with `goal_condition: str | None`. Have `HeuristicRouter` set it when (and only when) the issue is routed to `single` mode AND its title/description matches a `_GOAL_KEYWORDS` set; otherwise leave it `None`. Don't override `pipeline` / `coordinator` / `debate` / `swarm` — those modes already imply multi-agent decomposition and the Ralph loop would be redundant (and dangerous if a swarm coordinator's sub-tasks share a session).

```python
# src/orchestratord/mode_router.py (extension)
_GOAL_KEYWORDS: frozenset[str] = frozenset({
    "achieve",
    "until",
    "keep going",
    "make sure",
    "verify",
    "until verified",
    "finish",
    "complete the",
})

@dataclass(frozen=True)
class RouterResult:
    mode: str
    reason: str
    confidence: float = 0.5
    goal_condition: str | None = None  # NEW
```

Mapping matrix:

| Router decision | `goal_condition` | Notes |
|---|---|---|
| `swarm` (existing) | `None` | Multi-agent wins; coordinator handles planning |
| `debate` (existing) | `None` | Multi-agent wins |
| `coordinator` (existing) | `None` | Multi-agent wins |
| `pipeline` (existing) | `None` | Multi-agent wins |
| `single` + `_GOAL_KEYWORDS` match | `issue.title` | Auto-loop the single session |
| `single` (default fallback) | `None` | Existing single-turn behavior |

**Why:**

- Avoids accidental double-planning: a coordinator that delegates to workers would otherwise run its Ralph loop *and* each worker would also loop — exponential token burn.
- Lets users opt in by writing issue titles like "Verify migration until all tests pass" — natural-language intent, no new label to remember.
- Leaves the existing `mode:swarm` / `mode:pipeline` labels as the explicit escape hatch for users who want the old behavior.

**Trade-off accepted:** keyword matching is heuristic. A user who writes a short goal-style title but actually wants swarm decomposition will be silently auto-looped. Mitigated by the `ModeDecision.mode_decision_reason` field already persisted on `IssueRecord` — operators can grep for "goal_condition" reasons to audit.

### 4. Judge model selection — provider default, no hardcoded model class

**Decision:** Build the judge callable via `build_judge_callable(spec.provider)` and pass **no model override** — the judge uses whatever default model the provider resolves to. We do **not** bind to any specific model name (`opus`, `haiku`, `sonnet`, `gpt-5`, etc.) at the SPI layer.

```python
# backends/orchestratord-clawcodex/src/orchestratord_clawcodex/session.py
if spec.goal_condition:
    from src.goals import GoalManager, build_judge_callable
    # spec.provider is a string like "deepseek" / "anthropic" / "openai-codex" /
    # "ollama-cloud" etc. — NEVER a model id. The judge resolves the provider's
    # own default model via build_judge_callable's internal chain.
    self._judge = build_judge_callable(spec.provider)
    self._goal_mgr = GoalManager(
        session_id=self.session_id,
        default_max_turns=spec.goal_max_turns or 20,
        judge=self._judge,
    )
```

**Why:**

- Provider-neutral: the same code path works whether the underlying provider is Anthropic, OpenAI, DeepSeek, Ollama, or a future addition. No review-cycle every time the provider changes its model lineup.
- Operator-tunable per provider, not per orchestrator version: if a deepseek user wants `deepseek-v4-flash` as judge, they set it in their deepseek provider config; an Anthropic user picks their default in `~/.claude/settings.json`. The orchestrator never has to know.
- Avoids "binding to claude-class" by name — `haiku`, `opus`, `sonnet` are provider-specific and would silently fail or fall through on non-Anthropic providers. Default-by-provider is the lowest-surprise rule.
- Keeps the SPI surface minimal — `goal_judge_model` can be added later if a workflow genuinely needs cross-provider override; today, no caller has asked for it.

**Trade-off accepted:** cost is unpredictable across providers (Anthropic default ≠ DeepSeek default). We accept this because (a) the main agent's per-turn cost dominates the judge cost anyway, (b) provider defaults are by definition what the operator already pays for, and (c) the knob can land later via `SessionSpec.extra["goal_judge_model"]` if real usage shows the default is wrong.

### 5. Goal state persistence via the existing state_journal

**Decision:** When `goal_mode` is on, `ClawcodexSession.close()` serializes `GoalManager.state.to_dict()` into `session_state.py` / `state_journal` alongside the events. Resume replays the state into a fresh `GoalManager` via `restore(state_dict)` before the first continuation turn.

**Why:**

- Existing crash recovery already replays the event stream; goal state is the missing piece for "did the loop budget get burned before the crash, or is there still room?"
- `GoalManager` was designed for this — `state.to_dict()` / `restore()` are stable surface in `goals.py:205-234` and `goals.py:799-822`.

**Trade-off accepted:** if the journal is wiped but the agent's underlying conversation survives (unusual), the loop restarts at turn 0. The `goal_max_turns` backstop caps the worst case.

#### 5.1 Workspace change clears goal state (resolves Q3)

**Decision:** When `ClawcodexSession` is asked to resume with a workspace path that differs from the one the goal was originally set on, the goal is **cleared**, not migrated:

1. Detect on `create_session()` / early in `_run_turn`: compare `spec.cwd` (or the resolved workspace fingerprint from `extensions.api.query`) against the value stored in `goal_state["workspace"]`.
2. On mismatch, call `self._goal_mgr.clear()`, emit `GOAL_CLEARED{reason:"workspace-changed", from_workspace, to_workspace}`, and `logger.warning(...)` the same payload at WARNING level so it surfaces in the operator's log stream.
3. Continue with the new session; no `restore()` call.

**Why:**

- A workspace change usually means a new branch / new PR / fresh issue retry — the old goal's "evidence collected" is no longer meaningful (file paths, test results, git refs all changed).
- Silent carry-over would burn tokens on a judge that's reading irrelevant context, and could mark the goal `done` based on stale evidence.
- Explicit clear + log gives the operator a clear audit trail: "yes, I know the goal dropped on workspace change, here is the diff".

**Trade-off accepted:** the user must re-set the goal via `/goal <condition>` (or via `ModeRouter` re-trigger) after a workspace switch. Acceptable because the same workflow.md that produced the original `goal_condition` can be re-applied on the new workspace — the keyword match in `ModeRouter` (§3) regenerates `goal_condition` for free.

### 6. Failure modes and degradation paths

| Failure | Detection | Behavior |
|---|---|---|
| Workspace untrusted / hooks disabled | `_goal_set_gate()` in `headless.py:339-356` | SET refused; emit `GOAL_CLEARED{reason:"gate-refused"}`; fall back to single-turn |
| Judge transport error | `judge_goal` catches → returns `verdict="continue"` (fail-open per `goals.py:18-20`) | Loop continues; logged as WARNING |
| Judge parse failure × `MAX_CONSECUTIVE_PARSE_FAILURES` (3) | `apply_verdict` increments counter | Auto-paused; emit `GOAL_PAUSED{reason:"judge-parse-failures"}`; user must `/goal resume` |
| Judge timeout > `DEFAULT_JUDGE_TIMEOUT_S` (30s) | `judge_goal` returns `verdict="timeout"` | Goal parked (no continuation); emit `GOAL_PAUSED`; loop does NOT auto-retry the judge |
| `goal_max_turns` exhausted | `apply_verdict` returns `should_continue=False` | Auto-**paused** (resolves Q2: keep `/goal resume` path alive); emit `GOAL_PAUSED{reason:"budget-exhausted"}`; user must `/goal resume` |
| Workspace changed across `create_session` / resume | `cwd` mismatch vs. `goal_state["workspace"]` (§5.1) | Goal **cleared**; emit `GOAL_CLEARED{reason:"workspace-changed", from_workspace, to_workspace}` + `logger.warning(...)` with the same payload |
| Backend has `goal_mode=False` (Codex, dsh, hermes, opencode V1) | `ModeSelector` reads capability bit | Router falls back to existing swarm/coordinator decomposition — **no** silent single-turn fallback for complex issues |
| Orchestrator restart mid-loop (same workspace) | `state_journal` replay in §5 | `GoalManager.restore(state)` rehydrates budget; loop continues from next turn |

Each row maps to a test case (§7).

### 7. Test matrix

**Unit (`tests/test_orchestrator_clawcodex_goal_mode.py`):**

1. Mock `GoalManager.evaluate_after_turn` returning `done` → assert `GOAL_DONE` event + `SESSION_COMPLETE`.
2. Mock `evaluate_after_turn` returning `should_continue=True` → assert `_run_turn` called again with `CONTINUATION_PROMPT_TEMPLATE` text; assert `GOAL_CONTINUE` event emitted before second turn.
3. Mock `evaluate_after_turn` returning `timeout` → assert `GOAL_PAUSED`, no second `_run_turn` invocation.
4. Mock `apply_verdict` raising on `parse_failed=True` ×3 → assert auto-paused after third call.
5. `SessionSpec(goal_condition="...", goal_max_turns=5)` → assert `GoalManager` constructed with `default_max_turns=5`.
6. `SessionSpec(goal_condition=None)` → assert `self._goal_mgr is None` and zero goal events emitted.
7. Mock `_goal_set_gate()` returning `"workspace-untrusted"` → assert SET refused, `GOAL_CLEARED{reason:"gate-refused"}` emitted.
8. Workspace-change clear (§5.1): construct session A with `cwd="/work/a"` + `goal_condition="..."`, close, then construct session B with `cwd="/work/b"` + same `resume_session_id` → assert `GOAL_CLEARED{reason:"workspace-changed"}` emitted and `caplog` captures a WARNING log line containing both `from_workspace="/work/a"` and `to_workspace="/work/b"`.

**Contract (`tests/test_capability_drift.py` extension):**

8. `EXPECTED["clawcodex"]["bits"]` adds `"goal_mode"`; `EXPECTED["codex|dsh|hermes|opencode"]["bits"]` stays unchanged → assert drift detector still passes.

**E2E (`tests/manual_e2e_clawcodex_goal_loop.py`, default skip, requires clawcodex source on PYTHONPATH):**

9. Start a session with `goal_condition="Print exactly 42 and stop"`; assert `GOAL_SET` → ≥1 `GOAL_CONTINUE` → `GOAL_DONE` → `SESSION_COMPLETE` in that order, with `TEXT` events containing "42".
10. Issue with title containing "verify" routed via `ModeSelector` → assert `ModeDecision.goal_condition == issue.title`.

### 8. Out of scope (explicitly excluded)

- **Path B — Anthropic Cloud `beta.sessions` / `beta.agents`:** excluded per product decision (cloud-only, managed agents, separate cost/compliance profile). Future ADR if scope reopens.
- **Goal mode for non-clawcodex backends (Codex / dsh / hermes / opencode):** each would need a backend-internal Ralph-loop equivalent. Codex already has `thread/goal/*` over AppServer (separate ADR); the other three have no equivalent and get `goal_mode=False` until they do.
- **Multi-session goals (one goal shared across two sessions):** not a Ralph-loop use case; defer to coordinator mode.
- **Interactive `/goal pause` / `/goal resume` from a CLI tool:** the `GoalManager` API supports it, but exposing it as a CLI subcommand is a UX project, not a backend change.
- **Goal-mode routing through the issue tracker:** `IssueRecord.mode_decision_reason` already logs why; no schema change needed.

## Appendix A — File changes summary

```
src/orchestratord/spi/capabilities.py        [改] +goal_mode: bool = False
src/orchestratord/spi/backend.py             [改] SessionSpec +goal_condition/goal_max_turns/goal_subgoals
src/orchestratord/spi/events.py              [改] EventKind +GOAL_SET/STATUS/CONTINUE/DONE/CLEARED/PAUSED

src/orchestratord/mode_router.py             [改] +_GOAL_KEYWORDS; RouterResult +goal_condition
src/orchestratord/mode_selector.py           [改] Map ModeDecision.goal_condition → spec.goal_condition

backends/orchestratord-clawcodex/src/orchestratord_clawcodex/backend.py   [改] capabilities +goal_mode=True
backends/orchestratord-clawcodex/src/orchestratord_clawcodex/session.py   [改] GoalManager 包装 + §5.1 workspace-change 检测

src/orchestratord/session_state.py           [改] state_journal 加 goal_state 字段 + restore 路径

tests/test_orchestrator_clawcodex_goal_mode.py   [新] 8 个 unit（§7 case 8 覆盖 workspace-change clear + WARNING log）
tests/test_capability_drift.py                   [改] add clawcodex "goal_mode" to EXPECTED
tests/manual_e2e_clawcodex_goal_loop.py          [新] 2 个 E2E (skip)

backends/orchestratord-{codex,dsh,hermes,opencode}/src/*/backend.py   [改]
    docstring 加 "Capabilities: ... goal_mode=False"（drift detector 用）

README.md                                    [改] 能力矩阵加 goal_mode 行
DESIGN_agent_task_abstraction.md             [改] 引用本 ADR 作为 §goal mode 设计依据
```

## Appendix B — Ralph-loop overview (for reviewers new to the concept)

The `clawcodex/src/goals/` module implements a **per-session completion loop**. Each turn, after the agent has produced output, a side-model call ("the evaluator") judges whether the user's free-text condition is satisfied by what the agent has surfaced in the conversation. If not, the driver auto-injects a continuation prompt citing the evaluator's reason, and the agent runs another turn. The loop terminates when:

- judge returns `done` → `GOAL_DONE` event
- `default_max_turns` exhausted → `GOAL_PAUSED{reason:"budget-exhausted"}`
- judge times out 3× → `GOAL_PAUSED{reason:"judge-timeout"}` (parked; will not auto-retry the judge)
- judge parse-fails 3× → `GOAL_PAUSED{reason:"judge-parse-failures"}`
- user types real input → continuation prompt is preempted, judge re-runs after that turn
- user calls `/goal clear` → `GOAL_CLEARED`

Two invariants from `goals.py:14-24` make the design robust:

1. **Continuation prompt is a normal user message** — no system-prompt mutation, prompt caching stays intact.
2. **Judge failures are fail-open for transport errors but fail-closed for parse failures** — a broken network never wedges the agent, but a misbehaving model gets paused after 3 strikes.

This ADR does not invent any of this — it only exposes the loop to the orchestrator. The Ralph-loop design, semantics, and invariants are owned by `clawcodex/src/goals/goals.py`; if those change, the corresponding tests in §7 must update.

## Appendix C — Resolved review questions

1. **Judge model (resolved 2026-08-27):** use the provider's default model — no hardcoded `opus` / `haiku` / `sonnet` / `gpt-5` / etc. The orchestrator passes `spec.provider` (a string like `"deepseek"` / `"anthropic"` / `"openai-codex"`), `build_judge_callable` resolves the provider's own default chain. Rationale and revised code example in §4.
2. **Auto-pause on budget exhaustion (resolved 2026-08-27):** keep the current conservative behavior — emit `GOAL_PAUSED{reason:"budget-exhausted"}` and require an explicit `/goal resume`. The loop stays alive (no `ERROR` event). Rationale: keeps the user in control and avoids spuriously failing a run that just hit the default budget but might have succeeded with more turns.
3. **Workspace-change goal migration (resolved 2026-08-27):** **clear** the goal on workspace change, do not migrate. Emit `GOAL_CLEARED{reason:"workspace-changed", from_workspace, to_workspace}` and `logger.warning(...)` the same payload. Implementation in §5.1, test case 8 in §7. The keyword-driven `ModeRouter` (§3) will regenerate `goal_condition` on the next pass if the issue still matches.
