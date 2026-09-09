# Source Map — failure-recovery

本文件把 SKILL.md 的每条声明钉到 orchestratord 仓库的源文件与行号。
loader 在启动期校验：文件存在 + 行号内容 hash 匹配。
任何源文件修改触发本 skill 文档 review。

## 引用表

| 声明 | 源文件 | 行号 | 期望 hash |
|---|---|---|---|
| "FailureContext 上下文字段（timeout/attempt/delay 等）" | src/orchestratord/failure_messages.py | 34-44 | 12b336de |
| "_retry_hint 重试提示决策（自动重试 vs 人工介入）" | src/orchestratord/failure_messages.py | 47-56 | 7c555c83 |
| "_schedule_retry 重试调度入口" | src/orchestratord/orchestrator.py | 3786-3795 | 5a333c56 |
| "_process_retry_queue 重试队列消费" | src/orchestratord/orchestrator.py | 4053-4062 | cb7dc218 |
