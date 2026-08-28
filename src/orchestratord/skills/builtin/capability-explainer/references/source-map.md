# Source Map — capability-explainer

本文件把 SKILL.md 的每条声明钉到 orchestratord 仓库的源文件与行号。
loader 在启动期校验：文件存在 + 行号内容 hash 匹配。
任何源文件修改触发本 skill 文档 review。

## 引用表

| 声明 | 源文件 | 行号 | 期望 hash |
|---|---|---|---|
| "BackendCapabilities 数据类定义" | src/orchestratord/spi/capabilities.py | 13-14 | c83ed8d6 |
| "核心原则：backend 不自行降级，core 统一强制" | src/orchestratord/spi/capabilities.py | 3-7 | 6dbf4721 |
| "降级矩阵（能力位到 core 行为的对照表）" | src/orchestratord/spi/capabilities.py | 21-35 | b43c53c4 |
