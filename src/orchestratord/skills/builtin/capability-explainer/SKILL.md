---
name: capability-explainer
display_name: Capability Explainer
description: 解释 BackendCapabilities 各能力位的含义与 core 侧统一降级路径
user_invocable: false
allowed_tools: []
version: 1
---

# Capability Explainer

## 何时使用

当 agent 收到包含 capability 位信息（如 `streaming_deltas=False`、
`resumable=False`）的上下文，需要理解"这意味着什么、core 会怎么降级"时
调用本 skill。

## 核心原则

**Backend 永远不自行降级。** 每个 backend 如实上报自己支持的能力位；
所有降级路径由 orchestration core 统一强制执行。backend 实现者只声明
"我有什么"，不实现"我没有时怎么办"。

## 能力位 → core 降级路径对照

| 能力位为 False 时 | core 的统一降级行为 |
|---|---|
| `streaming_deltas` | core 把整段 `text` 拆成伪增量，消费者始终看到 delta |
| `resumable` | core 把 session 日志重放为 prompt 实现续跑 |
| `interrupt` | core 标记 "abandoned"，turn-complete 时尽力丢弃 |
| `approval_hooks` | core 用 tool_filtering 预过滤危险工具 + 事后审计 |
| `tool_filtering` | core 在 approval 回调里 DENY 未授权工具 |
| `cost_reporting` | core 用 token 估算器代替真实成本 |
| `goal_mode` | core 回退到 swarm/coordinator 分解复杂 issue |

权威定义见 `spi/capabilities.py` 的 `BackendCapabilities`（见 source-map）。

## 输出

用一段话向用户解释该能力位的含义与对应的降级行为；引用上表的行。
