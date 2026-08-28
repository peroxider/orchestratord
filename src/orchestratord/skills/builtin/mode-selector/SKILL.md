---
name: mode-selector
display_name: Mode Selector
description: 帮 orchestrator 在 single / pipeline / coordinator / debate / swarm 间选择合适的多 agent 编排模式
user_invocable: false
allowed_tools: []
version: 1
---

# Mode Selector

## 何时使用

当 orchestrator 收到一个 issue 但没有显式 `mode:` label，需要推断该 issue
适合哪种多 agent 编排模式时调用本 skill。

## 模式一览

| mode | 含义 | 适用场景 |
|---|---|---|
| `single` | 单 agent 顺序执行 | 默认；大多数实现类 issue |
| `pipeline` | 多 agent 顺序接力 | 明确的先后阶段（如 先设计→再实现→再验证） |
| `coordinator` | 主 agent 拆解 + 分发 + 汇总 | 跨模块重构、需要全局视角的评审 |
| `debate` | 两个独立提案者 + 一个裁决者 | 设计方案对比、技术选型 |
| `swarm` | 动态分解依赖波次并行执行 | 彼此独立的批量任务 |

## 选择规则

路由逻辑在 `HeuristicRouter`（见 source-map 引用）。按顺序匹配：

1. **swarm 关键词**（parallel / concurrently / 各自 / 批量）→ `swarm`
2. **debate 关键词**（design / debate / compare / vs / 评审方案）→ `debate`
3. **coordinator 关键词**（refactor / 跨模块 / 协调）→ `coordinator`
4. **pipeline 关键词**（sequential / 先后 / 阶段）→ `pipeline`
5. **默认** → `single`（低置信度，selector 可能回退）

mode 的合法集合由 `KNOWN_MODES` 定义，包含 `auto`（元模式，交由路由决定）。

## 输出

返回 mode 名字符串（`single|pipeline|coordinator|debate|swarm` 之一）。
