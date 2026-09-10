# DESIGN_EXPERIENCE_LOOP — 经验闭环合并设计（friction / learnings / recall × 检视意见规则回灌）

> 状态：v1（2026-09-10）。本文档合并两个来源的规划：
> ① 现有检视意见→规则回灌管线（`rules_learner.py`，触发环节断链）；
> ② `docs/FEATURE_GAP_VS_TEAMAI_CLI.md` §7.2 P0 规划（friction 评分 +
> share-learnings + recall）。合并原则：**共享触发层与检索层，保留
> 双存储分工**——不重写任何现有提取/去重/存储/注入机制。
> 关联：`DESIGN_PR_GATE_TEST.md`（新增 CLI 子命令需登记基线）、
> `FEATURE_GAP_VS_TEAMAI_CLI.md`（§7.2 部分被本文档取代）。

## 1. 背景与动机

### 1.1 现有 rules 回灌管线：链路完整，触发断链

```
检视意见 → follow-up session → GitSyncService 写 review metadata（git/sync.py:1246 review-pr/review-id）
        → 【断链】规则提取（原在 daemon 内，已移除）
        → workflow.rules.yaml（RuleStore）
        → PromptBuilder 注入（prompt_builder.py:401 _inject_rules_reference_from_store）
```

- 提取/去重/评分/裁剪全部就绪：`rules_learner.py` — `RuleEngine.extract`（:449）、
  `BatchedLLMJudge`（:85，LLM 去重/合并/冲突判定）、`RuleEngine.score`（:602）、
  `RuleEngine.prune`（:652）、`ExtractTracker`（:48，按 commit SHA 幂等）。
- 人工入口就绪：`orchestratord rules learn`（cli/rules.py，扫描未处理 follow-up commit）。
- **断点**：`interpret.py:1461 _apply_review_rules` 为 `pass` 桩——daemon 不再自动
  提取，依赖人工记得跑 CLI。检视意见不会自动变成规则。

### 1.2 teamai-cli 对标规划：三层全新建，与 rules 管线大量重叠

FEATURE_GAP §7.2 P0 规划新建：friction 评分（telemetry/friction.py）、
share-learnings skill、recall 子包（BM25 + graph-boost）、recall CLI/子 agent。
其中"从 session 素材提取知识"这一步与 rules 管线的能力完全重叠——分开建会
出现两套 LLM 提取、两套去重、两种存储格式。

### 1.3 合并收益

| 维度 | 不合并 | 合并后 |
|---|---|---|
| 提取能力 | 新写一套 learnings 提取 | 复用 RuleEngine + BatchedLLMJudge |
| 触发器 | rules 管线与 learnings 各缺各的 | 一个 friction 触发器喂两端 |
| 检索 | rules 只能全量注入 | recall 统一检索面：经验+约定+代码图 |
| P0 交付节奏 | 三层齐建才见效 | 第一阶段（触发+落盘）即可积累数据 |

## 2. 目标与非目标

**目标**
1. 检视意见规则回灌自动化：daemon 内自动触发 RuleEngine，`rules learn` 降级为人工补录入口。
2. 情景经验（learnings）自动沉淀：friction 超阈值的 session 生成结构化经验文档。
3. 统一检索：`orchestratord recall <query>` + recall 子 agent，索引 learnings + rules + repo_tracker。
4. 新增能力全部复用现有管线，零重复提取/去重逻辑。

**非目标（与 FEATURE_GAP D15 一致）**
- 不做跨团队 source 订阅、角色/标签过滤、co-author reconcile（单用户模式不补）。
- 不替代 `repo_tracker`（recall 复用其索引，不重建代码图）。
- 不在本期做 weekly digest / session save / MCP server（P1，另行切片）。

## 3. 目标架构

```
                    ┌─────────────────────────────────────────────┐
                    │  触发层（新增，唯一的新"判断"逻辑）             │
                    │  SESSION_COMPLETE（backend_runner.py:1438）   │
                    │    → friction 评分（telemetry/friction.py）   │
                    │    → 超阈值 → share-learnings                │
                    └──────────────┬──────────────────────────────┘
                                   │ 一个动作，喂两个消费端
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
  ┌───────────────────────────┐          ┌───────────────────────────────────┐
  │ 情景沉淀（写侧 A）           │          │ 蒸馏沉淀（写侧 B，复活断链）          │
  │ share-learnings skill 生成  │          │ daemon 内调用 RuleEngine +         │
  │ 结构化经验 → learnings/     │          │ BatchedLLMJudge 提取本 session      │
  │ （session 级，保留上下文）    │          │ follow-up commits → rules store    │
  └──────────────┬────────────┘          └──────────────┬────────────────────┘
                 │                                      │
                 ▼                                      ▼
     <workspace>/.orchestratord/            workflow.rules.yaml（不变）
       learnings/（workspace 级）            （PromptBuilder 全量注入，不变）
     + ~/.orchestratord/learnings/（全局兜底）
                 │                                      │
                 └──────────────┬───────────────────────┘
                                ▼
              ┌─────────────────────────────────────────┐
              │ 检索层（新增，统一读侧）                     │
              │ recall/ 子包：BM25 + graph-boost           │
              │ 索引：learnings + rules store + repo_tracker│
              │ 出口：`orchestratord recall <query>` CLI    │
              │      + agents/orchestratord-recall.md 子agent│
              └─────────────────────────────────────────┘
```

语义分工（保留二元结构，不强行合一）：

| | rules（蒸馏） | learnings（情景） |
|---|---|---|
| 粒度 | 泛化约定，一条规则一个约束 | 单次 session 的坑与解法，保留上下文 |
| 生效方式 | PromptBuilder 全量注入，常态生效 | recall 按需命中，标注出处 |
| 去重 | BatchedLLMJudge 强判定 | frontmatter 指纹（repo + 主题标签）弱去重 |
| 数量 | 少而精（max_rules 裁剪） | 多而全（按时间自然累积） |

## 4. 存储设计（已决策）

采用**workspace 优先 + 全局兜底**两级布局：

1. **workspace 级**：`<workspace_root>/.orchestratord/learnings/`——默认落这里。
   与 `workflow.rules.yaml` 同级存放、随仓库共享给团队、git 友好（与规则文件
   现行策略一致）。文件名 `YYYY-MM-DD-<slug>.md`，frontmatter 记
   `session_id / issue_id / repo / friction_score / tags / created_at`。
2. **全局级**：`~/.orchestratord/learnings/`——仅当 session 无 workspace 上下文
   （如临时 CLI 会话）或经验明确跨项目（用户显式标注 global）时落这里。

理由：daemon 按 workflow 运行，经验绝大多数与 workspace 绑定；放仓库内可随
代码评审共享，且 recall 索引范围天然限定为"当前项目 + 全局"，不会跨项目串味。

## 5. friction 评分设计

- 位置：`src/orchestratord/telemetry/friction.py`，纯函数 + 可注入信号源（可测）。
- 信号源（全部来自现有 telemetry，不新增埋点）：
  - 重试次数与重试原因（retry 链路）
  - capability 降级事件（degradation policy 触发）
  - 审批中断次数（permission 交互）
  - 失败/恢复事件（failure-recovery 触发）
  - 时长离群（相对同 app 历史分布）
- 评分：加权求和到 0–100，各信号权重集中在模块常量区，首版凭经验定标，
  按阶段 C 验收后校准。
- 触发：SESSION_COMPLETE 消费点（backend_runner.py:1438 处已有 session_id
  分发逻辑）追加 friction 计算与阈值判断；阈值经 workflow frontmatter
  `experience.friction_threshold` 可配，缺省值常量化。

## 6. 复用矩阵（新增 vs 沿用）

| 组件 | 处置 | 说明 |
|---|---|---|
| `telemetry/friction.py` | **新增** | 评分纯函数，信号源注入 |
| `skills/builtin/orchestratord-share-learnings/` | **新增** | 情景经验生成 skill（LLM prompt + 落盘） |
| `recall/` 子包（index/search/boost） | **新增** | BM25 + graph-boost；graph 复用 repo_tracker |
| `cli/recall.py` + `recall` 子命令 | **新增** | 基线文件需独立 commit（§12.2） |
| `agents/orchestratord-recall.md` | **新增** | 子 agent 定义 |
| `RuleEngine` / `BatchedLLMJudge` / `RuleStore` / `ExtractTracker` | **沿用** | 零修改；daemon 内新增调用方 |
| `_apply_review_rules`（interpret.py:1461） | **复活** | pass 桩 → 调 RuleEngine 跑本 session follow-up commits |
| `rules learn` CLI | **沿用** | 降级为人工补录入口，与自动触发共享幂等记账 |
| `PromptBuilder._inject_rules_reference_from_store` | **沿用** | 不变 |
| `repo_tracker` | **沿用** | recall 的 graph-boost 数据源 |
| events/emitter、usage_aggregates | **沿用** | friction 信号源 |

## 7. 落地阶段与验收

**阶段 A — 触发器 + 情景落盘（先让数据积累起来）**
- 内容：friction.py、SESSION_COMPLETE 挂钩、share-learnings skill、两级 learnings 落盘、
  `_apply_review_rules` 复活（daemon 自动蒸馏 follow-up commits）。
- 验收：构造重试/降级 fixture session → friction 分超阈值 → learnings 文件落盘且
  frontmatter 完整；含检视意见 follow-up 的 session 结束后 rules store 出现新规则、
  重复 session 不产生重复规则（幂等）。门禁：G0/G1 常规覆盖，friction 评分函数
  进进程内单测。

**阶段 B — 统一检索（recall CLI + 子 agent）**
- 内容：recall/ 子包（BM25 + graph-boost）、`recall` 子命令、recall 子 agent。
- 验收：阶段 A 积累的 learnings + 既有 rules + repo_tracker 索引三者均可命中；
  命中 rules 时输出标注"约定"；`expected_cli_subcommands.txt` 独立 commit 登记。

**阶段 C — 校准与注入优化**
- 内容：friction 阈值按真实数据校准；recall 命中 learnings 时 agent 自动引用的
  prompt 模板打磨；learnings 跨 workspace 重复检测（frontmatter 指纹）。
- 验收：FEATURE_GAP §7.4 端到端标准——`orchestratord recall <query>` 输出
  BM25 + graph-boost 结果且 recall 子 agent 在 session 中命中 learnings / rules。

## 8. 原开放问题的推荐方案（已定，2026-09-10）

1. **friction 阈值定标 → 绝对下限 + 滚动分位双门槛，蒸馏与落盘分档。**
   蒸馏进 rules（贵：BatchedLLMJudge 批判定）门槛为
   `score ≥ 60 且 ≥ P75(最近 20 个同 workspace session)`；learnings 落盘
   （便宜：一次 LLM 调用）门槛降为 `score ≥ 40 且 ≥ P50`。样本 <20 时退化为
   纯绝对下限。阶段 A 起每个 session 分数追加一行 JSONL 到 workspace
   reports 目录——阶段 C 校准直接拿真实分布回填，无需回溯。
2. **隐私脱敏 → 写入时双层脱敏，fail-closed。** 现状核查：仓库尚无
   utils/redact.py（仅 im_gateway token 脱敏），故新建独立模块
   `privacy/scrub.py`（正则规则集 + workspace 配置 allowlist；P1 session
   save 复用同一模块）。第一层生成端约束：share-learnings prompt 要求
   "转述而非摘抄"，凭据/内网 URL/客户名禁止出现；第二层写入端拦截：
   scrub 异常即丢弃该 learnings 文件并 WARN，绝不落未脱敏内容。
3. **中文分词 → 不引分词器；ASCII 词 + CJK 双字 n-gram 混合切分，
   质量押在排序融合。** learnings/rules 均为短文档，BM25 分词敏感度低；
   命中质量取决于 BM25 + frontmatter tags 精确匹配 + repo_tracker
   graph-boost + 时间衰减的 RRF 融合。share-learnings 生成时强制打
   3–5 个主题标签（免分词精确路径）。升级路径留死不开工：阶段 C 实测
   n-gram 精度不足再以 jieba（纯 Python）替换，检索层预留 tokenizer
   可替换点。
