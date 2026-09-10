---
name: orchestratord-share-learnings
display_name: Share Learnings
description: 将本次 session 踩过的坑与解法沉淀为结构化 learnings 文档（friction 超阈值后的情景经验写侧）
user_invocable: true
allowed_tools: []
version: 1
---

# Share Learnings

## 何时使用

当 session 触发经验闭环（friction 评分超过 learnings 门槛），或用户
明确要求"把这次的坑记下来"时调用。产出一份单文件 Markdown 经验文档，
供 `orchestratord recall` 检索命中。

## 核心原则

**转述而非摘抄。** 经验文档必须用自己的话总结"坑是什么、怎么解的"，
不得大段复制 session 原文；转述过程天然脱敏。

**隐私红线（fail-closed）。** 以下内容一律禁止出现：
- 凭据与密钥（token / api_key / password，包括打码后的残段）
- 内网 URL 与内网主机名（10.x / 192.168.x / *.internal 等）
- 客户名、真实邮箱、文件路径中的用户名

无法安全转述的细节直接省略，而不是尝试打码。

## 输出结构

文件名：`YYYY-MM-DD-<短横线小写slug>.md`，落盘目标为
`<workspace>/.orchestratord/learnings/`（无 workspace 上下文时
`~/.orchestratord/learnings/`）。

Frontmatter（缺一不可）：

```yaml
---
session_id: <本 session id>
issue_id: <issue 标识，无则留空>
repo: <仓库名>
friction_score: <0-100 整数>
tags: ["标签1", "标签2", "标签3"]
created_at: YYYY-MM-DD
---
```

正文依次包含四节：

1. **背景**——一句话说明当时在做什么（issue、环境、目标）。
2. **坑**——具体错误现象与误判路径，转述不摘抄。
3. **解法**——最终生效的排查/修复路径，以及"下次第一时间该查什么"。
4. **验证**——怎么确认解法有效。

## 标签要求

强制 3–5 个主题标签（design §8：免分词精确命中依赖标签路径）：
- `kind:` 前缀标信号类型（`kind:retry` / `kind:degradation` /
  `kind:approval` / `kind:error`）
- `backend:` 前缀标后端（如 `backend:claude`）
- 其余为自由主题词（如 `git-push`、`测试超时`、`权限策略`）
