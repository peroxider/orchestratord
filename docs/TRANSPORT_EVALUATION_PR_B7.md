# PR-B7 — Peer transport 评估 spike：REST invoke vs frame INVOKE

> 结论先行：**REST invoke 是当前唯一在真实 socket 上完全可用的 peer 调用通道。**
> frame transport 的"双向 chunked POST"设计在当前锁定的依赖组合
> （uvicorn 0.52.4 / starlette 1.6.0，见 `uv.lock`）上**不可用**——
> 服务端一旦开始发送响应，请求体就被切断。修复需要 PR-B9 的传输层改造
> （见 §5 建议），不是一行补丁。

## 1. 评估方法

`scripts/bench_peer_transport.py` 在进程内起一个真实 daemon
（scratch PostgreSQL 库 `orchestratord_bench` + uvicorn 真实端口，
与 `tests/db_integration` 同一套搭库方式），对两条 transport 做端到端压测：

* **REST**：每请求一次 `POST /api/peer/peers/{orch}/invoke`
  （request-line 认证 + D18 dedup 缓存 + DB 查询）。
* **Frame**：一条持久 `POST /peer/v1/stream`，INVOKE→RESULT 顺序往返，
  以及并发（pipelined）批。
* **压缩开关**：每个场景在
  `ORCHESTRATORD_PEER_FRAME_COMPRESS_BYTES=4096`（PR-B8 默认）与 `0`
  下各跑一遍（PR-B8 压缩只作用于 frame wire，REST 不受影响）。

复现：

```bash
uv run python scripts/bench_peer_transport.py --n-small 200 --n-large 60 --save
```

## 2. REST 基线（本机，2026-09-10）

| 场景 | 模式 | 结果 |
|---|---|---|
| REST small（~120B body） | 每请求一连接 | p50=4.8ms p95=6.1ms ≈193 rps |
| REST large（64KB padded） | 每请求一连接 | p50=5.7ms p95=7.9ms ≈170 rps |

64KB 大 payload 只增加 ~0.9ms p50 —— REST 的每请求开销（连接、认证、
dedup）在这个量级上不构成瓶颈。数字描述开发机（WSL2 + 本机 PG），
不外推生产。

## 3. Frame transport：BROKEN（真实 socket；PR-B9 批式绑定已修复，见 §5 后记）

bench 对 frame 路径的全部场景给出：

```
Frame all scenarios | persistent stream | BROKEN — no WELCOME/RESULT within 15s
```

### 3.1 证据链（最小探针，均已在本机复现）

1. **双向 chunked 探针**（FastAPI StreamingResponse +
   `async for chunk in request.stream()` 并发）：客户端发出的 body
   chunk **永远到不了** `request.stream()`，响应里只有流结束哨兵。
2. **先读完再响应探针**：同一端点改为先耗尽 `request.stream()`
   再返回响应 —— 所有 chunk（含间隔 10s 的慢 chunk）正常到达。
   说明 HTTP/客户端/服务端栈本身没问题，问题出在**响应开始后**。
3. **提前消费探针**：在返回响应**之前**就 `await stream.__anext__()`
   读到第一个 chunk（响应头 `X-Got-First` 证明拿到了），响应开始后
   后续 chunk 依然被切断。
4. **协议实现对照**：uvicorn `http="h11"` 与 `http="httptools"`
   行为一致。

### 3.2 根因

uvicorn 0.52（h11 与 httptools 两个协议实现）在 ASGI 应用发出
`http.response.start` 之后**停止向应用投递请求体**（`receive()` 直接进入
结束路径）。PR-B2 的 frame 设计（MCP Streamable HTTP 风格的双向
chunked POST：客户端持续上行帧、服务端同流下行 RESULT/EVENT）依赖
"响应开始后请求体继续流动"，在该版本组合上不成立。

### 3.3 为什么一直没被发现

* 单元测试（`tests/api/test_peer_frame_router.py`、
  `tests/test_peer_trace.py`）用 `TestClient`，它会**缓冲完整请求体**
  再执行应用 —— 双向语义从未在真实 socket 上被检验。
* 唯一真实 socket 的集成测试 `tests/peer_integration/
  test_two_daemon_frame.py` 因 SQLite 无法建 PG 方言 schema（JSONB）
  而一直 **skip**（见其 `pytest.skip("aiosqlite schema create failed")`
  路径）。
* `uv.lock` 在 PR-B6 之前已刷新到 starlette 1.6.0 / uvicorn 0.52.4，
  旧版本组合下的验证结果不再适用。

## 4. 传输特性对照

| 维度 | REST invoke | frame（chunked POST 设计） |
|---|---|---|
| 真实 socket 可用性（当前依赖） | ✅ | ❌（§3） |
| 每次调用开销 | 每请求连接 + 认证 + dedup | 流内零额外握手（设计值） |
| D18 dedup / D19 ordering | ✅ | ✅（同一 dispatcher） |
| 服务端推送 EVENT | SSE（`GET /api/peer/peers/{id}/events`，已有） | 同流推送（设计值，当前不可用） |
| PR-B8 压缩 | 不适用（请求未压缩） | wire 层透明支持 |
| trace 贯通（X-Trace-Id） | ✅ | ✅（frame headers） |
| D25 限流 | 每请求 | 每 INVOKE 帧 |

## 5. PR-B9 建议（按优先序）

1. **推荐：批式 POST 绑定** —— 保留 `/peer/v1/stream` 端点与帧格式，
   语义改为"一次 HTTP 请求 = 一个批次"：客户端把帧写到 body EOF，
   服务端读完整个 body（边读边派发）后一次性回放 RESULT/EVENT 并结束
   响应。会话 = 连续多次 POST（bearer 认证 + dispatcher 缓存天然跨批）。
   EVENT 推送走已有 SSE 端点。客户端 `HttpsFrameTransport` 改为
   每批开一个请求；`PeerClient.invoke/subscribe` 的上层 API 不变。
   这与 §3.1 探针 2 证明可行的模式完全一致。
2. **WebSocket 绑定**：真双向长连接，但认证、D24 drain、REVOKE 语义
   都要重做，改动面大于方案 1。
3. **临时止血**：二分 uvicorn/starlette 找到双向 chunked 可用的版本组合
   并钉住（`starlette<1.0` 一档）。风险：fastapi 0.141 要求新版
   starlette，降级可能牵连其它 surface；且只是推迟问题。

在 PR-B9 落地前，peer 调用请使用 REST 通道（Agent Card 的
`preferred_transport` 暂不建议改回 `rest`，但部署文档已加注）。

> **PR-B9 后记（2026-09-10，已落地）**：本节方案 1 已实施 —— frame
> 传输重绑为批式 POST，bench 的 frame 相位恢复出真实数字，§3 的
> BROKEN 判定随之移除。根因也比 §3 的表述更深一层：不只是
> "uvicorn 在响应开始后切断请求体"，而是 starlette 的
> `StreamingResponse` 在 `spec_version < 2.4` 的 ASGI 服务器
> （uvicorn h11/httptools 均为 2.3）上会并发跑一个
> `listen_for_disconnect` 任务，与响应流共用同一条 receive 通道，
> 边读 body 边写响应在结构上就是竞争条件。批式绑定的
> "先读完 body、再回放响应" 两段式结构彻底消除了这一竞争。
> 真 socket 回归见
> `tests/peer_integration/test_two_daemon_frame.py`（PR-B9/D5 起
> 跑 scratch PostgreSQL，不再永久 skip）。
>
> 数字注记：bench 里 frame 相位 ~48ms/次 而 REST ~5ms/次，差异来自
> 客户端栈而非批式协议 —— 本机（WSL2）上 async httpx 客户端连
> `GET /.well-known/agent.json` 也要 ~45ms，sync `httpx.Client` 则
> ~5ms；帧内 dispatch 开销在总时延中占比极小（空响应与全批次
> RESULT 回放同为一档）。

## 6. 附带产出

* `scripts/bench_peer_transport.py` —— 可复现基准（PR-B9 起 frame
  相位直接出真实数字；此前的受控超时 + BROKEN 判定已随批式绑定移除）。
* 两个真实 bug 修复：`PeerClient`/`HttpsFrameTransport` 自建
  `httpx.AsyncClient` 时 `httpx.Timeout` 缺 `pool` 参数直接
  `ValueError`（此前所有走 base_url 自建 client 的路径都会炸）。
* `docs/DEPLOY_PEER_FEDERATION.md` 增补 frame transport 现状警告。
