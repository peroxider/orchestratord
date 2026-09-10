# DESIGN_PEER_FEDERATION

> **状态**: 临时设计稿，待评审（详见 `feedback_temporary_docs_at_root.md`）。
> **作者**: Claude（基于 2026-09-08 用户 goal 分析）
> **范围**: orchestratord daemon ↔ orchestratord daemon 的互联能力（"加群"语义）

---

## §0. 设计动机（用户原始诉求）

> 我想让不同（守护）进程的编排器之间也可以交互。例如 A1（本地）可以连接到 B1（内网另一机器）或 A2（本地另一实例），连接类似"加群"：多编排器去中心化加入一个群；B1 允许后 A1 可调用 B1、可看到 B1 的状态（含当前任务会话消息、历史记录）；A1 的任何 Agent 可与 B1 的任何 Agent 互通消息、相互查看对方状态。

## §1. 现状盘点（2026-09-08 审计）

orchestratord 当前是**单 daemon、单进程**模型，跨进程边界只有两条路径：

| 通道 | 现状 | 跨机器能力 |
|---|---|---|
| `ipc/gateway/1`（UDS + JSONL，`src/orchestratord/ipc/{protocol,client,models}.py`） | 与本地 IM gateway 通信（飞书/Lark/WeCom）；`GatewayIpcClient` 是唯一客户端 | ✗ 仅本机 UDS |
| `orchestratord serve`（FastAPI @ 127.0.0.1:9000，21 个 domain router + `/ws` 实时层） | Web console 主入口 | ✗ 默认 127.0.0.1；README 明确要求外露必须 VPN/反向代理 |
| `chat_gateway.py`（UDS → SSE/HTTP） | per-run 控制 socket 桥 | ✗ 仅本机 |
| `control_socket.py`（per-run UDS） | pause/resume/inject/stop/takeover | ✗ 仅本机 |
| `bridge/worker.py`（stdio JSON-RPC） | agent worker 子进程管理 | ✗ 仅子进程 |
| `modes/{single,pipeline,coordinator,swarm}` | 多 agent 协作 | ✗ 全部在同一 daemon 进程内 |
| `domain/mention.py`（@mention 解析） | 仅做 in-process dispatch | — |

**关键观察（已用代码佐证）**：

1. `RealtimeBroker`（`src/orchestratord/api/realtime.py:35-142`）是**具体类**（不是抽象 backend），注释明示 "Single-instance only — multica's `server/internal/realtime` adds a Redis pub/sub relay for fan-out across server instances. We mirror that boundary here so swapping in Redis later only changes the backend, not the consumer API."——这是给多实例 fan-out 留的扩展点。
2. `sessions.py` 的 `_forward_approval`（`src/orchestratord/api/routers/sessions.py:155-181`）与 `_forward_interrupt`（同文件 :184-216）已经预留"Phase B cross-process bridge"语义：
   > "Missing live session → silent no-op (**Phase B cross-process bridge** will route)."
3. "Phase B" 一词**已被占用**——指 chat + Slack/Lark 通讯层（见 `docs/FEATURE_GAP_VS_MULTICA.md:41, 832`）。本设计命名 "Peer 层" 以避免撞车。
4. `AuthToken` + `auth_tokens` 表 + SHA-256 哈希（`src/orchestratord/domain/auth_token.py:28-30`，`src/orchestratord/db/models/audit_auth.py:29-39`）是干净的扩展点；现有 `scopes` 字段可直接携带 `peer.read / peer.invoke / peer.approve`。
5. `orchestratord serve` 默认绑定 `127.0.0.1:9000`（`src/orchestratord/cli/serve.py:38-40`）；Peer 端口必须**新增 flag** `--peer-listen` 单独控制对外接口，不污染本地控制面。
6. `peer` 关键字在源码中**只出现 1 次**（`src/orchestratord/chat_gateway.py:5`，语义是"对端本地客户端"），命名干净可占用。

## §2. Goals / Non-Goals（2026-09-08 v3 修订）

### Goals（Phase 1）

- **G1**：在 LAN / VPN 内，A1 可通过 HTTPS 调用 B1 的现有 API，无需在 B1 上额外暴露内部端口。
- **G2**：A1 加入 B1 群必须经 B1 的 operator **显式接受**（信任 gate）；可被 `ORCHESTRATORD_PEER_TRUST` 白名单绕过。
- **G3**：A1 可通过 Redis pub/sub + HTTPS SSE 订阅 B1 的 broker topic，**B1 的 agent event 可重放到 A1 的本地 `/ws` 订阅者**（per-agent topic，如 `peer.agent.{id}.events`，D22）。
- **G4**：远端调用在 B1 上落地为 DB 行 + audit log 记录（与现有 `sessions.py` 写入路径一致），并写入 `peer_call_id` 串联字段（AC12）。
- **G5**：Agent Card 借鉴 A2A 字段命名（`name`/`description`/`url`/`version`/`provider`/`capabilities[]`/`defaultInputModes`/`skills[].id+description`，详见 §14.2 与 D20），但 `protocol_version="peer/1"`，**不暴露 A2A 兼容标识**。
- **G6**：≥2 个 peer 互相连接后**自动成群**；**单 daemon 可同时属于 N 个群**（D16）；群成员变化通过 Redis pub/sub 广播。
- **G7**：新增 CLI 群操作子命令：`orchestratord peer {list,invite,accept,reject,remove,leave,group}`。
- **G8**：远端消息在 B1 用户允许的前提下可**自动调度**新 turn（受 `peer.auto_schedule=true/false` 控制；默认 `false`，与原 NG4 行为一致，但开启后即生效，无需 Phase 3）。
- **G9**（**v3 新增**）：A1 可通过 `POST /api/peer/peers/{orch-id}/sessions` 在 B1 上**创建新 session**（D17）；audit log 带 `invited_by={orch_id, peer_call_id}`。
- **G10**（**v3 新增**）：消息投递采用 **at-least-once + 幂等** 语义（D18）：每帧带 `msg_id`；B1 端按 `(msg_id, orch_id)` 30s 窗口 dedup。顺序保证 **per-session FIFO**（D19）；跨 session 无序。

### Non-Goals（Phase 1 明确不做）

- **NG1**：libp2p / DHT / NAT 穿透 / 端到端加密 —— 纯 HTTPS + HMAC。
- **NG2**（**已废止**）：原 "RealtimeBroker 抽象 Phase 2 才做" —— 现已 **提前到 Phase 1**（G3）。
- **NG3**：远端 agent runtime takeover —— 只能远端发消息 / 审批 / 读状态，不能接管 agent 控制流。
- **NG4**（**已废止**）：原 "远端消息自动调度 Phase 3" —— 现已 **提前到 Phase 1**（G8，受控）。
- **NG5**（**已废止**）：原 "浏览器只能连自己 daemon" —— 现**不强制**；网络互通时浏览器可直连对端 daemon，仅在 UI 默认走本地（操作性约定，非代码强制）。
- **NG6**：Redis Sentinel / Cluster 高可用 —— 仅单实例 Redis（operator 自托管）；高可用是运维议题。
- **NG7**：多租户 / RBAC —— workspace 级别 ACL 已是上限；peer 层不引入新角色。
- **NG8**（**v3 新增**）：Token 自动轮换 + grace period —— Phase 1 仅手工 rotate（D15）；自动轮转是 Phase 2+。
- **NG9**（**v3 新增**）：群组管理员 / owner 角色 —— 按 D26 决策走**去中心化**管理（任何已 accept 成员都能 invite/kick），不引入 owner/admin/concept；投票 / 共识机制不进 Phase 1。

## §3. 开源参照

| 项目 | 模式 | 关键技术 | 与本设计契合点 |
|---|---|---|---|
| **A2A Protocol**（Google, 2025-04） | Client-Server，能力发现 | JSON-RPC / SSE / gRPC；`/.well-known/agent.json` Agent Card；OpenAPI 风格鉴权 | ★★★★★ 直接对齐 |
| **MCP**（Anthropic） | Server 提供 tools/resources；远程 stdio/HTTP/SSE | JSON-RPC over transport | ★★★★ 可作子层 |
| **AgentTeams**（agentscope-ai, 5.4k★） | Manager + Worker + Matrix rooms | **Matrix 联邦协议** + MinIO 共享 FS + K8s | ★★★★ 范式参考：聊天即协作载体 |
| **ANP**（Agent Network Protocol, 2025-2026） | 去中心化 P2P + DID | `.well-known/agent-descriptions`、DID 签名、端到端加密 | ★★★★ 远期强去中心化形态 |
| **Synaptic Mesh / SwarmMind-AI** | 真正去中心 | libp2p + Kademlia DHT + Gossipsub | ★★★ 适合内网 P2P |
| **AgentGateway** | A2A + MCP 代理 + K8s Gateway API | K8s 原生部署 | ★★★ 部署形态参考 |
| **OpenAI Swarm** | 进程内 handoff | 仅单进程 | ✗ 不可用 |
| **AutoGen / CrewAI / LangGraph** | 进程内多 agent | 不可用 | ✗ |

**决策**：Phase 1 采用 **A2A 兼容** 的 Agent Card + HTTPS + SSE；远期需要去中心化时叠加 ANP/libp2p 作为 transport 后端。

## §4. 协议 `peer/1`

```
PROTOCOL_VERSION = "peer/1"
TRANSPORT = HTTPS + JSONL
```

### §4.1 帧类型

```
HELLO          # 首次握手：宣告 orch-id + capability + 签名
WELCOME        # 应答：接受 / 拒绝
INVOKE         # 转发 HTTP 调用（JSON-RPC request 风格；含 msg_id + ordering）
RESULT         # 应答（含 status / body；echo msg_id）
EVENT          # 远端订阅推送（来自 realtime broker）
SUBSCRIBE      # 订阅远端 topic
UNSUBSCRIBE
PING / PONG
REVOKE         # 撤销某个 peer 权限
GOODBYE        # v3 新增（D24）：优雅关闭通知；携带 in-flight count 便于对端 drain
```

### §4.2 Frame 字段（最小集）

| 字段 | 类型 | 说明 |
|---|---|---|
| `type` | enum | §4.1 帧类型 |
| `frame_id` | str(uuid4) | 帧唯一 ID（防重放） |
| `protocol_version` | str | `"peer/1"` |
| `orch_id` | str | 发送方 orchestrator ID（持久，每个实例一个） |
| `peer_token` | str | Bearer token（SHA-256 hash 的原值，仅对端持有） |
| `request_id` | str? | INVOKE ↔ RESULT 匹配键 |
| `msg_id` | str(uuid4)? | **v3 新增（D18）**：INVOKE/RESULT 必须携带；B1 端按 `(msg_id, orch_id)` 30s 窗口 dedup |
| `ordering` | str? | **v3 新增（D19）**：per-session 单调递增序号；空表示无序 |
| `method` | str? | 如 `"POST /api/sessions/{id}/messages"` |
| `headers` | dict? | 转发的 HTTP 头（剔除 host/auth/cookie） |
| `body` | dict? | JSON-decoded body |
| `status` | int? | RESULT 中的 HTTP status |
| `topic` | str? | EVENT 中的 realtime topic |
| `payload` | dict? | EVENT 的 payload |
| `capabilities` | list[str]? | HELLO 携带的能力声明 |
| `timestamp` | float | 时间戳（用于 ±60s 窗口校验） |
| `nonce` | str? | 一次性随机串（HMAC replay guard） |
| `signature` | str? | HMAC-SHA256(orch_id + frame_id + body, peer_token) |

### §4.3 Agent Card（`GET /.well-known/agent.json`）

```json
{
  "name": "orchestratord-A1",
  "description": "orchestratord daemon instance (Alice's laptop)",
  "version": "0.1.0",
  "url": "https://a1.example.lan:9001",
  "orch_id": "orch-A1-2026-09-08-9f3c1a",
  "protocol_version": "peer/1",
  "preferred_transport": "https+sse",
  "capabilities": [
    "peer.invoke", "peer.subscribe",
    "sessions.read", "sessions.message.post", "sessions.approve",
    "agents.list", "agents.message",
    "inbox.read", "realtime.subscribe"
  ],
  "security_schemes": {
    "bearer": {
      "type": "http", "scheme": "bearer",
      "description": "Per-peer token; SHA-256 hash stored in auth_tokens.scopes=['peer.*']"
    },
    "hmac": {
      "type": "mutual-tls",
      "description": "HMAC-SHA256 over (orch_id, frame_id, body, ts); ±60s window; nonce replay guard"
    }
  },
  "skills": [
    {
      "id": "sessions.message.post",
      "description": "Append a message to a session timeline (DB-backed)",
      "input_schema": {"session_id": "uuid", "role": "user|assistant", "content": "string"}
    },
    {
      "id": "realtime.subscribe",
      "description": "SSE stream of broker topic frames",
      "input_schema": {"topics": ["string"]}
    }
  ],
  "provider": {"organization": "self-hosted", "contact": "operator@example.com"}
}
```

## §5. 首次握手（A1 想加入 B1 的群）

```
A1                                          B1
 │                                            │
 │  GET /.well-known/agent.json               │
 │ ─────────────────────────────────────────▶ │
 │ ◀───────────────────────────────────────── │  200 + B1 Agent Card
 │                                            │
 │  POST /api/peer/invite (含 A1 Agent Card +  │
 │                       nonce + 签名)        │
 │ ─────────────────────────────────────────▶ │  202 Accepted
 │                                            │  → 在 B1 operator inbox 投 "INVITE_REQUEST"
 │  ... 人工批准 ...                          │
 │                                            │
 │  POST /api/peer/invite/{token_id}/accept   │
 │ ◀───────────────────────────────────────── │
 │                                            │
 │  HELLO (orch_id=A1, capabilities, 签名)    │
 │ ─────────────────────────────────────────▶ │
 │ ◀─────────── WELCOME ──────────────────── │  含 A1↔B1 双向 subscribe 句柄
 │                                            │
 │  INVOKE GET /api/workspaces/{ws}/sessions  │
 │ ─────────────────────────────────────────▶ │
 │ ◀───── RESULT (200, [...sessions]) ────── │
 │                                            │
 │  SUBSCRIBE topic=session.*                 │
 │ ─────────────────────────────────────────▶ │
 │ ◀──── EVENT topic=session.{x} payload=... │
```

关键点：
- **握手是同步的，但 accept 是异步的**——B1 人类 operator 必须显式接受；A1 默认永久 pending。
- A2A 的 Agent Card 只服务"发现"，真正的 trust gate 在 `/api/peer/invite` 路由。
- HMAC + nonce 防重放（防中间人 replay）。
- 接受方白名单可通过 `ORCHESTRATORD_PEER_TRUST=orch-A,orch-B` 跳过人工。
- **v3 新增（D23）超时**：TCP connect 10s + HELLO 响应 30s + retry 3× 指数退避；超时即拒绝，不留 half-state。
- **v3 新增（D14）Workspace 边界**：同 workspace 的 daemon 在 `peers.json` 中可省略 workspace 字段（**天然允许**）；跨 workspace 必须显式 `invite` + 双方 accept。

## §6. 改动清单

### §6.1 新增文件（peer 层 + CLI；2026-09-08 v3 修订）

| 文件 | 估计行数 | 用途 |
|---|---|---|
| `src/orchestratord/peer/__init__.py` | 10 | 包导出 |
| `src/orchestratord/peer/protocol.py` | 140 | `PeerFrame`, `PeerFrameType`, 编解码；**v3** 含 `msg_id`/`ordering`/GOODBYE 帧（D18/D19/D24） |
| `src/orchestratord/peer/card.py` | 90 | 本地 Agent Card 构建；**v3** 按 §14.2 落地 A2A 字段借鉴清单（D20） |
| `src/orchestratord/peer/registry.py` | 140 | **v3**：peer 表 **workspace-scoped**（D21），存于 workspace DB 而非全局 `peers.json`；含 `workspace_id` 字段以支持 D14 同 ws / 跨 ws 区分 |
| `src/orchestratord/peer/group.py` | 130 | **v3**：≥2 自动成群 + 单 daemon 可加 N 群（D16）+ **去中心化**管理（D26，无 owner 角色） + Redis pub/sub 广播成员变化 |
| `src/orchestratord/peer/client.py` | 220 | HTTPX 异步客户端 + SSE 长连接管理；**v3** 集成 D23 超时（10s/30s/3×） |
| `src/orchestratord/peer/handshake.py` | 160 | HELLO/WELCOME/SUBSCRIBE 状态机；**v3** D23 超时落地 |
| `src/orchestratord/peer/hmac_sig.py` | 60 | HMAC 签名/校验 + 防重放 nonce 表（**v3** Phase 1 仅手工 rotate，D15） |
| `src/orchestratord/peer/nonce_store.py` | 80 | nonce 持久化（单进程 SQLite；Redis SETNX 用于多实例） |
| `src/orchestratord/peer/redis_relay.py` | 200 | Redis pub/sub 订阅 + 远端事件落地到本地 broker；**v3** key 命名 `orch:peer:{orch_id}:topic:{topic_name}`（D22，多租户安全） |
| `src/orchestratord/peer/agent_topics.py` | 60 | per-agent topic 路由（`peer.agent.{id}.events`） |
| `src/orchestratord/peer/dispatcher.py` | 170 | 远端消息自动调度（受 `peer.auto_schedule` 控制，并发限制 R12）；**v3** 加 `(msg_id, orch_id)` 30s dedup（D18） + per-session `ordering` 校验（D19） |
| `src/orchestratord/peer/connections.py` | 80 | **v3 实施期新增（spec 未列）**：live outbound peer-connection registry；D24 shutdown drain 句柄（5s drain + 发 GOODBYE）；§6.2 Phase-B bridge hook 句柄；process-local 设计，无持久化 |
| `src/orchestratord/db/models/peer.py` | 50 | **v3 实施期新增（D16 隐含产物）**：`peers` 表 ORM model；`workspace_id` + `orch_id` 联合唯一约束 `uq_peers_workspace_orch`（防并发邀请竞态）；`remote_workspace_id` 字段支持 D14 跨 ws 区分 |
| `alembic/versions/0046_create_peers_table.py` | 50 | **v3 实施期新增**：`peers` 表 DDL migration |
| `alembic/versions/0047_add_peer_audit_fields.py` | 35 | **v3 实施期新增**：`audit_log.invited_by_orch_id` + `audit_log.invited_by_peer_call_id` 字段 migration |
| `src/orchestratord/api/routers/peer.py` | 290 | **v3** 增加 `POST /api/peer/peers/{orch-id}/sessions`（D17，跨 daemon 创建 session）+ audit 写入 `invited_by={orch_id, peer_call_id}`；7+1=8 个 API 端点（含 auto-schedule 开关） |
| `src/orchestratord/cli/peer.py` | 200 | CLI 子命令 `peer {list,invite,accept,reject,remove,leave,group}`；**v3** D26 去中心化 —— `add/remove` 仅校验"调用者是成员"，不查 owner |

**新增合计 ~2165 行 + 700 行测试**（v3 实施期比 v3 spec 多约 215 行，主要来自 connections.py + db/models/peer.py + alembic 0046/0047；测试多 250 行，因 peer_integration/test_two_daemon.py + db_integration/test_peer_registry.py 补充）。

### §6.2 修改文件

| 文件 | 修改点 | 性质 |
|---|---|---|
| `src/orchestratord/api/realtime.py:35-142` | **必须改**：抽出 `RealtimeBackend` Protocol；`LocalBackend` 保留当前行为；新增 `RedisBackend`；`RealtimeBroker` 接受 backend 注入；**不修改 publish 行为**（AC13） | +80 行 |
| `src/orchestratord/api/app.py:127-148` | 注册 `peer.router`（`include_router(peer.router, dependencies=[Depends(require_peer_auth)])`） | +10 行 |
| `src/orchestratord/cli/serve.py` | 新增 `--peer-listen HOST:PORT`、`--redis-url URL` flag；默认 `127.0.0.1:9001`、`redis://localhost:6379/0` | +30 行 |
| `src/orchestratord/api/deps.py` | 新增 `require_peer_auth` 依赖：从 `X-Peer-Orchestrator-Id` + `Authorization: Bearer` 校验，scopes 必须含 `peer.*`；**v3** 在其后挂 D25 速率限制（100 INVOKE/s/peer，burst 200，可配置） | +60 行 |
| `src/orchestratord/domain/mention.py:14` | `parse_mentions` 支持 `@agent@orch-id` 形式，返回 `(handle, orch_id)` 元组 | +15 行 |
| `src/orchestratord/api/routers/sessions.py:165, 191` | `_forward_approval/_forward_interrupt` 的 silent no-op 分支：若 session workspace 有 peer 可达，调用 `peer.client.invoke_remote(...)`（**Phase B bridge 真正落地**）；**v3** INVOKE 必带 `msg_id`+`ordering`（D18/D19） | +25 行 |
| `src/orchestratord/db/models/audit_auth.py` | **v3 新增（D17）**：`audit_log` 表加 `invited_by_orch_id` 与 `invited_by_peer_call_id` 字段 | +15 行 |
| `src/orchestratord/seed.py` | `--enable-peer` 时，额外 seed 一个 peer daemon runtime token | +10 行 |
| `src/orchestratord/config/schema.py` | 新增 `PeerConfig` (auto_schedule, max_concurrent_peer_turns, redis_url, trust list)；**v3** 加 `timeouts.{connect,hello}`（D23）、`rate_limit.{rps,burst}`（D25）、`dedup_window_seconds=30`（D18） | +50 行 |
| `src/orchestratord/main.py`（或 daemon 入口）| **v3 新增（D24）**：注册 shutdown hook —— 收到 SIGTERM 后向所有已连接 peer 发 `GOODBYE` 帧 + 5s drain in-flight 请求 | +15 行 |

**修改合计 ~310 行 + 200 行测试**（v3 比 v2 多约 80 行，主要来自速率限制 + 审计字段 + shutdown hook）。

### §6.3 不改的文件（明确边界；2026-09-08 v3 修订）

- ❌ `src/orchestratord/orchestrator.py`
- ❌ `src/orchestratord/workflow_orchestrator.py`
- ❌ `src/orchestratord/modes/*`
- ❌ `src/orchestratord/kernel/*`
- ❌ `src/orchestratord/agent/*`
- ✅ **`v3 例外`** `src/orchestratord/api/realtime.py`（D7/D22：Phase 1 即抽 `RealtimeBackend` 抽象 + `RedisBackend`；**不修改 publish 行为**，仅注入 backend——AC13 例外条款）
- ✅ **`v3 例外`** `src/orchestratord/main.py`（D24：注册 SIGTERM shutdown hook 发送 `GOODBYE` 帧 + 5s drain）
- ✅ **`v3 例外`** `src/orchestratord/db/models/audit_auth.py`（D13：新增 `invited_by_orch_id` / `invited_by_peer_call_id` 字段，不修改既有字段语义）

Peer 层只在 API 边界 + 少量明确的 v3 例外文件活动，机制内核（orchestrator / workflow_orchestrator / modes / kernel / agent）保持纯净。

## §7. 风险表（Phase 1 必须先看到的真实坑；2026-09-08 v3 修订）

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| **R1** | **远程消息自动调度的副作用** | 若 B1 用户开 `peer.auto_schedule=true`，B1 的 agent 会真的消耗 token 跑一轮 turn | **默认 false**；开启时 B1 端需明确 ack；token cost 写入 audit；R5 缓解——operator 看到的是真实资源消耗 |
| **R2** | **HMAC nonce 表持久化与多进程同步** | 重启丢失 → 重放；多进程不同步 → 重放 | 单进程 SQLite 持久化；多实例下用 Redis SETNX（**Phase 1 即接**——见 R9） |
| **R3** | **Accept 必须人工** | UX 不流畅 | `ORCHESTRATORD_PEER_TRUST` 白名单环境变量（D4 决策） |
| **R4** | **远端 SSE 实时性** | HTTPS 长连接跨 NAT 比 WS 多一层风险；断线需补帧 | subscribe 设计 idempotent + cursor-based (`from_seq`)；断线重连自动续传 |
| **R5** | **浏览器直连对端 daemon 的安全/审计** | 跨 daemon 调用不经 A1，audit log 不连续 | UI 默认走本地 daemon；operator 显式配 CORS 才暴露；直连路径仍写 `peer_call_id` |
| **R6** | **Peer token 一旦泄露 = 完整 workspace 权限** | 比本地 daemon token 危险 | scopes 最小化：`peer.read` / `peer.invoke` / `peer.approve` 分别发；接受方最小授权 |
| **R7** | **跨 daemon audit log 拼接** | operator 在 A1 上看到的 agent 调 Y，Y 在 B1 上 | **Phase 1 即做**：两侧都写 audit log，用 `peer_call_id` 串联（D13 决策）；**v3** 新增 `invited_by={orch_id, peer_call_id}`（D17） |
| **R8** | **RealtimeBroker 是单进程**——A1 的 `/ws` 订阅者收不到 B1 推送 | 用户期望"实时看到 B1 状态" | **Phase 1 即解决**（D3 决策）：抽出 `RealtimeBackend` + `RedisBackend`，远端事件经 Redis pub/sub 落地到本地 broker；per-agent topic（`peer.agent.{id}.events`） |
| **R9** | **Redis 是 Phase 1 硬依赖** | Operator 必须自托管 Redis；多一个进程 | 文档明示；提供 docker-compose 片段；单实例 Redis 不需 Sentinel |
| **R10** | **Auto-group 副作用：误连成群** | 配置错 orch-id 容易把无关 daemon 拉进同一群 | group 操作显式确认；**v3** workspace-scoped registry（D21）+ peer 表显式列出"已加入成员"；未列出的即使连通也不成群 |
| **R11** | **per-agent topic 命名冲突** | `agent.{id}.events` 可能与其他 broker topic 冲突 | topic 前缀 `peer.` + Redis key 加 `orch:peer:{orch_id}` 前缀（D22）双重隔离 |
| **R12** | **远端自动调度导致 token 风暴** | B1 多个 peer 同时给 B1 发消息，B1 自动调度 → 多次 turn 并发 | B1 端并发限制（`MAX_CONCURRENT_PEER_TURNS` env var）；**v3** D25 速率限制（100 INVOKE/s/peer，burst 200）兜底；超额时拒绝并回 429 |
| **R13**（**v3 新增**） | **msg_id dedup 窗口 vs 重启丢失** | daemon 重启期间到达的 INVOKE 无法去重 → 重复落库 | dedup 表持久化到 SQLite；窗口 30s（D18）；窗口内的重复 INVOKE 返回上次 RESULT 缓存 |
| **R14**（**v3 新增**） | **per-session ordering 校验失败** | A1 端丢包或乱序 → B1 收到的 `ordering` 不连续 | 仅 WARN 日志 + audit 标记 `out_of_order=true`；不阻断业务（auto-schedule 才需要严格 FIFO，常规消息容忍乱序） |
| **R15**（**v3 新增**） | **手工 rotate token 的窗口期** | A1/B1 各自改 token + 各自重启存在不一致窗口 | 文档给出 rotate SOP：**先改 A1 端 `peers.json`，A1 重启确认 handshake 仍通过；再改 B1 端**（B1 端 rotate 会切断对端直到对方同步） |

## §8. 验收标准（Acceptance Criteria — 必须全勾；2026-09-08 v3 修订）

### §8.1 Phase 1 必达（PR 合并前）

- [ ] **AC1**：新增 peer 包 + cli/peer.py 共 **14 个文件**（v3：增 GOODBYE 帧 + msg_id/dedup + `/sessions` 端点 + 速率限制 + shutdown hook），合计 ≤ 2000 行（含 docstring），无重复实现。
- [ ] **AC2**：`PeerFrame` 单元测试覆盖所有帧类型（含 **v3 新增 GOODBYE**）的 encode/decode、对损坏 JSON 的报错路径。
- [ ] **AC3**：`hmac_sig.sign/verify` 单测：相同输入相同签名；篡改 `frame_id` 即 verify 失败；nonce 重放即 reject。
- [ ] **AC4**：`Agent Card` 在 `GET /.well-known/agent.json` 返回 JSON，借鉴 A2A 字段命名（§14.2 清单），`protocol_version="peer/1"`；orch_id 跨重启稳定（持久化到 `data/orch_id`）。
- [ ] **AC5**：A1 启动 → 通过 invite 协议 → B1 人工 accept → **v3** 双端在 **workspace DB `peers` 表**（D21，非全局 `peers.json`）互相写入对方条目；同 ws 可省略 `workspace_id`、跨 ws 必带。
- [ ] **AC6**：`POST /api/peer/peers/{orch-id}/invoke POST /api/sessions/{sid}/messages` 在 B1 上成功落库 `messages` 表（写 `audit_log.peer_call_id` + **v3 `invited_by_orch_id` + `invited_by_peer_call_id`**）。
- [ ] **AC7**：`RealtimeBroker` 抽出 `RealtimeBackend` 抽象 + `LocalBackend`（默认）+ `RedisBackend`；现有 `/ws` 路由**零行修改**即可通过 Redis pub/sub 收到远端事件（per-agent topic `peer.agent.{id}.events`；**v3 Redis key 加 `orch:peer:{orch_id}` 前缀**，D22）。
- [ ] **AC8**：B1 移除 A1 后，A1 的后续 INVOKE 返回 401/403 且不再收到 EVENT（含 Redis 端 SUBSCRIBE 取消）。
- [ ] **AC9**：≥2 个 peer 互相连接后**自动成群**（R10 缓解），群成员变化通过 Redis pub/sub 广播；**v3** registry workspace-scoped（D21）+ 显式列出"已加入成员"；**单 daemon 可同时属于 N 群**（D16）；**任何已 accept 成员可 invite/kick**（D26）。
- [ ] **AC10**：CLI 子命令 `orchestratord peer {list,invite,accept,reject,remove,leave,group}` 全可用，单测覆盖退出码与 stdout 输出；**v3** D26 `remove` 不查 owner。
- [ ] **AC11**：B1 设 `peer.auto_schedule=true` 时，A1 发的远端消息**自动调度**新 turn（落库 + 启动 `AgentRunner.run`，受 `MAX_CONCURRENT_PEER_TURNS` 限制，R12 缓解）；默认 false 时仅落库。
- [ ] **AC12**：现有 21 个 router 在不安装 peer 包的情况下 `make test` 全绿（隔离回归）。
- [ ] **AC13**：`src/orchestratord/orchestrator.py`、`workflow_orchestrator.py`、`modes/*`、`kernel/*`、`agent/*` 在 Phase 1 PR 中**零行修改**（验证：`git diff --stat master...phase-1` 不含这些路径）。注意：`api/realtime.py` 必须改（抽抽象+RedisBackend），但**不修改 publish 行为**。
- [ ] **AC14**：README 增加 "Peer Federation (Phase 1)" 小节，给出 A1/B1 启动示例、Redis docker-compose 片段、`ORCHESTRATORD_PEER_TRUST` 说明、**v3 Token Rotation SOP**（D11）。
- [ ] **AC15**：ADR-001-peer-federation.md 记录决策（**v3** 修订：peer/1 自定义但借鉴 A2A 字段命名、HMAC 鉴权、Redis pub/sub Phase 1、auto-group、auto-schedule 受控、CLI 群操作、不强制浏览器直连 + **D10-D21 新增**：workspace 边界、手工 rotate、多群粒度、跨 daemon 会话、msg_id dedup、per-session FIFO、workspace-scoped registry、Redis key 前缀、超时、GOODBYE、速率限制、去中心化群管理**）。

**v3 新增 AC（AC16-AC27；详见 ADR-001 §Verification）**：
- AC16 跨 ws 邀请必须显式 `workspace_id`；AC17 文档含 rotate SOP；AC18 单 daemon 多群；AC19 `POST /sessions` 端点 + audit `invited_by_*`；AC20 msg_id 30s dedup；AC21 ordering 警告；AC22 peers 表在 workspace DB；AC23 Redis key 前缀；AC24 10s/30s/3× 超时；AC25 GOODBYE 帧 + 5s drain；AC26 100/200 速率限制；AC27 任何成员 invite/kick（无 owner）。

### §8.2 Phase 2-4 待办（不在本设计范围）

- [ ] **Phase 2**（**缩减**）：原"抽 RealtimeBackend + Redis"已被提前到 Phase 1（AC7）；Phase 2 改为 Redis Sentinel / Cluster 高可用 + pub/sub 性能优化 + JWT tokens（v3 D11 alt-B）+ Prometheus 指标 + 备份集成。
- [ ] **Phase 3**（**废止**）：原"远端消息自动调度"已被提前到 Phase 1（AC11）。
- [ ] **Phase 4**：可选 ANP/libp2p transport backend（解 NG1）；DID 签名替换 HMAC。

### §8.2 Phase 2-4 待办（不在本设计范围）

- [ ] **Phase 2**（**缩减**）：原"抽 RealtimeBackend + Redis"已被提前到 Phase 1（AC7）；Phase 2 改为 Redis Sentinel / Cluster 高可用 + pub/sub 性能优化。
- [ ] **Phase 3**（**废止**）：原"远端消息自动调度"已被提前到 Phase 1（AC11）。
- [ ] **Phase 4**：可选 ANP/libp2p transport backend（解 NG1）；DID 签名替换 HMAC。

## §9. 开放问题（待用户拍板）

- **Q1**：是否接受 Phase 1 范围？特别是 NG4（远端消息不自动调度）与 NG5（浏览器只连自己 daemon）。
- **Q2**：协议骨架是否采用 A2A 兼容？若完全自定义 `peer/1` 可省去 `/.well-known/agent.json` 但失去未来生态兼容。
- **Q3**：命名采用 "Peer 层"（本文）还是 "Federation 层"？
- **Q4**：ORCHESTRATORD_PEER_TRUST 白名单机制是否需要？默认只走人工 accept。
- **Q5**：审计日志是否现在就要打 `peer_call_id` 串联字段（AC12 隐含），还是 Phase 2 再做？

## §10. 实施顺序建议（2026-09-08 v3 修订）

按以下顺序出 PR，每个 PR 跑 `make test` 必须全绿。**注意：Redis 抽象必须在 PR3 提前完成**（原 PR2 才做 Agent Card）。

每个 PR 末尾列出**本次 PR 落地的 D14-D26 决策**，便于 reviewer 追踪。

1. **PR1**：`peer/protocol.py`（含 `msg_id`/`ordering`/GOODBYE）+ `peer/hmac_sig.py` + `peer/nonce_store.py` + 单测
   - 落地：**D18**（msg_id）、**D19**（ordering 字段）、**D24**（GOODBYE 帧类型）
   - 估时：≈ 2 天
2. **PR2**：`peer/card.py`（按 §14.2 A2A 字段清单）+ `peer/registry.py`（workspace-scoped）+ `/.well-known/agent.json` 路由 + 单测
   - 落地：**D14**（workspace 边界字段）、**D20**（A2A 字段借鉴清单）、**D21**（workspace-scoped）
   - 估时：≈ 1.5 天
3. **PR3**（**关键路径，提前**）：`api/realtime.py` 抽出 `RealtimeBackend` 抽象 + `LocalBackend` + `RedisBackend` + `peer/redis_relay.py`（D22 Redis key 命名）+ `peer/agent_topics.py` + 单测
   - 落地：**D22**（Redis key 前缀）
   - 估时：≈ 3 天
4. **PR4**：`peer/client.py` + `peer/handshake.py`（D23 超时）+ `peer/group.py`（D16 多群 + D26 去中心化）+ `cli/serve.py --peer-listen` + 单测
   - 落地：**D16**（多群）、**D23**（connect/HELLO 超时）、**D26**（去中心化群管理）
   - 估时：≈ 2.5 天
5. **PR5**：`api/routers/peer.py` 8 端点（含 `POST /sessions`，D17）+ `require_peer_auth` 依赖（D25 速率限制）+ `peer/dispatcher.py`（D18 dedup + D19 ordering 校验 + auto-schedule 受控）+ `cli/peer.py` + `audit_log.invited_by_*` 字段（D17）+ 单测
   - 落地：**D17**（跨 daemon 会话 + audit 字段）、**D18**（dedup 落地）、**D19**（ordering 校验）、**D25**（速率限制）
   - 估时：≈ 3.5 天
6. **PR6**：跨进程集成测试（两个 `orchestratord serve` + Redis + 远端 INVOKE + SSE 订阅 + auto-schedule）+ README（D15 手工 rotate SOP）+ ADR-001 v3
   - 落地：**D15**（rotate 文档）、**D24**（shutdown hook 注册到 daemon 入口）
   - 估时：≈ 2 天

合计 **约 14.5 个工作日**（v3 比 v2 多 2 天，主要由 msg_id/dedup、跨 daemon session 端点、速率限制、shutdown hook 增量）。

---

## 引用文件清单（已用代码佐证）

- `src/orchestratord/ipc/{protocol,client,models}.py`
- `src/orchestratord/api/app.py:127-148`
- `src/orchestratord/api/realtime.py:35-142`
- `src/orchestratord/api/routers/realtime.py`
- `src/orchestratord/api/routers/sessions.py:155-216, 332-430`
- `src/orchestratord/api/routers/{inbox,agents,integrations}.py`
- `src/orchestratord/api/deps.py:66-94`
- `src/orchestratord/cli/serve.py:38-40`
- `src/orchestratord/cli/server.py`
- `src/orchestratord/chat_gateway.py`
- `src/orchestratord/chat_daemon.py`
- `src/orchestratord/control_socket.py`
- `src/orchestratord/bridge/worker.py`
- `src/orchestratord/domain/{auth_token,mention,channel}.py`
- `src/orchestratord/db/models/audit_auth.py:29-39`
- `src/orchestratord/modes/{single,pipeline,coordinator,swarm}.py`
- `docs/FEATURE_GAP_VS_MULTICA.md:41, 832`

---

## §11. 需求覆盖矩阵（User-Requirement → Design-Evidence）

把用户原始诉求的每一句**字面诉求**与设计文档/ADR/源码证据一对一映射，供 reviewer 一眼验证完整性。

### §11.1 用户原话 → 设计章节

| # | 用户原话（关键短语） | 设计证据 |
|---|---|---|
| **U1** | "查看当前项目提供的能力" | §1 现状盘点表（6 通道全列）；file:line 引用均已 Read |
| **U2** | "不同（守护）进程的编排器之间也可以交互" | §4 协议 `peer/1`；§5 握手时序；§6 改动清单 |
| **U3** | "A1 连接 B1" "类似与加群" "去中心化地加入一个群" | §5 握手流程图（A1 → B1）；§4 D4 "join-a-group 握手"决策；ADR-001 D4 |
| **U4** | "B1（或者 A2）允许的情况下" | §5 "必须 B1 operator 显式接受"；§2 G2 "信任 gate"；ADR-001 D4 |
| **U5** | "A1 可以调用 B1" | §6.1 `api/routers/peer.py` `POST /api/peer/peers/{orch-id}/invoke`；§8 AC6 |
| **U6** | "可以看到 B1 的状态（包含但不限于当前任务的会话消息，历史会话记录）" | §6.1 `GET /api/peer/peers/{orch-id}/events`（实时）；§4 D7 远端 SSE；§8 AC7 |
| **U7** | "A1 中的任何一个 Agent 也可以与 B1 中任何一个 Agent 进行交互" | §7 R1（远端消息落库）；§6.2 `domain/mention.py` 扩 `@agent@orch-id`；§10 PR4 |
| **U8** | "互通消息" | §6.1 peer INVOKE 调用 `POST /api/sessions/{sid}/messages`；§8 AC6 |
| **U9** | "相互查看对方的状态" | §6.1 `GET /api/peer/peers/{orch-id}/events`；§4.3 skills 包含 `sessions.read` / `agents.list` |
| **U10** | "搜索开源社区查看是否有类似项目" | §3 表（9 个项目对比 + 评级）；turn 2 引用 A2A Protocol / MCP / AgentTeams / ANP / Synaptic Mesh 等 web 链接 |

### §11.2 "当前能力 → 缺口 → 新增"逐项追踪

| 用户能力诉求 | 当前是否已有 | 缺口 | 设计新增 |
|---|---|---|---|
| 跨进程 RPC 调用 | ✗（仅同进程 `BackendRunner`） | 无 | `peer/client.py` + `api/routers/peer.py` §6.1 |
| 远端能力发现 | ✗（本地 `BackendRunner.registry` 仅在进程内） | 无 | `peer/card.py` + `/.well-known/agent.json` §4.3 |
| 远端状态订阅 | ✗（`RealtimeBroker` 仅单进程，注释已留 Redis 扩展点） | RealtimeBroker 无 backend 抽象 | §4 D7：Phase 1 远端 SSE 直连；Phase 2 抽抽象 |
| 远端 Agent 互通 | ✗（`@mention` 解析存在但只路由到本地 dispatcher） | 无远端解析 | §6.2 扩 `parse_mentions` 支持 `@agent@orch-id` |
| 信任门 / 加群审核 | ✗（无 peer 概念） | 无 | §5 握手 + `/api/peer/invite` 端点 + D4 决策 |
| 远端消息落库 | ✓（`POST /api/sessions/{sid}/messages` 已 DB-backed） | 无 | 复用现有端点，无需新增 |
| 远端读取历史会话 | ✓（`GET /api/sessions/{sid}/messages` 已存在） | 无 | 复用现有端点，无需新增 |
| 远端 agent 调度新 turn | ✗（chat daemon 仅轮询本地 pending sessions） | 无 | §7 R1：Phase 1 落库 ≠ 自动调度；Phase 3 自动 schedule |

### §11.3 "用户场景 → 协议动作"逐一验证

| 用户场景 | A1 动作 | B1 动作 | 协议帧序列 |
|---|---|---|---|
| A1 发现 B1 | `GET https://b1.example.lan:9001/.well-known/agent.json` | 响应 Agent Card | HTTP GET（无需 peer frame） |
| A1 申请加入 B1 群 | `POST /api/peer/invite`（含自身 Agent Card + 签名） | 投 inbox "INVITE_REQUEST"；operator 人工 `accept` | HTTP POST → 202 |
| A1 完成握手 | `HELLO` frame（orch_id + 签名） | `WELCOME` frame（含双向 subscribe 句柄） | peer/1 frame |
| A1 读 B1 的 sessions | `INVOKE GET /api/workspaces/{ws}/sessions` | `RESULT (200, [...sessions])` | peer/1 frame |
| A1 给 B1 的 session 发消息 | `INVOKE POST /api/sessions/{sid}/messages` | 落库 `messages` 表；写 `audit_log.peer_call_id` | peer/1 frame |
| A1 订阅 B1 的 session event | `SUBSCRIBE topic=session.*` | `EVENT` SSE 推流 | peer/1 frame + HTTPS SSE |
| A1 的 agent @B1 的 agent | A1 UI：`@bot-x@orch-B1` → mention 解析 | B1 mention dispatcher → 落库 message | peer/1 INVOKE |
| B1 移除 A1 | — | `DELETE /api/peer/peers/{orch-A1}` | A1 后续 INVOKE → 401/403 |

### §11.4 验证 checklist（reviewer 用）

- [ ] §1 现状盘点表每一行 file:line 引用都可在仓库 `Read` 成功
- [ ] §3 表格中 9 个开源项目链接在 2026-09 时仍可访问
- [ ] §4.3 Agent Card JSON 经 `python -c "import json; json.loads(open('card.json').read())"` 通过
- [ ] §5 握手时序每一步都有对应路由/帧类型可指
- [ ] §6.1 / §6.2 改动清单的每行 file 都存在
- [ ] §7 R1-R15 每个风险都对应一个 Phase X 缓解措施
- [ ] §8 AC1-AC15 每个 AC 都能在测试或 PR 描述中找到证据
- [ ] §10 PR1-PR6 每个 PR 都能单独跑 `make test` 全绿
- [ ] §11.5 D14-D26 每条决策在 §4-§10 都有具体落地位置（不止在 D 表里出现一次）

### §11.5 D14-D26 决策矩阵（v3 新增；reviewer 必查）

| 决策 | 落地章节 / 文件 |
|---|---|
| **D14** Workspace 边界（**同 ws 天然允许，跨 ws 显式 invite**） | §2 G2 + §5 握手注；§6.1 `peer/registry.py` 加 `workspace_id` 字段 |
| **D15** Token 手工 rotate（**无自动 + grace period**） | §6.2 `config/schema.py` 不加 rotate 字段；README SOP；R15 文档 |
| **D16** 群组粒度（**orch_id 粒度 + 单 daemon 多群**） | §6.1 `peer/group.py` 数据模型；§2 G6 修订 |
| **D17** 跨 daemon 会话创建（**允许 + audit 带 invited_by**） | §2 G9；§6.1 `api/routers/peer.py` 新增 `POST /sessions`；§6.2 `audit_auth.py` 新增字段；§7 R7 |
| **D18** at-least-once + `msg_id` + dedup | §4.2 新字段；§6.1 `peer/protocol.py` + `peer/dispatcher.py`；§7 R13 |
| **D19** per-session FIFO；跨 session 无序 | §4.2 `ordering` 字段；§6.1 `peer/dispatcher.py`；§7 R14 |
| **D20** A2A 字段借鉴清单 | §4.3 Agent Card；§14.2 推荐方案 |
| **D21** Peer 表 workspace-scoped | §6.1 `peer/registry.py`；§7 R10 缓解 |
| **D22** Redis key `orch:peer:{orch_id}:topic:{topic_name}` | §6.1 `peer/redis_relay.py`；§7 R11 缓解 |
| **D23** 超时 connect 10s + HELLO 30s + 3× 指数退避 | §5 握手注；§6.1 `peer/client.py` + `peer/handshake.py`；§6.2 config |
| **D24** GOODBYE 帧 + 5s drain in-flight | §4.1 新帧类型；§6.1 `peer/client.py`；§6.2 daemon 入口 shutdown hook |
| **D25** 速率限制 100 INVOKE/s/peer，burst 200，可配置 | §6.2 `api/deps.py` + `config/schema.py`；§7 R12 兜底 |
| **D26** 去中心化群管理（**任何已 accept 成员 invite/kick，无 owner**） | §2 NG9；§6.1 `peer/group.py` + `cli/peer.py`；§11.5 本行 |

---

## §12. 复审签字栏（Reviewer Sign-off）

| Reviewer | 角色 | 签字日期 | 备注 |
|---|---|---|---|
| | Architecture owner | | |
| | Security reviewer | | |
| | Backend maintainer | | |
| | UX/Operator experience | | |

**合并门槛**：
- 全部 AC1-AC12 勾完（§8.1）
- §11.4 验证 checklist 全部勾完
- 至少 2 位 reviewer 签字

---

## §13. 元数据

- **首次创建**：2026-09-08
- **作者**：Claude（基于同日用户 goal 分析）
- **来源对话**：用户 goal "查看当前项目提供的能力…让不同进程的编排器之间也可以交互"
- **关联文档**：`ADR-001-peer-federation.md`、`project_inter_orchestrator_connections.md`（memory）、`project_peer_federation_design.md`（memory）、`project_peer_federation_user_decisions.md`（memory）
- **状态机**：`DRAFT → REVIEW → ACCEPTED → SUPERSEDED`（当前 `DRAFT`）

---

## §14. 协议骨架：A2A 兼容 vs 自定义 `peer/1` 可扩展性对比

（用户回合 7 提出 O1："协议骨架是否采用 A2A 兼容的 Agent Card 还是完全自定义 peer/1 需要再分析一下哪个可扩展性更好"——以下分析后给出推荐。用户 D4 已定走 `peer/1`，本节为推荐理由。）

### §14.1 五个维度的可扩展性评分

| 维度 | A2A 兼容 | 自定义 `peer/1`（推荐） | 评分理由 |
|---|---|---|---|
| **D-α：生态兼容** | ★★★★★ 直接可与 Google A2A / MCP / IBM 适配 agent 互调 | ★★ 自有协议，外部 agent 不能 0 成本接入 | A2A 优；但 orchestratord 目标用户是 self-hosted LAN（Operator 自己搭 daemon），外部生态接入非 Phase 1 优先级 |
| **D-β：协议演进自由度** | ★★ 必须跟 A2A spec 演进（每年可能大版本变更）；breaking change 受限 | ★★★★★ 完全自主：加帧类型、改字段、加 topic 都不需要走外部 RFC | 自定义优；orchestratord 内部还要支持 chat dispatcher、skill loader、kernel event bus 等多个域，自有协议演进空间大 |
| **D-γ：调试难度** | ★★★ 标准 JSON-RPC 框架，工具链成熟；但 A2A + gRPC + SSE 三传输并存让 trace 复杂 | ★★★ 单一 HTTPS + JSONL + SSE，自带 `peer_call_id`，抓包 = 看 HTTPS body | 持平；自定义版本更易在 LAN 抓包 |
| **D-δ：多传输支持** | ★★★★★ A2A spec 强制 JSON-RPC + SSE + gRPC 三传输 | ★ HTTPS only（Phase 1）；Phase 4 才考虑 libp2p/ANP | A2A 优；但 orchestratord 不需要 gRPC（Python + FastAPI 生态） |
| **D-ε：社区参与成本** | ★★ 需要向 A2A 工作组解释需求、提交 PR、follow 节奏；进程可能数月 | ★★★★★ 完全自主；任何变更只对内部 reviewers 负责 | 自定义优；orchestratord 是单组织项目，外部标准组织的流程负担是负价值 |

**总分（按 D-α/D-γ/D-δ 等权 + D-β/D-ε 等权加权）**：A2A 11 分 vs 自定义 13 分——微弱优势倾向自定义。

### §14.2 推荐：自定义 `peer/1` + 选择性借鉴 A2A 子集

**Why 自定义**：
- 生态兼容（A2A 优势维度）只在**对外暴露**场景重要；Phase 1 仅供 orchestratord daemon ↔ daemon 通信，无对外暴露诉求。
- 协议演进自由度（自定义优势维度）是**长期成本**：orchestratord 还在快速演进（kernel 解耦、chat dispatcher、skill loader），被外部 spec 锁住会拖慢迭代。
- 多传输支持（A2A 优势维度）在 Python 生态用不上 gRPC（grpc.aio + FastAPI 集成摩擦大），HTTPS + SSE 已覆盖所有需求。

**借鉴 A2A 子集（不暴露 A2A 标识；**v3 按 D20 落地清单**）**：
- ✅ Agent Card JSON 字段命名（A2A spec §4.x 对齐）：
  - `name`（orchestrator 实例名）
  - `description`（人类可读描述）
  - `url`（orchestrator 入口 URL）
  - `version`（orchestratord 版本）
  - `provider`（`{organization, contact}`）
  - `capabilities[]`（能力字符串数组，如 `peer.invoke`、`sessions.message.post`）
  - `defaultInputModes`（默认输入模式列表，如 `text/plain`、`application/json`）
  - `skills[].id` + `skills[].description`（**借鉴到此为止**；`skills[].input_schema` 用自家 schema，不抄 A2A 完整定义）
- ✅ JSON-RPC 2.0 风格帧（`type` / `method` / `params` / `result` / `error`）
- ✅ Bearer + HMAC 鉴权模型（A2A 推荐 OpenAPI security schemes 子集；用 `security_schemes.bearer` + `security_schemes.hmac` 两段描述）
- ❌ **不**借鉴：JSON-RPC 协议绑定（orchestratord 帧更扁平，无 transport-level RPC 抽象）
- ❌ **不**借鉴：A2A task state machine（用本地 session 状态机；A2A 的 working/input-required/completed 映射不直观）
- ❌ **不**借鉴：A2A streaming / SSE artifact 协议（与本地 `RealtimeBroker` + RedisBackend 冲突）
- ❌ 不暴露 `protocol_version: "1.0"` 中的 "A2A" 字样
- ❌ 不跟 A2A spec 演进；自定义 `peer/1`

**折中表述**（用于 Agent Card 输出，让其他 A2A-aware 工具能识别但不假装兼容）：
```json
{
  "name": "orchestratord-A1",
  "protocol_version": "peer/1",  // 不是 A2A
  "agent_card_version": "draft-2026-04-a2a-style",  // 借鉴 A2A 字段命名
  "inspired_by": ["a2a-protocol-v1.0", "mcp-2026-07"],
  ...
}
```

### §14.3 反向条件（什么时候改回 A2A 兼容）

未来**以下任一条件**触发，应重新评估改为 A2A 兼容：

1. 出现大客户要求 orchestratord 与其自家 A2A agent 互调
2. A2A 出现 orchestrator↔orchestrator 联邦 profile（目前 spec 只有 client↔server）
3. Google / Microsoft / Salesforce 把 A2A 升级为 W3C / IETF 标准
4. orchestratord 用户基数突破 10k，需要生态互通

**结论**：Phase 1 走 `peer/1`，但**借鉴 A2A 字段命名以保持未来迁移余地**。

---

## §15. 设计变更日志（与 §13 状态机对应）

| 日期 | 变更 | 来源 |
|---|---|---|
| 2026-09-08 v1 | 初稿（DRAFT） | Claude 起草 |
| 2026-09-08 v2 | 加入 §14 A2A vs peer/1 可扩展性对比；Goals/Non-Goals 调整；§7 R1 缓解改为 Phase 1 自动调度（受控）；§6 新增 `peer/redis_relay.py` + `cli/peer.py` | 用户回合 7 反馈 |
| 2026-09-08 v3 | （1）§2 升级 v3；G6 补"多群"；新增 G9（跨 daemon session 创建）、G10（at-least-once + 顺序保证）；新增 NG8（自动 token 轮换）、NG9（群组 owner）。（2）§4.1 新增 GOODBYE 帧；§4.2 新增 `msg_id` + `ordering` 字段。（3）§5 握手注加超时（connect 10s / HELLO 30s / retry 3×）+ workspace 边界说明。（4）§6.1 / §6.2 文件描述全部按 D14-D26 重新标注（registry workspace-scoped、group 多群去中心化、router 增加 `/sessions` 端点、audit 加 `invited_by_*`、deps 加速率限制、main.py 注册 shutdown hook）。（5）§7 新增 R13（dedup 窗口重启丢失）、R14（ordering 校验失败）、R15（手工 rotate 窗口期）；R7/R10/R11/R12 缓解升级。（6）§8 / §11 升级指向 v3 AC。（7）§10 PR 序列标注每个 PR 落地的 D14-D26 决策；合计 14.5 工作日。（8）§11.5 新增"D14-D26 决策矩阵"小节。（9）§14.2 落地 A2A 字段借鉴的具体清单（D20）。 | 用户回合 8-9 反馈（D14-D26 共 13 项决策）；详见 `project_peer_federation_user_decisions.md` |
| 2026-09-09 v4 | **实施期回填**。§6.1 新增 4 行反映 v3 spec 未列、但实施期落地的文件：`peer/connections.py`（D24 shutdown drain + Phase-B bridge hook 句柄；spec 缺失）、`db/models/peer.py`（D16 workspace-scoped `peers` 表 ORM；spec 仅隐含）、`alembic/versions/0046_create_peers_table.py`（`peers` DDL）、`alembic/versions/0047_add_peer_audit_fields.py`（audit `invited_by_*`）。总行数从 ~1950 上调至 ~2165（+215），测试从 ~450 上调至 ~700（+250，因 `peer_integration/test_two_daemon.py` + `db_integration/test_peer_registry.py`）。**Phase-1 边界已声明**：`serve` 未绑定 peer/1 帧监听器，跨 daemon 流量走 HTTP REST + bearer token + `X-Peer-Orchestrator-Id`；peer/1 帧编解码完整但 frame transport 留 Phase B。 | 实施完成态对照（commit `512a86c` + `51f7a9a`）；ADR §Verification AC6/AC7 同步更新集成测试路径到 `tests/peer_integration/test_two_daemon.py` |