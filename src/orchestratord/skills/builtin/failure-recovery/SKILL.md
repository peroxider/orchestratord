---
name: failure-recovery
display_name: Failure Recovery
description: agent 会话失败（timeout / max_turns / rate_limit / stagnation 等）后决定下一步：重试、降级还是放弃
user_invocable: false
allowed_tools: []
version: 1
---

# Failure Recovery

## 何时使用

当 orchestrator 收到 ERROR 事件或 `SESSION_COMPLETE(reason=error)` 时，
需要决定对该 issue 执行哪种恢复动作时调用本 skill。

## 失败类别与推荐动作

| 失败类别 | 判断依据 | 推荐动作 |
|---|---|---|
| 超时 | `FailureContext.timeout_ms > 0` | 自动重试（加大 turn 预算或沙箱超时） |
| 轮数耗尽 | `max_turns` 耗尽 | 自动重试（拆小任务或换 mode） |
| 限流 | 429 / rate limit | 指数退避后自动重试 |
| 停滞 | 连续 N 轮无产出 | 换 mode（如 single → debate）再重试 |
| 验证失败 | verification 输出非通过 | 自动重试并附验证输出为上下文 |
| 达到最大重试次数 | `attempt >= max_attempts` | 停止自动重试，需人工介入 |

## 重试机制

- 调度入口：`Orchestrator._schedule_retry`（见 source-map）— 计算退避
  延迟并把 issue 放入重试队列。
- 队列消费：`Orchestrator._process_retry_queue`（见 source-map）— 到期
  后重新派发 session。
- 用户文案：`failure_messages.py` 为每类失败生成带重试提示的友好消息；
  `_retry_hint` 依据 `attempt / max_attempts / retry_delay_ms` 决定提示
  "将自动重试"还是"已达上限请手动处理"。

## 人工兜底

达到最大重试次数后，orchestrator 会停止自动重试。用户可以给 issue 打
`agent:retry` 标签重新触发。

## 输出

返回一个动作名：`retry`（自动重试）/ `escalate`（换策略重试）/
`give_up`（停止并请求人工介入），附一句理由。
