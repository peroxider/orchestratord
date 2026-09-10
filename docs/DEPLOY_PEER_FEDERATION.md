# Peer Federation 生产部署指南（PR-B6）

两个（或多个）`orchestratord` daemon 通过 `peer/1` 协议互联的完整部署
手册：TLS 自签证书、daemon 启动、信任建立、token 生命周期与运维参数。
协议设计见 `DESIGN_PEER_FEDERATION.md`，安全边界见 `ADR-001-peer-federation.md`。

## 1. 架构概览

* 每个 daemon 的 HTTP 表面（REST + `POST /peer/v1/stream` frame 传输）
  由同一个 uvicorn listener 承载（单端口设计）。
* 认证两层：TLS（本指南）之上的请求线 bearer token +
  `X-Peer-Orchestrator-Id`（`require_peer_auth`），JSONL 帧再带
  HMAC-SHA256 签名 + nonce 防重放（`peer.hmac_sig`）。
* 服务端 D25 限流：REST 路径按请求、frame 路径按 INVOKE 帧，
  共享同一 per-peer token bucket（默认 100 rps / burst 200）。

## 2. 证书生成与分发

每个 daemon 生成自己的本地 mini-CA 并签发 server 证书：

```bash
scripts/gen_peer_tls_certs.sh --out-dir ./peer-tls --hostname orch-a1.example.com
```

产物：

| 文件 | 用途 | 分发 |
|---|---|---|
| `ca.key` | 本地 CA 私钥 | **不外发** |
| `ca.pem` | 本地 CA 证书 | 发给所有要拨入本 daemon 的对端 |
| `server.crt` / `server.key` | TLS server 证书/私钥（SAN 含指定主机 + localhost/127.0.0.1/::1） | 留在本机，供 serve 使用 |

**信任模型**：双向各持对方 `ca.pem`。daemon A 要拨号 B，就把 B 的
`ca.pem` 放到 A 机器上，通过 `ORCHESTRATORD_PEER_TLS_CA` 指给客户端；
反向同理。证书链通过 `openssl verify -CAfile ca.pem server.crt` 自检
（脚本结束时也会打印该命令）。

## 3. 启动 daemon

```bash
# daemon A（9000 端口，TLS）
orchestratord serve --host 0.0.0.0 --port 9000 \
  --tls-certfile ./peer-tls/server.crt --tls-keyfile ./peer-tls/server.key

# daemon B（同拓扑，另一个端口/机器）
orchestratord serve --host 0.0.0.0 --port 9000 \
  --tls-certfile ./peer-tls/server.crt --tls-keyfile ./peer-tls/server.key
```

TLS 参数有 env 兜底（便于容器/双 daemon 集成测试免 CLI 改造）：

| 环境变量 | 作用 |
|---|---|
| `ORCHESTRATORD_TLS_CERTFILE` / `ORCHESTRATORD_TLS_KEYFILE` | serve 的 TLS 证书/私钥（未设置 = 纯 HTTP，仅回环部署） |
| `ORCHESTRATORD_PEER_TLS_CA` | **客户端**拨号对端时信任的 CA 证书路径（PEM） |
| `ORCHESTRATORD_PEER_TLS_INSECURE` | `1` = 关闭客户端证书校验。**仅限开发/诊断**，每次生效都会打 WARNING |
| `ORCHESTRATORD_PEER_LISTEN` / `ORCHESTRATORD_PEER_FRAME_LISTEN` | peer listener 绑定（§6.2 / PR-B2.1） |
| `ORCHESTRATORD_REDIS_URL` | 跨 daemon 事件扇出（Redis backend） |

客户端拨号信任配置示例（daemon A 拨 B）：

```bash
export ORCHESTRATORD_PEER_TLS_CA=/opt/orchestratord/pki/peer-b-ca.pem
```

## 4. 建立信任（invite / accept 握手）

```bash
# 4.1 B 向 A 申请加入 A 的工作区
curl -sk https://a1.example.com:9000/api/peer/invite \
  -H 'Content-Type: application/json' \
  -d '{"orch_id":"orch-B","name":"Daemon B","url":"https://b1.example.com:9000",
       "workspace_id":"<WS_UUID>","capabilities":["peer.invoke"],
       "peer_client_version":"v2"}'
# → 202 {"status":"pending","peer_id":"..."}

# 4.2 A 侧操作者接受 → 一次性返回 per-peer token（只显示这一次）
curl -sk -X POST https://a1.example.com:9000/api/peer/invite/<peer_id>/accept
# → {"status":"accepted","orch_id":"orch-B","token":"<PLAINTEXT>"}

# 4.3 B 持 token 拨 A：frame 传输自动协商（Agent Card preferred_transport=frame）
```

预信任白名单（跳过人工 accept，invite 响应直接 200 带 token）：

```bash
export ORCHESTRATORD_PEER_TRUST=orch-B,orch-C   # R10
```

> 不带 `peer_client_version` 的 Phase 1 客户端会被打上
> `client_kind="v1_sunset"`（peers 表 + `GET /api/peer/peers` 的
> `client_kind` 字段可见），用于日落期观察。

## 5. Token 生命周期（D15 / NG8）

* token 明文只在 accept/rotate 响应中出现一次，库中只存 SHA-256。
* **轮换**（旧 token 不立即失效）：

```bash
curl -sk -X POST \
  'https://a1.example.com:9000/api/peer/peers/orch-B/rotate-token?workspace_id=<WS_UUID>'
# → {"status":"rotated","token":"<新明文>","grace_seconds":300.0,
#    "old_token_expires_at":"2026-09-10T12:05:00+00:00"}
```

* grace 期内（默认 300s，`ORCHESTRATORD_PEER_TOKEN_GRACE_SECONDS` 可调，
  `0` = 立即失效）旧 token 继续可用，在途连接不断流；到期后由
  `require_peer_auth` 的标准 `expires_at` 检查自然拒绝。
* **移除**（AC8）：`DELETE /api/peer/peers/{orch_id}?workspace_id=...`
  删除注册行，对端立即失去访问。

## 6. 运维参数

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ORCHESTRATORD_PEER_RATE_RPS` / `ORCHESTRATORD_PEER_RATE_BURST` | 100 / 200 | D25 per-peer 限流（超限 429 + Retry-After；frame 路径回 RESULT status=429 不断连） |
| `ORCHESTRATORD_PEER_DEDUP_WINDOW` | 30 | D18 at-least-once 去重窗口（秒） |
| `ORCHESTRATORD_PEER_AUTO_SCHEDULE` | 0 | NG4：远端消息是否自动调度 agent turn |
| `ORCHESTRATORD_MAX_CONCURRENT_PEER_TURNS` | 4 | R12 并发 peer turn 上限 |
| `ORCHESTRATORD_PEER_TOKEN_GRACE_SECONDS` | 300 | NG8 rotate grace（秒） |
| `ORCHESTRATORD_PEER_NONCE_PATH` | /tmp/... | HMAC nonce 防重放 sqlite 路径（生产建议持久盘） |

## 7. 故障排查

* **`certificate verify failed`（客户端）**：`ORCHESTRATORD_PEER_TLS_CA`
  没指对端 CA，或对端证书 SAN 不含所拨主机名（重新用 `--hostname`
  生成）。临时诊断可 `ORCHESTRATORD_PEER_TLS_INSECURE=1`，用完必须关。
* **`peer is not accepted`（401）**：token 与 `X-Peer-Orchestrator-Id`
  不匹配、peer 行不是 accepted 状态，或 token 已过 grace 期。rotate 后
  确认用的是新 token。
* **429**：D25 限流命中。REST 看 `Retry-After` 响应头；frame 路径看
  RESULT body 的 `retry_after`。调大 `ORCHESTRATORD_PEER_RATE_*`。
* **ordering gap 告警（D19）**：发送端 per-session `ordering` 序列有
  跳号/重复——消息照常投递，审计行带 `out_of_order: true`，检查发送端
  序列生成。
* **关停丢消息**：daemon 关停会先向所有 peer 发 GOODBYE 并 drain 5s
  （D24）；确认没有绕过 `orchestratord serve` 直接 kill -9。
