# Source Map — mode-selector

本文件把 SKILL.md 的每条声明钉到 orchestratord 仓库的源文件与行号。
loader 在启动期校验：文件存在 + 行号内容 hash 匹配。
任何源文件修改触发本 skill 文档 review。

## 引用表

| 声明 | 源文件 | 行号 | 期望 hash |
|---|---|---|---|
| "KNOWN_MODES 合法 mode 集合（含 auto 元模式）" | src/orchestratord/mode_selector.py | 44-51 | 6ea728df |
| "HeuristicRouter 决策流与关键词桶顺序" | src/orchestratord/mode_router.py | 127-168 | 45b90120 |
| "swarm 优先于 debate/coordinator/pipeline 的桶顺序" | src/orchestratord/mode_router.py | 163-168 | 60636d32 |
