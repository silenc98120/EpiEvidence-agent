---
name: quality-evaluator
description: 对已标准化、去重并持久化的医学文献按 StudyType 执行摘要初筛、相关性与回答问题能力评价、逐项方法学信号核查、程序重算和 query-conditioned 集合覆盖评估；需要全文方法学或偏倚风险评价时仅说明摘要阶段边界。
---

# Quality Evaluator

## 目录

1. [任务边界](#任务边界)
2. [权威来源](#权威来源)
3. [执行流程](#执行流程)
4. [统一评分契约](#统一评分契约)
5. [研究类型目录](#研究类型目录)
6. [问题目录与映射](#问题目录与映射)
7. [数据库契约](#数据库契约)
8. [完成检查](#完成检查)

## 任务边界

本 Skill 只产生摘要阶段的 `preliminary_quality`：

- 判断文献与 `user_query` 的相关性；
- 独立判断摘要是否足以回答问题；
- 根据研究类型 Rubric 评价摘要中明确可见的方法学信号；
- 核查引文、理由和评分的一致性；
- 为程序重算、阈值筛选、集合覆盖和稳定排序提供结构化输入。

摘要没有报告的信息不能被当作研究没有执行。用户获得全文后，才进入正式的方法学或 risk of bias 评价；本 Skill 不替代 RoB 2、NOS、JBI 或其他全文工具。

## 权威来源

本文件规定执行边界和问题模型；字段、细则和计算逻辑由代码维护：

| 内容 | 权威来源 |
| --- | --- |
| 字段、枚举、输出值域和完整性 | [`schema.py`](schema.py) |
| `StudyType` 与 legacy `criterion_id` | [`rubric_registry.py`](rubric_registry.py) |
| 统一问题、示例和研究类型映射 | [`references/quality-question-catalog.md`](references/quality-question-catalog.md) |
| 文章级、核查级、集合级 Prompt | [`prompts/core_prompt.md`](prompts/core_prompt.md)、[`prompts/verification_prompt.md`](prompts/verification_prompt.md)、[`prompts/portfolio_evaluation_prompt.md`](prompts/portfolio_evaluation_prompt.md) |
| 分数重算、阈值和排序 | [`scoring.py`](scoring.py) |
| hydrate、重试和阶段编排 | [`evaluator.py`](evaluator.py)、[`subgraph.py`](subgraph.py) |

当需要具体研究类型的评价指标、评分等级、正反例或问题位置时，读取 `references/quality-question-catalog.md` 中对应章节；不要为节省几行而自行概括或改写问题。

## 执行流程

### 1. 校验任务并路由

输入必须符合 `QualityEvaluationJob`：`task_id`、`group_id`、已校验的 `study_type`，以及 1～10 个不重复的 PostgreSQL `articles.article_id` canonical UUID。一个批次只能包含一种研究类型。

先校验 `StudyType`，再从固定注册表获取 Rubric。不得使用用户输入构造文件路径、模块路径或 Rubric。研究类型的完整条目必须与注册表和 Schema 一致。

完成条件：任务元数据有效，且已经确定唯一研究类型、legacy `criterion_id` 集合和规范 `question_id` 映射。

### 2. 按 UUID hydrate

运行时按 UUID 从 PostgreSQL 读取 `EvaluationArticle`，形成 `HydratedArticleBatch`。数据库返回的 UUID 集合必须与任务集合完全相等；UUID 是所有阶段唯一的 join key。

文章至少包含 `article_id`、`title` 和可能为空的 `abstract`。作者、期刊、日期、DOI/PMID/PMCID、研究设计、发表状态和撤稿状态只能作为调用方显式提供的元数据使用。缺失摘要保持缺失，不用搜索片段、全文假设、外部检索或模型记忆补写。

完成条件：得到可序列化的 `HydratedArticleBatch`，没有遗漏、重复或替换文章。

### 3. 文章级初筛

每组建议 5～8 篇，最多 10 篇；摘要较长时缩小批次。一次调用可以评价同组文章，但必须逐篇完成以下顺序：

1. 根据 `user_query` 判断 population、intervention/exposure、comparator、outcome 和 research direction，输出 relevance `score`、`level` 和 `reason`。
2. 独立判断摘要是否给出样本量、主要结局、效应方向、效应量、不确定性、关键时间点和结论，输出 `answerability_score` 和 `answerability_reason`。
3. 按当前 Rubric 覆盖全部 legacy `criterion_id`，并按照问题目录提供的规范顺序评价每项。
4. 为每篇记录 `strengths`、`limitations`、`evidence_gaps` 和 `confidence`。

同组文章只提供判定尺度的比较背景，不能因另一篇更好而降低某篇的绝对评价。`ArticleAssessmentBatch` 的两个评价列表必须分别完整覆盖输入 UUID，每个 UUID 恰好出现一次。

### 4. 结构化校验与失败

文章级结果必须符合当前研究类型的 `ArticleAssessmentBatch`：

- `task_id`、`group_id`、`study_type` 与任务一致；
- `relevance_assessments` 和 `quality_assessments` 完整覆盖输入 UUID；
- relevance `level` 与分数一致；
- 每个质量状态与固定分值一致；
- 质量条目完整覆盖当前 Rubric 且无重复。

模型调用或 Schema 校验失败时按运行时重试策略重试。耗尽后返回 `evaluation_failed`、错误类型和原因，不生成分数，不进入重算。已处理或正在处理的批次返回 skipped，不重复计算。

完成条件：成功结果通过 Pydantic 校验；失败结果明确、可诊断且不含伪造评分。

### 5. 程序初算和逐项核查

模型不计算 `preliminary_quality`、综合分、筛选结果或排序。程序根据适用条目分值先计算原始质量与综合分。

随后使用 `verification_prompt.md` 输出符合 `VerificationBatch` 的核查结果，逐项检查：

- `evidence_quote` 是否真实存在于声明来源；
- `reason` 是否忠实解释原文；
- 原证据是否足以支持 relevance、answerability 和每个质量状态；
- `original_total_score` 是否保持原值。

核查通过时保留原状态和分值；核查失败时输出 `original_*`、`corrected_*`、`issue` 和 `verification_evidence`。核查 Agent 不计算修正后综合分、不决定纳入状态、不修改原始记录。

程序根据修正后的底层条目重算 `corrected_composite_score`：大于 `3` 保留，小于等于 `3` 排除。每个输入 UUID 必须恰好进入 retained 或 excluded 之一。

### 6. 集合覆盖和排序

仅将保留候选传入 `portfolio_evaluation_prompt.md`，输出符合 `PortfolioEvaluation` 的集合结果。先根据 query、Intent 和 SearchPlan 建立目标概念，再依次判断：

1. `keyword_coverage` 是否完整覆盖 `required_concepts`；
2. `constraint_coverage` 是否完整覆盖明确的 `explicit_constraints`；
3. 主要概念是否在同一研究中联合出现；
4. 关键缺口、重复、冲突和每篇文章的边际贡献。

覆盖状态只能为 `direct`、`partial`、`indirect`、`absent` 或 `conflicting`。除 `absent` 外的覆盖判断必须引用候选 UUID，不能引用候选集之外的文章。没有明确指定的时间、地区、研究类型、剂量、比较组或随访期限不是覆盖缺口。

程序按以下顺序稳定排序：

1. `coverage_contribution_score` 降序；
2. `corrected_composite_score` 降序；
3. 发表日期降序，缺失日期置后；
4. canonical UUID 升序。

没有保留候选时，portfolio 为 `null`，排序结果为空。

## 统一评分契约

### 原子问题与评分维度

评价前先读取 [`quality-question-catalog.md`](references/quality-question-catalog.md)。每个 `question_id` 必须是一个可用“满足 / 不足 / 未报告”独立回答的单一命题，并且只评价一个主要对象。题目同时包含人群、场景、抽样、方法、测量、统计或结论中的多个对象时，按目录拆成多个问题；一个子问题的证据不能替另一个子问题评分。

每个问题绑定唯一的 `dimension_id`。维度表示评价对象，不是额外分数：`D03` 是抽样与招募，`D02` 是研究设计与方法，`D04` 是研究人群与场景，`D05` 是暴露与干预，`D07` 是结局与测量，`D09` 是统计与精度，`D10` 是证据综合，`D11` 是结论与局限。完整维度表、问题定义和示例只维护在问题目录中。

执行时先按全局顺序建立当前研究类型的问题清单，再按 `study_type` 投影适用问题；不要求文章回答全目录。若一个 legacy `criterion_id` 覆盖多个规范问题，迁移前保留兼容输出，迁移时必须显式拆分并确定计分方式，不能复制旧分数。

### 质量条目

每个质量问题均使用以下四级状态。`not_applicable` 是适用性判定，不是“摘要没写”的宽松写法：

| 状态 | 分值 | 适用条件 |
| --- | ---: | --- |
| `favorable` | `+1` | 摘要明确、充分支持该问题 |
| `unclear` | `-0.5` | 有相关信息，但不足以确认是否满足 |
| `not_reported` | `-1` | 摘要完全没有该问题的信息，`evidence_quote` 固定为“摘要未报告” |
| `not_applicable` | `null` | 该问题确实不适用于当前研究；正例是非 Meta 研究不评价 Meta 异质性，反例是摘要缺失该信息 |

每个问题的具体评价指标、适用范围、`favorable` 正例、`unclear` 边界例和 `not_reported` 反例见 [`quality-question-catalog.md`](references/quality-question-catalog.md)。目录的统一 Rubric 还规定：正例必须支持该单一命题，反例必须说明为什么不能达到该级；`not_applicable` 必须有当前研究不适用的具体理由。`not_applicable` 不能用来掩盖摘要信息缺失。

### 相关性与回答问题能力

相关性和 answerability 均为 0～10 分。相关性等级固定为：`high=8～10`、`partial=4～7`、`low=0～3`。answerability 独立评分，不能由 relevance 分数代替。

只使用题名、摘要和显式元数据。引文逐字来自 `title`、`abstract` 或 `provided_metadata`；理由用中文说明证据如何支持判断或缺少什么信息。不得把关联写成因果，不得夸大统计显著性、临床意义、外推性或证据确定性。

## 研究类型目录

当前支持的研究类型及问题映射详见 [`quality-question-catalog.md`](references/quality-question-catalog.md)：

| 研究类型 | 代码值 | 评价重点 |
| --- | --- | --- |
| RCT | `randomized_controlled_trial` | PICO、研究场景、干预时序、统计报告、招募周期 |
| Cohort | `cohort` | 人群/数据源、暴露、比较与混杂、结局时间、统计报告 |
| Case-control | `case_control` | 病例定义、对照来源、可比性与混杂、暴露确认、统计报告 |
| 分析型横断面 | `cross_sectional_analytical` | 人群抽样、暴露测量、结局测量、混杂、关联统计 |
| 患病率横断面 | `cross_sectional_prevalence` | 抽样框、招募覆盖、样本精度、状态测量、患病率统计 |
| 系统评价 | `systematic_review` | 问题/纳入、检索、质量与提取、合成、结论支持 |
| Meta 分析 | `meta_analysis` | 系统评价通用项、效应量/模型、异质性、敏感性、发表偏倚 |
| 定性系统评价 | `qualitative_systematic_review` | 问题/检索、质量与提取、主题合成、解释可信度 |
| 伞状评价 | `umbrella_review` | 纳入评价质量、跨评价合成、重叠、冲突结果 |
| 叙述性综述 | `narrative_review` | 主题范围、证据选择、平衡讨论、结论支持 |

路由规则：

- 横断面先区分 analytical 与 prevalence；患病率研究不强制报告关联效应量，分析型横断面不强制报告患病率。
- Meta 分析是证据综合家族的定量合成亚型；伞状评价以系统评价而不是原始研究为主要纳入单位。
- 观察性研究的 exposure 可包括治疗、行为、环境、政策、社会人口学、出生队列和时间窗口；匹配不是 Cohort 或 Case-control 的必需条件。
- PRISMA 及其扩展只用于报告完整性，不能等同于低偏倚风险或正式质量结论。

## 问题目录与映射

统一问题目录位于 [`references/quality-question-catalog.md`](references/quality-question-catalog.md)，采用两级标识：

- `question_id`：未来数据库和 Schema 使用的稳定规范问题 ID；
- `legacy_criterion_id`：当前 Python Schema、注册表和 Prompt 使用的兼容 ID。

目录按以下规则组织：

1. 先定义共同评价维度，再定义可以跨研究类型复用的规范问题；
2. 只有评价对象、证据要求和评分语义完全一致的问题才合并；相近主题保留不同问题 ID，只共享维度；
3. 统一目录维护全局顺序，各研究类型只投影自己适用的问题，并维护研究类型内顺序；
4. 每个问题保留评分等级、正例、反例、证据要求和适用范围；
5. 当前 legacy 条目如果包含多个未来规范问题，必须在 Schema 迁移时显式拆分，不能静默复制一个分数到多个问题。

## 数据库契约

目标是使用统一的评价结果表，不为每种研究类型建立独立表：

```text
quality_evaluation_batches
  batch_id, task_id, study_type, rubric_version, status

quality_evaluation_items
  item_id, batch_id, article_id, question_id,
  status, score, evidence_quote, evidence_source, reason
```

推荐唯一键为 `article_id + question_id + rubric_version`。研究类型用于路由和审计，问题身份由 `question_id` 确定。迁移完成前，现有 `criterion_id`、研究类型专属 Schema 和 JSON 快照继续有效；不因目录文档变化而直接改变运行时输出。

## LangGraph 与持久化边界

LangGraph/worker 负责分组、切批、hydrate、调用、核查、程序重算、fan-in、幂等和持久化；本 Skill 文件不会自动执行。全局 state 只保存必要的结构化阶段产物，不追加摘要正文、完整 Prompt 或模型原始响应。

失败批次保留 `evaluation_failed`、错误类型和可诊断原因，阻止其进入重算和下游汇总。只有结构化校验成功并完成核查/重算的批次才可持久化为成功结果。

## 完成检查

- [ ] YAML frontmatter 包含有效的 `name` 和可区分触发范围的 `description`，正文目录可导航。
- [ ] 输入是标准化、去重、持久化的同类型 UUID 批次，hydrate 后 UUID 集合完全匹配。
- [ ] 当前 `study_type` 只使用注册表中的 Rubric 和 legacy `criterion_id`。
- [ ] 每篇 relevance、answerability 和全部质量条目均有可追溯证据；缺失信息使用正确状态。
- [ ] 文章级输出符合 `ArticleAssessmentBatch`，核查级输出符合 `VerificationBatch`，集合级输出符合 `PortfolioEvaluation`。
- [ ] 综合分、核查后重算、阈值筛选和排序均由程序完成；模型未输出派生决策。
- [ ] portfolio 只评价保留候选，未把未指定约束当作覆盖缺口，所有引用均属于候选集。
- [ ] 失败批次没有伪造分数；成功批次才允许持久化和进入下游汇总。
- [ ] 对外表述使用 `preliminary_quality`，未声称完成正式全文方法学或偏倚风险评价。
