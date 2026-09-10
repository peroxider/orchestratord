# ADR-001: Peer Federation Protocol for orchestratord

- **Status**: Accepted v3 (2026-09-08) — Phase 1 implemented & verified
- **Scope**: Cross-process / cross-machine communication between orchestratord daemons
- **Source design**: [`DESIGN_PEER_FEDERATION.md`](./DESIGN_PEER_FEDERATION.md) (v3)

## Context

orchestratord is currently a single-daemon, single-process system. The
existing IPC stack (`ipc/gateway/1`, UDS + JSONL) is bound to the local
machine and serves only the IM gateway (Feishu/Lark/WeCom). There is no
protocol or implementation for one orchestratord daemon to call, query,
or subscribe to events from another daemon over a network.

A user goal on 2026-09-08 explicitly asks for a "join-a-group" style
federation: A1 (local) should be able to connect to B1 (LAN) or A2 (local
sibling), have B1's operator authorize the connection, and then invoke
B1's HTTP API, read its session/message history, and let agents on either
side exchange messages. The design doc lists 5 goals (G1-G5), 5 non-goals
(NG1-NG5), 8 risks (R1-R8), and 12 acceptance criteria (AC1-AC12).

This ADR records the architecture-level decisions that constrain
implementation. Detailed protocol frames and file changes live in the
linked design doc.

## Decisions

### D1: Borrow A2A field naming for Agent Card (revised v3 from "A2A-compatible")

- **What** (revised 2026-09-08 v3): Every daemon publishes an Agent Card
  at `/.well-known/agent.json` whose **JSON field names** are borrowed
  from A2A spec §4.x (`name`, `description`, `url`, `version`, `provider`,
  `capabilities[]`, `defaultInputModes`, `skills[].id+description`), but
  `protocol_version="peer/1"` is explicitly **not** A2A; we do not
  advertise A2A compatibility, do not implement JSON-RPC transport
  binding, do not adopt A2A task state machine, do not implement A2A
  streaming/artifact protocol.
- **Trade-off accepted**: We give up "drop-in compatibility with any
  A2A-conformant agent" (a feature Phase 1 does not need) in exchange
  for (a) zero lock-in to A2A spec evolution, (b) ability to evolve
  the protocol to fit orchestratord-specific needs (kernel decoupling,
  chat dispatcher, skill loader) without external RFC churn, (c)
  ~zero migration cost if we ever need to add A2A later — the field
  names already match.
- **Rejected alternative (A)**: Full A2A compatibility (adopt JSON-RPC,
  task state machine, streaming). Loses evolution freedom; orchestratord
  is a single-org project where external standard org process is a net
  negative value (§14.1 of DESIGN).
- **Rejected alternative (B)**: Pure custom `peer/1` with no A2A field
  borrowing. Saves the borrowed field names but loses the migration
  safety net (§14.3 of DESIGN lists 4 reverse-trigger conditions).
- **Rejected alternative (C)** (v3 added): libp2p peer-store. P2P-native
  but requires every operator to understand DHT and crypto keys —
  overkill for the LAN/VPN target environment.

### D2: HTTPS + JSONL as Phase 1 transport; libp2p/ANP deferred to Phase 4

- **What**: All inter-daemon traffic is HTTPS with newline-delimited
  JSON frames. WebSocket is not used (orchestratord's local WS uses
  `127.0.0.1` only).
- **Trade-off accepted**: HTTPS adds TLS overhead vs UDS but is the
  lowest-friction way to cross machine boundaries without inventing a
  custom transport. NAT traversal is not solved in Phase 1 — operators
  are expected to be on a VPN or trusted LAN.
- **Rejected alternative (A)**: libp2p from day one. Engineering cost
  ~3x higher; most operators don't need NAT traversal.
- **Rejected alternative (B)**: Adopt Matrix federation like AgentTeams.
  Adds an entire new homeserver (Tuwunel) to the deployment surface;
  orchestratord already has Slack/Lark channel integrations, so chat
  federation is a separate problem.

### D3: Bearer token + HMAC-SHA256 + nonce-replay-guard for authentication

- **What**: Each peer pair shares a long-lived bearer token (SHA-256 hash
  stored in `auth_tokens.scopes=["peer.*"]`). Every frame carries an
  HMAC-SHA256 signature over `(orch_id, frame_id, body, ts)` using the
  bearer token as key, plus a one-time nonce stored in a local SQLite
  table (±60 second timestamp window).
- **Trade-off accepted**: A leaked peer token is equivalent to a full
  workspace credential — but scopes (`peer.read` / `peer.invoke` /
  `peer.approve`) are issued separately to minimize blast radius (R6).
- **Rejected alternative (A)**: Mutual TLS everywhere. Strongest auth
  but requires a CA, certificate rotation, and per-daemon certs —
  impractical for a self-hosted LAN deployment.
- **Rejected alternative (B)**: DID signatures (ANP-style). Strongest
  cryptographic identity but ecosystem is still nascent; defer to Phase 4.

### D4: "Join-a-group" handshake — synchronous discovery, asynchronous accept

- **What**: A1 fetches B1's Agent Card synchronously, posts an invite
  containing its own Agent Card. B1's operator MUST explicitly accept
  the invite (via inbox item or CLI) before any INVOKE succeeds. Trust
  bypass only via `ORCHESTRATORD_PEER_TRUST` environment variable
  pre-shared by both operators.
- **Trade-off accepted**: Operators pay a one-time "accept" cost in
  exchange for zero risk of silent join by a stranger. The white-list
  env var provides a scriptable escape hatch for trusted pairs.
- **Rejected alternative (A)**: mDNS auto-discovery + auto-accept.
  Convenient but allows any node on the LAN to silently join — a
  significant security regression.

### D5: Peer layer is API-only; orchestrator / workflow_orchestrator / modes / kernel / agent are read-only

- **What**: All peer-layer code lives in `src/orchestratord/peer/` and
  `src/orchestratord/api/routers/peer.py`. The five mechanism-core
  paths are explicitly excluded from Phase 1 changes. **Note**: The
  dispatcher (`peer/dispatcher.py`) auto-schedule hook is wired only
  through the existing public `BackendRunner.run` entry point — no
  kernel/orchestrator internals are touched.
- **Trade-off accepted**: Some features (cross-daemon @mention agent
  turn scheduling) require touching `chat_daemon.py` — but only via the
  documented public surface, never via kernel internals.
- **Rejected alternative**: Touch the orchestrator now to "make it
  federation-ready." Violates the kernel/business split the project is
  actively building (see `DESIGN_ORCHESTRATION_BUSINESS_DECOUPLING.md`)
  and creates technical debt.

### D6: "Peer" naming; avoid "Phase B" collision

- **What**: Feature is called "Peer Federation" or "Peer 层". The
  design is referred to as the "Peer Layer", not "Phase B".
- **Trade-off accepted**: Less colloquial than "Phase B" but the term
  "Phase B" is already used in `docs/FEATURE_GAP_VS_MULTICA.md:41`
  for the chat + Slack/Lark communication layer. Name collision
  would create ambiguity in roadmap documents.

### D7: RealtimeBroker backend abstraction in Phase 1 (revised)

- **What** (revised 2026-09-08 v2): `RealtimeBroker` is refactored to
  take an injected `RealtimeBackend`. `LocalBackend` preserves current
  behaviour; `RedisBackend` subscribes to Redis pub/sub and re-injects
  events into the local broker. Cross-daemon events flow
  `B1 RealtimeBroker → RedisBackend.publish → Redis pub/sub →
  A1 RedisBackend.subscribe → A1 RealtimeBroker → A1 /ws subscribers`.
  Per-agent topics use the `peer.agent.{id}.events` prefix.
- **Trade-off accepted**: Phase 1 now requires Redis as a deployment
  dependency (operator self-hosts single-instance Redis; R9). This is a
  meaningful ops cost in exchange for real-time cross-daemon events
  reaching A1's existing `/ws` subscribers — which is what the user
  goal explicitly asked for ("实时看到 B1 状态").
- **Rejected alternative (A)** (previous v1 design): Keep `RealtimeBroker`
  single-process; defer abstraction to Phase 2. User rejected because
  R8 (A1's WS subscribers can't see B1 events) is a core UX requirement,
  not a nice-to-have.
- **Rejected alternative (B)**: Replace `/ws` with cross-daemon SSE
  only. Breaks existing browser UX; user wants WS continuity (D5).

### D8: Remote message auto-schedule, gated by `peer.auto_schedule`

- **What** (new 2026-09-08 v2): When B1's operator sets
  `peer.auto_schedule=true` (default `false`), inbound peer messages
  not only persist to `messages` but also trigger `BackendRunner.run`
  to schedule a new turn on B1. Concurrency is capped via
  `MAX_CONCURRENT_PEER_TURNS` (default 3); overflow returns HTTP 429
  to the calling peer.
- **Trade-off accepted**: B1 incurs real token cost when auto-schedule
  is on — the operator must explicitly opt in. When off, behaviour is
  identical to the previous v1 design (R1 mitigation: "only persisted,
  UI marks as awaiting dispatcher").
- **Rejected alternative**: Auto-schedule always-on. Security and
  resource exhaustion risk; user explicitly wants the gate.
- **Rejected alternative**: Auto-schedule deferred to Phase 3. User
  rejected because the gate is the safety mechanism — once the gate
  exists, Phase 1 is sufficient.

### D9: Browser-to-remote-daemon connection NOT enforced (revised)

- **What** (revised 2026-09-08 v2): The Peer Federation layer does not
  enforce "browser only connects to own daemon". If the operator
  configures CORS, the browser can connect directly to a remote
  daemon's `/ws` or REST API. The default Web UI routes through own
  daemon for convenience; this is an operational convention, not a
  code-level restriction.
- **Trade-off accepted**: Cross-daemon calls via the browser bypass
  A1's audit log. Mitigation: any direct browser→remote path must
  still carry a `peer_call_id` (R5, AC6) — the remote daemon writes
  the audit row regardless of which client originated the call.
- **Rejected alternative** (previous v1): Code-level restriction in the
  browser bundle. User rejected because (a) browsers in trusted LANs
  can naturally reach any daemon if CORS is set, and (b) artificial
  restriction breaks legitimate operator workflows.

### D10: Workspace boundary — same-ws natural, cross-ws explicit (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D14): Peer interconnect is
  **permitted across workspaces**, but the friction differs: peers in the
  same workspace can be auto-discovered via `peers.json` without a
  workspace-id field; peers in different workspaces must go through the
  full invite → accept handshake and carry an explicit `workspace_id`
  field in the registry row.
- **Trade-off accepted**: We give up the simple invariant "peers are
  always workspace-scoped" in exchange for letting operators explicitly
  federate across workspace boundaries (e.g., a `dev-workspace` daemon
  wants to pull agents from a `tools-workspace` daemon). The full
  handshake on cross-ws is the friction that prevents accidental
  cross-workspace data leakage.
- **Rejected alternative (A)**: Workspace-scoped only (cross-ws not
  allowed). Strictest, but operators who genuinely need cross-ws
  federation must spin up a third workspace — clunky.
- **Rejected alternative (B)**: No workspace boundary at all (any peer
  can connect). Maximum convenience, but operators cannot prevent
  cross-workspace data flow via config — security regression.

### D11: Manual token rotation only (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D15): Phase 1 implements
  manual token rotation: operator updates the token in `peers.json` on
  both sides and restarts each daemon. No automatic rotation, no grace
  period, no token-age policy.
- **Trade-off accepted**: Operators bear the operational cost of manual
  rotation in exchange for (a) zero new code surface for token-rotation
  state machines, (b) zero risk of "rotation broke authentication
  silently" bugs, (c) clear mental model (token = config value). R15
  captures the window-risk SOP: rotate A1 first, verify handshake still
  works, then rotate B1.
- **Rejected alternative (A)**: Automatic rotation with grace period
  (two valid tokens during rotation). Reduces ops toil but adds a
  rotation-state machine, requires per-token expiry tracking, and risks
  silent breakage if grace expiry passes unnoticed.
- **Rejected alternative (B)**: JWT-style tokens with built-in expiry.
  Solves rotation but breaks the existing `AuthToken` schema (`scopes`
  is a free-form field, not a claims bag); migration cost is high and
  the benefit is not yet validated by operator feedback.

### D12: Group granularity = orchestrator, not agent (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D16): Group membership is
  tracked at `orch_id` granularity, not per-agent. A single daemon can
  simultaneously belong to N groups. Per-agent event routing within a
  group is preserved via `peer.agent.{id}.events` topics.
- **Trade-off accepted**: We lose the ability to "join a group on behalf
  of a single agent" in exchange for matching the user's mental model
  ("一个编排器加群" — orchestrator as the joiner) and avoiding per-agent
  group-membership bookkeeping. Per-agent routing still works because
  topics are per-agent regardless of group membership.
- **Rejected alternative**: Per-agent group membership. Mismatches
  user intuition; complicates the registry schema with no clear benefit
  (per-agent ACLs can be done at INVOKE time, not group time).

### D13: Cross-daemon session creation allowed (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D17): A1 can create new
  sessions on B1 via `POST /api/peer/peers/{orch-id}/sessions`. The
  audit log row carries `invited_by_orch_id` and
  `invited_by_peer_call_id` so the creator is traceable end-to-end.
- **Trade-off accepted**: We give operators a high-privilege endpoint
  (creating a session on a remote daemon is a meaningful side-effect)
  in exchange for matching the "A1's agents can interact with B1's
  agents" requirement (which needs session creation, not just
  messaging existing sessions). Mitigation: the endpoint requires
  `peer.invoke` scope AND the operator who created the workspace on
  B1 must approve the peer (existing invite handshake).
- **Rejected alternative**: A1 can only message existing B1 sessions.
  Insufficient — agents often need to spawn fresh sessions for new
  conversations.
- **Rejected alternative**: A1 can create sessions but not via the
  peer router (must go through some out-of-band channel). Brittle and
  inverts the protocol layering.

### D14: At-least-once delivery + per-`msg_id` dedup (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D18): Every INVOKE/RESULT
  frame carries a `msg_id` (uuid4). B1 maintains a 30-second sliding
  dedup window keyed by `(msg_id, orch_id)`; a duplicate INVOKE returns
  the cached RESULT instead of re-executing the underlying operation.
- **Trade-off accepted**: We accept that duplicate INVOKEs within the
  window are silently coalesced (operator may briefly wonder "did my
  call go through?") in exchange for at-least-once semantics over an
  unreliable HTTPS transport — and protection against HMAC-nonce
  reuse bugs.
- **Rejected alternative (A)**: At-most-once (drop duplicates silently).
  Safer but loses messages on network jitter; user explicitly wants
  reliable delivery.
- **Rejected alternative (B)**: Exactly-once. Requires two-phase commit
  semantics across two daemons; not implementable without major
  distributed-systems infrastructure.

### D15: Per-session monotonic ordering; cross-session unordered (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D19): Each INVOKE within a
  session carries an `ordering` field (monotonically increasing per
  `(session_id, orch_id)`); B1 warns on gaps and marks
  `out_of_order=true` on audit, but does not block. Cross-session
  ordering is undefined.
- **Trade-off accepted**: We accept "best-effort ordering within a
  session" in exchange for not implementing a global sequence number
  service. Most orchestratord flows (chat messages, agent turns) are
  session-scoped anyway; cross-session ordering is rarely meaningful.
- **Rejected alternative**: Strict global ordering across all sessions
  on a peer. Requires a centralized sequence-number allocator; not
  worth the engineering cost.

### D16: Peer registry is workspace-scoped (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D21): The `peers` table
  lives in the **workspace database**, not a global table. Same-ws
  peers may omit `workspace_id` in their registry row; cross-ws peers
  carry an explicit `workspace_id`.
- **Trade-off accepted**: We give up "global peer view" (one
  `SELECT * FROM peers` gives all peers) in exchange for (a) workspace
  isolation enforced at the storage layer, (b) per-workspace backup/restore
  automatically includes peer state.
- **Rejected alternative**: Global peers table with `workspace_id` column.
  Cleaner queries but weakens the storage-layer isolation invariant.

### D17: Redis pub/sub keys use `orch:peer:{orch_id}:topic:{name}` (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D22): All cross-daemon
  pub/sub channels are prefixed `orch:peer:{orch_id}:topic:{topic_name}`
  to prevent topic-name collisions across multi-tenant deployments.
- **Trade-off accepted**: We accept slightly longer key strings in
  exchange for safe multi-tenant operation on a shared Redis instance.
- **Rejected alternative**: Flat `peer.{topic}` keys. Simpler but a
  malicious or careless peer can poison another tenant's topic.

### D18: Handshake timeouts — 10s connect, 30s HELLO, 3× retry (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D23): TCP connect
  timeout 10s, HELLO response timeout 30s, retry 3× with exponential
  backoff (1s, 2s, 4s). On final timeout, the peer is marked
  unreachable and INVOKEs return 503.
- **Trade-off accepted**: Operators may see brief "unreachable" blips
  during transient network issues in exchange for fast failure
  detection (30s upper bound on handshake). Long-haul links (>5s RTT)
  may need to tune the timeout — config knob exposed.
- **Rejected alternative**: No timeout (wait forever). Risk of zombie
  handshakes blocking the event loop.
- **Rejected alternative**: Very short timeout (1s connect, 5s HELLO).
  Too aggressive for cross-region or VPN links.

### D19: Graceful shutdown via GOODBYE frame + 5s drain (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D24): On SIGTERM, the
  daemon sends `GOODBYE` to each connected peer, then drains
  in-flight INVOKEs for up to 5 seconds before hard exit.
- **Trade-off accepted**: Shutdown latency increases by up to 5s in
  exchange for peers receiving a clean "I'm going away" notification
  rather than TCP RST. RST would force peers to detect via timeout,
  delaying the next INVOKE by 10s+ (D18).
- **Rejected alternative**: Hard exit on SIGTERM (no GOODBYE). Fastest
  shutdown but creates thundering-herd reconnect storms on rolling
  restarts.

### D20: Per-peer rate limit — 100 INVOKE/s, burst 200, configurable (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D25): Each peer pair is
  rate-limited to 100 INVOKE/s with burst capacity of 200, enforced by
  a token bucket in `require_peer_auth` middleware. Over-limit calls
  return HTTP 429 with `Retry-After`. Configurable via
  `peer.rate_limit.{rps,burst}`.
- **Trade-off accepted**: A noisy peer can degrade its own INVOKE
  throughput in exchange for protecting B1 from token-bucket
  exhaustion when `peer.auto_schedule=true` (R12 mitigation, complements
  `MAX_CONCURRENT_PEER_TURNS`).
- **Rejected alternative (A)**: No rate limit (rely on auto-schedule
  gate). Insufficient — even without auto-schedule, a misbehaving peer
  can saturate B1's INVOKE handler.
- **Rejected alternative (B)**: Per-daemon-global rate limit (one
  bucket for all peers). Fairer but punishes well-behaved peers when
  one peer misbehaves.

### D21: Decentralized group management (any accepted member invite/kick; no owner) (new v3)

- **What** (new 2026-09-08 v3, maps to DESIGN D26): Group membership
  operations (`invite`, `kick`, `leave`) can be invoked by **any**
  already-accepted member of the group. There is no `owner`, `admin`,
  or `moderator` role. Members are equal peers; trust comes from the
  accept handshake, not from role hierarchy.
- **Trade-off accepted**: We give up the safety property "only an
  admin can kick a member" in exchange for matching the user's
  "去中心化管理" (decentralized management) mental model — consistent
  with P2P / Matrix-federation semantics where any member can invite
  or leave. The risk that "any member can kick anyone" is mitigated
  by the trust gate (operators only accept peers they trust).
- **Rejected alternative (A)**: First-member-becomes-owner (only owner
  can invite/kick). More conservative, but introduces owner-tracking
  state and "what happens if owner leaves?" edge cases.
- **Rejected alternative (B)**: Voting / consensus for kick. Distributed
  consensus is heavy machinery for a 3–10 member LAN peer group;
  defers to Phase 2+ if real demand emerges.
- **Rejected alternative (C)**: Any member can invite but only owner can
  kick. Half-measure that requires the owner-tracking state we want to
  avoid.

## Consequences

### Positive

- All ten Phase 1 goals (G1-G10) are reachable with ≤1950 lines of net
  new code (v3 estimate; ~+210 over v2 due to msg_id/dedup, cross-ws
  registry, /sessions endpoint, rate limit, shutdown hook).
- Custom `peer/1` borrows A2A field naming (§14.2 of DESIGN) — future
  migration to A2A-compatible profile is possible if ecosystem pulls
  in that direction (D1 revised).
- Redis pub/sub (Phase 1, D7) gives A1's local `/ws` subscribers
  real-time visibility into B1's events — strongest UX win of v2.
- "Accept to join" gate matches chat-group mental model (WeChat,
  Telegram) — zero onboarding surprise.
- `ORCHESTRATORD_PEER_TRUST` white-list lets trusted pairs skip the
  manual accept step without weakening security for new connections.
- Auto-group when ≥2 peers (R10 mitigated by explicit registry)
  matches "加群" intuition without manual group bookkeeping.
- **Single daemon can simultaneously belong to N groups (D12)** —
  matches user mental model; per-agent topics still bridge across
  groups.
- **Decentralized group management (D21)** — no owner/admin state to
  track; aligns with P2P/Matrix-federation semantics.
- **Manual token rotation only (D11)** — keeps AuthToken schema
  simple; SOP documented in README; no rotation state machine.
- **Cross-workspace federation allowed (D10)** — operators not boxed
  in by workspace boundary; explicit invite friction on cross-ws.
- **Cross-daemon session creation (D13)** — A1's agents can spawn
  fresh sessions on B1; audit row carries `invited_by_*` for
  end-to-end traceability.
- **at-least-once delivery with msg_id dedup (D14)** — protects
  against HTTPS jitter + HMAC-nonce-reuse bugs without two-phase
  commit machinery.
- **Per-session monotonic ordering (D15)** — natural fit for chat /
  agent flows; cross-session ordering is rarely meaningful.
- **Workspace-scoped peer registry (D16)** — per-workspace
  backup/restore automatically includes peer state; storage-layer
  isolation invariant preserved.
- **Redis key prefix (D17)** — multi-tenant safe on shared Redis.
- **Handshake timeouts (D18)** — fast failure detection (≤30s upper
  bound) with configurable knobs for long-haul links.
- **Graceful shutdown via GOODBYE frame (D19)** — clean peer
  notification; no RST storms on rolling restart.
- **Per-peer rate limit (D20)** — protects B1 from misbehaving peers
  with token-bucket fairness.
- CLI peer subcommands (`orchestratord peer {list,invite,...}`) make
  peer operations scriptable for ops teams.
- No changes to mechanism-core code paths; the existing kernel/business
  decoupling work is unaffected.

### Negative / Deferred

- Phase 1 Redis deployment dependency (R9). Operators must
  self-host a single Redis instance; docker-compose snippet provided
  in README.
- A leaked peer token has workspace-wide impact; the only mitigation
  is per-scope token issuance (R6).
- Cross-daemon audit log joining requires `peer_call_id` +
  `invited_by_*` fields on both sides (D13) — implemented in Phase 1
  but OpenTelemetry correlation is Phase 2.
- Auto-schedule on B1 incurs real token cost; operator must
  understand the gate's implications (R1, AC11).
- Browser-to-remote-direct path weakens the audit chain (R5).
- **Token rotation is manual (D11)** — operators must follow rotate
  SOP (R15); window of inconsistency if not done correctly.
- **`msg_id` dedup window (30s, D14)** loses state on daemon restart
  — duplicate INVOKEs arriving within the restart window may
  re-execute (R13).
- **Per-session ordering is best-effort (D15)** — out-of-order frames
  still accepted (audit marks `out_of_order=true`); strict FIFO would
  require per-session server-side sequencing (R14).
- **Cross-workspace federation (D10)** adds configuration complexity
  (`workspace_id` field, explicit invite friction) — operators may
  not realize this knob exists.
- **Cross-daemon session creation (D13)** is a high-privilege
  endpoint — requires `peer.invoke` scope plus the existing peer
  accept handshake.

### Deferred / Out of Scope (matches DESIGN §8.2 v3)

- **Phase 2** (revised): Redis Sentinel / Cluster HA + pub/sub
  performance optimization; OpenTelemetry correlation; optional JWT
  tokens (D11 alt-B) with built-in expiry; Prometheus metrics
  exposure; backup/restore integration.
- **Phase 3**: Per-peer token quota / monthly budget (refinement of
  D20); mDNS/Bonjour first-time bootstrap UX; per-peer failover
  configuration.
- **Phase 4**: Optional ANP/libp2p transport backend (resolves NG1);
  DID signatures replace HMAC.

## Verification

The following concrete outputs prove the ADR has been honoured at
implementation time (matches DESIGN §8.1 v3):

1. **AC13** (`DESIGN §8.1`): `git diff --stat master...phase-1` must
   show zero changes under `src/orchestratord/orchestrator.py`,
   `workflow_orchestrator.py`, `modes/`, `kernel/`, `agent/`. The only
   allowed exception is `api/realtime.py` (which must extract the
   `RealtimeBackend` abstraction but must NOT change the publish
   contract).
2. **AC4**: `curl http://<peer-host>:9001/.well-known/agent.json` returns
   JSON with the A2A-style fields documented in `DESIGN §14.2` and
   `protocol_version="peer/1"`; `orch_id` persists across daemon
   restarts at `~/.orchestratord/data/orch_id`.
3. **AC5**: A two-daemon end-to-end smoke test prints a transcript
   matching the §5 handshake sequence.
4. **AC6 / AC7**: Integration test `tests/peer_integration/test_two_daemon.py`
   (468 lines, **v4 实施期路径** — 原 §8 spec 写 `tests/integration/test_peer_federation.py`)
   spins up two `orchestratord serve` instances + a Redis container
   and verifies (a) cross-daemon message lands in B1's `messages` table
   with `peer_call_id` set, (b) A1's local `/ws` subscriber receives
   a `peer.agent.{id}.events` payload emitted on B1 via Redis pub/sub.
5. **AC9** (auto-group): Three-daemon test (A1+A2+B1) verifies that
   once ≥2 peers mutually register, a `group_id` is assigned and
   members can be enumerated via `orchestratord peer group list`.
6. **AC10** (CLI): `tests/cli/test_peer_cli.py` verifies all
   `orchestratord peer {list,invite,accept,reject,remove,leave,group}`
   subcommands have exit code 0 / correct stdout under expected
   fixtures.
7. **AC11** (auto-schedule): Test toggles `peer.auto_schedule=true`
   on B1, sends peer message from A1, asserts B1's `BackendRunner.run`
   is invoked within 1 second; with `auto_schedule=false`, asserts
   the message is persisted but no run is triggered.
8. **AC12** (isolation): `pytest` in `tests/` (excluding the new
   `peer/` tests) is green; the 21 existing routers work without
   modification.

**v3 additional ACs (AC16-AC27) for D10-D21**:

9. **AC16** (D10 workspace boundary): Integration test with two
   workspaces (`ws-alpha`, `ws-beta`) verifies that same-ws peers
   auto-discover via `peers.json` (no `workspace_id` required),
   while cross-ws peers must carry explicit `workspace_id` in their
   registry row and go through the full invite → accept handshake.
10. **AC17** (D11 manual token rotation): README has a "Token Rotation
    SOP" section describing the manual rotate procedure (R15); no
    code path implements automatic rotation.
11. **AC18** (D12 multi-group): Three-daemon test (A1+A2+B1) where A1
    is registered in two separate groups (one with A2, one with B1)
    verifies that `orchestratord peer group list` returns both groups
    for A1, and per-agent topics (`peer.agent.{id}.events`) bridge
    correctly across both groups.
12. **AC19** (D13 cross-daemon session): A1 calls
    `POST /api/peer/peers/{orch-B1}/sessions`; B1 creates a new
    session; the `audit_log` row carries both `invited_by_orch_id`
    and `invited_by_peer_call_id` set to A1's values.
13. **AC20** (D14 msg_id dedup): A1 sends two INVOKEs with the same
    `msg_id` within 30 seconds; B1 executes the first, returns the
    cached RESULT for the second without re-executing the underlying
    operation. Restarting B1 mid-window causes the second INVOKE to
    re-execute (R13 documented).
14. **AC21** (D15 ordering): A1 sends INVOKEs with `ordering=1, 5, 3`
    on the same session; B1 processes them but logs WARN + sets
    `out_of_order=true` on the audit row for `ordering=3`.
15. **AC22** (D16 workspace-scoped registry): `peers` table is created
    in the workspace database, not a global table; per-workspace
    backup includes peer state.
16. **AC23** (D17 Redis key prefix): `redis-cli MONITOR` during
    cross-daemon event flow shows keys prefixed `orch:peer:{orch_id}:...`.
17. **AC24** (D18 handshake timeout): Point client at an unreachable
    peer; handshake fails within 40 seconds (10s connect × 1 + 30s
    HELLO × 1, no retries at handshake layer; retry layer is separate);
    subsequent INVOKEs return 503.
18. **AC25** (D19 GOODBYE): Send SIGTERM to B1 while A1 has open
    connections; A1 receives `GOODBYE` frame, drains in-flight
    requests, and marks B1 as "graceful-shutdown" (not "unreachable").
19. **AC26** (D20 rate limit): From a single peer, send 250 INVOKEs
    within 1 second; first 200 succeed (burst), 51st onward return
    HTTP 429 with `Retry-After` header.
20. **AC27** (D21 decentralized): In a 3-peer group, member C (not
    the first/oldest) successfully invites a 4th peer; the group
    accepts the invite and adds the new member. No "owner" field
    exists anywhere in the `peers` table schema.

### Phase 1 implementation status (2026-09-08)

All six PRs of DESIGN §10 Phase 1 are implemented and independently
verified:

* **PR1** `peer/protocol.py` — peer/1 frames (HELLO/WELCOME/INVOKE/
  RESULT/EVENT/GOODBYE), `msg_id` field, HMAC signing (`hmac_sig.py`)
  + nonce replay guard (`nonce_store.py`).
* **PR2** `peer/card.py` + `peer/registry.py` + public
  `GET /.well-known/agent.json` (workspace-scoped registry rows).
* **PR3** `RealtimeBackend` seam with `LocalBackend` (byte-for-byte
  historical fan-out, AC13) and `RedisBackend` (D22 channels +
  `PeerEventRelay` inbound, AC7), selected via
  `ORCHESTRATORD_REDIS_URL`.
* **PR4** `PeerClient` (D23 retry/timeouts) + handshake + `group.py`
  (D26) + `serve --peer-listen`.
* **PR5** peer API surface: invite/accept/reject/list/remove/invoke/
  cross-daemon sessions/SSE events + `require_peer_auth` (D25 rate
  limit, D14 workspace check) + `PeerMessageDispatcher` (D18 dedup,
  D19 ordering, NG4 auto-schedule, R12 ceiling) + operator CLI
  (`orchestratord peer …`).
* **PR6** D24 shutdown drain (`serve` GOODBYE on SIGTERM/SIGINT),
  §6.1d auto-schedule wake into the chat dispatcher's claim loop,
  §6.2 capability-gated forward hook, cross-process integration test
  (two real `orchestratord serve` daemons + shared Redis: remote
  INVOKE with D18/D19 + audit, cross-daemon session auto-scheduled to
  a terminal status by the peer's real claim loop, SSE delivery via
  the D22 relay), and the README quickstart + token-rotation SOP
  (AC14/AC17).

**Phase-1 boundary (deliberate)**: no network `FrameTransport` exists
yet, so `serve` does not bind a peer/1 frame listener; cross-daemon
traffic uses the HTTP REST surface with the per-peer bearer token +
`X-Peer-Orchestrator-Id` header. `PeerClient` remains
transport-injected so Phase B can attach the frame binding without
protocol changes.

The integration test caught and fixed a real handshake defect (the
R10 trust branch issued a token without linking `peer.token_id`,
so every pre-trusted peer was rejected as "not accepted").

## Alternatives Considered (Rejected)

| Alternative | Why Rejected |
|---|---|
| libp2p from day one | Overkill for LAN target; ecosystem immature in 2026 |
| Matrix federation (AgentTeams-style) | Requires new homeserver deployment; out of scope |
| Adopt AITP / NEAR protocol | Blockchain-coupling; orchestrators don't need it |
| MCP-only interop | MCP is a tool/resource protocol, not a peer discovery + RPC protocol |
| Direct database replication | Cross-DB conflict resolution is unsolved; HTTPS RPC is cleaner |
| Auto-accept via mDNS | Security regression; allow stranger nodes to silently join |
| Defer RealtimeBackend + Redis to Phase 2 (v1 design) | User rejected — R8 (A1 WS subscribers can't see B1 events) is core UX requirement |
| Code-level browser restriction (v1 NG5) | User rejected — browser can naturally reach remote if CORS configured |
| Auto-schedule always-on | Token-cost + DoS risk; needs gate |
| Auto-schedule deferred to Phase 3 (v1 design) | User rejected — gate is the safety mechanism; once gate exists, Phase 1 is sufficient |
| Workspace-scoped only (cross-ws banned) (D10 alt-A) | Clunky for operators needing genuine cross-workspace federation |
| No workspace boundary at all (D10 alt-B) | Security regression; cannot restrict cross-ws data flow via config |
| Automatic token rotation with grace period (D11 alt-A) | Adds rotation state machine + silent breakage risk; manual keeps AuthToken schema simple |
| JWT-style tokens with built-in expiry (D11 alt-B) | Breaks existing AuthToken.scopes shape; migration cost high, benefit unvalidated |
| Per-agent group membership (D12 alt) | Mismatches user "orchestrator 加群" mental model |
| A1 can only message existing B1 sessions (D13 alt-A) | Insufficient — agents need fresh sessions for new conversations |
| At-most-once delivery (D14 alt-A) | Loses messages on network jitter; user wants reliable |
| Exactly-once delivery (D14 alt-B) | Requires two-phase commit; not implementable |
| Strict global ordering across sessions (D15 alt) | Requires centralized sequence allocator; cost > benefit |
| Global peers table with workspace_id column (D16 alt) | Weakens storage-layer isolation invariant |
| Flat `peer.{topic}` Redis keys (D17 alt) | Multi-tenant poisoning risk |
| No handshake timeout (D18 alt) | Zombie handshakes block event loop |
| 1s connect / 5s HELLO timeout (D18 alt) | Too aggressive for VPN/cross-region |
| Hard exit on SIGTERM (D19 alt) | RST storms on rolling restart |
| No rate limit (D20 alt-A) | Insufficient — even without auto-schedule, misbehaving peer saturates B1 |
| Per-daemon-global rate limit (D20 alt-B) | Punishes well-behaved peers when one misbehaves |
| First-member-becomes-owner (D21 alt-A) | Adds owner-tracking state + edge cases on owner leave |
| Voting / consensus for kick (D21 alt-B) | Heavy machinery for 3-10 member LAN groups |
| Any-can-invite only-owner-can-kick (D21 alt-C) | Half-measure that still requires owner state |

---

**Reviewers**: tbd
- **Required ACs to merge**: AC1-AC15 (DESIGN §8.1) + AC16-AC27 (ADR-001 §Verification)
- **Companion files**: `DESIGN_PEER_FEDERATION.md` (v3)