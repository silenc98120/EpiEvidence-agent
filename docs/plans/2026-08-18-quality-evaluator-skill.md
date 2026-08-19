# Quality Evaluator Skill 设计记录

## 设计目标

第一版只做文献搜索推荐，不生成循证汇总报告。质量评价分为两个阶段：

1. **摘要初筛**：对检索返回的题录和摘要进行相关性、回答问题能力和摘要可见的方法学信息评价。
2. **全文精筛**：用户选择需要深入阅读的文献后，再下载全文并进行更完整的方法学和偏倚风险评价。

摘要阶段的结果必须称为 `preliminary_quality`，不能声称已经完成正式的偏倚风险评价。

## 批量评价策略

质量评价按研究类型分组批量执行，不为每篇文献启动一个独立子 Agent。

- 每组原则上包含 5～8 篇，最多 10 篇；摘要较长时动态缩小批次。
- RCT、队列、病例对照、横断面、系统评价等不能默认混在同一组。
- 一个文章级 Quality Evaluator 调用负责一组同类型文献。
- 模型必须为每篇文献分别返回相关性和研究类型质量评价，不负责计算最终综合分、筛选或排序。
- 所有分组完成后，先由核查 Agent 对评分依据逐项复核，再由程序重算分数并应用阈值。
- 仅对核查后综合分大于 3 分的候选文献执行集合级综合评估。
- 集合级 Agent 只评价每篇文献对 query 覆盖的贡献；最终排序仍由确定性程序完成。

批量评价可以减少 LLM 调用次数和限流压力，也能在同一研究类型内提供稳定的比较上下文。但绝不能只返回一个组级总分；每篇文献必须拥有独立、可通过 canonical UUID 关联的结构化评价。同组文章只提供判定尺度的比较背景，不能因为另一篇更好就降低某篇文章的绝对评分。

## 文献标识、数据库补全与消息边界

Graph state 和 `evidence_data` 只携带 PostgreSQL `articles.article_id` 的 canonical UUID，不复制摘要正文。评价节点收到 UUID 后按以下顺序准备输入：

```text
QualityEvaluationJob.article_ids
-> PostgreSQL repository 批量查询 articles
-> 校验返回 UUID 集合与请求完全一致
-> 形成 HydratedArticleBatch
-> 序列化为一次结构化 user message
-> 与对应 system prompt 一起调用模型
```

数据库补全后的文章 JSON 至少包含 `article_id`、`title`、`abstract`、作者、期刊、发表日期、DOI/PMID/PMCID、研究设计和撤稿状态。缺失摘要必须显式保留为缺失，不能用搜索片段或模型知识补写。

UUID 是各阶段唯一的 join key：不使用列表下标、题名、DOI 或 PMID 合并评价结果。每次输出都必须完整覆盖本次输入 UUID，不能遗漏、重复、替换或新增。Prompt、文章正文和模型原始响应不追加到全局 `MessagesState`；全局 state 只保存必要的结构化阶段产物和派生列表。

## 评价维度

### 相关性

相关性使用 0～10 分，并同时转换为分类：

- `high`：8～10
- `partial`：4～7
- `low`：0～3

评分应分别考虑用户问题中的 population、intervention/exposure、comparator、outcome 和 research direction，不能只根据题名相似度打分。

### 摘要阶段初步方法学质量

根据研究类型加载不同 Rubric，只评价摘要中明确可见的内容。摘要阶段不模拟完整的 RoB 2 或 JBI 全文评价；摘要没有报告的信息不能推断为研究没有执行。

摘要初筛条目的暂定状态和分值为：

```text
favorable       = +1
unclear         = -0.5
not_reported    = -1
not_applicable  = 不计入分母
```

每个条目都必须保存 abstract 中的原文依据和中文评分理由。`not_reported` 没有可引用的原文时，理由应明确写出“摘要未报告”，不能写成“研究没有做到”。

### RCT 摘要初筛规则

RCT 的摘要评价以 Cochrane RoB 2 为参考来源，但第一阶段只判断摘要是否提供了足够、合理、可解释的研究证据信号。分配隐藏、ITT、依从性、盲法和缺失数据处理等通常需要全文确认，不应因为摘要没有展开就声称完成正式 RoB 评价。

#### 研究设计识别

先确认摘要是否明确写出 randomized/random allocation，并确认人群、干预、对照和结局。随机化信息主要用于确认研究类型；不能仅凭“trial”或“controlled study”推断为 RCT。

#### 摘要层核心信号

第一版暂定使用以下五个核心评价点：

| `criterion_id` | 核心评价点 | 摘要阶段判定范围 |
|---|---|---|
| `rct_pico_clarity` | 人群、干预、对照和结局是否清楚 | 评价摘要是否能明确说明观察/干预人群、干预措施、比较方案和主要结局 |
| `rct_multicenter_setting` | 多中心与研究场景 | 多中心可作为研究场景和潜在代表性信号；单中心不自动判为低质量，也不能替代 PICO 和结果评价 |
| `rct_intervention_timing` | 干预和观察时间是否合理 | 结合干预机制、目标结局、干预持续时间和观察时间，给出摘要层的合理性判断；必须说明判断依据 |
| `rct_statistical_reporting` | 统计结果是否完整 | 根据结局类型判断是否报告合适的效应量、比较指标、样本量/分母、不确定性指标和关键时间点；不机械要求所有研究都报告 P 值 |
| `rct_recruitment_period` | 招募时间是否合理 | 结合招募周期、样本量、研究场景和结局特征判断；招募时间较长本身不构成质量问题，摘要证据不足时记 `unclear` |

招募时间是软指标。只有摘要同时提供招募周期、样本量、事件数或足够的研究场景信息时，模型才可以给出 `favorable`；不能仅凭常识断定某个疾病“应该很快招满”。

如果摘要在结果或结论中明确报告严重失访、明显不利因素或结果解释限制，必须将其写入对应条目的 `evidence_quote` 和 `reason`。当前不新增 `concern` 状态；显式但不足以形成正式全文判断的负面信息暂按 `unclear` 处理，并保留人工复核标记。

#### 相关性与回答问题能力

研究结果和结论是否直接回答用户 query 属于相关性和回答问题能力，不重复计入上述方法学信号：

- `relevance` 判断研究的人群、干预/暴露、对照、结局和研究方向是否匹配用户问题；
- `answerability` 判断摘要是否给出足以回答问题的结果、效应量、不确定性、时间点和结论；
- 结果与结论是否一致应单独记录。结论必须反映摘要报告的限制，不能只摘取有利结果。

#### 示例：HPV 疫苗 2 剂与 3 剂免疫原性 RCT

对 “Immunogenicity of 2 doses of HPV vaccine in younger adolescents vs 3 doses in young women” 这篇摘要，暂定记录为：

| 核心点 | 状态 | 分值 | 摘要依据 |
|---|---:|---:|---|
| PICO 清晰度 | `favorable` | `+1` | 明确给出女孩、年轻女性、2 剂/3 剂方案、HPV-16/18 抗体结局 |
| 多中心场景 | `favorable` | `+1` | `randomized, phase 3, postlicensure, multicenter... study of 830 Canadian females` |
| 干预时间合理性 | `favorable` | `+1` | 0、2、6 个月接种，并在 1、7、18、24、36 个月测量抗体 |
| 统计结果完整性 | `favorable` | `+1` | 报告 GMT 比值、95% CI、非劣效界值和多个时间点结果 |
| 招募时间合理性 | `unclear` | `-0.5` | 报告 2007 年 8 月至 2011 年 2 月招募，但摘要没有解释招募周期与样本量/研究场景的关系 |

五项核心信号等权时：

```text
(+1 +1 +1 +1 -0.5) / 5 = 0.7
preliminary_method_signal = 5 + 5 * 0.7 = 8.5 / 10
```

该分数只能表示“摘要提供了较完整的初步证据信号”，不能表述为正式的高质量或低偏倚结论。

该示例还需要记录一个相关性解释：主要比较是 9～13 岁女孩 2 剂与 16～26 岁年轻女性 3 剂，年龄和剂量同时不同；女孩 2 剂与女孩 3 剂的次要比较更接近单纯剂量问题。这个比较关系应进入 `reason` 和 `evidence_gaps`，而不是被多中心信息抵消。

### 观察性研究摘要初筛规则

队列和病例对照都属于观察性研究，都必须先明确比较结构，再评价匹配、混杂和结果报告。摘要阶段不要求完整复现 NOS，而是检查摘要是否提供了可解释的比较证据。

#### 暴露的广义定义

观察性研究中的“暴露”不是只指化学物质或环境因素，而是任何用于区分研究对象或人群组别、并可能与结局相关的因素、状态、条件或时间特征。它可以包括：

- 化学、生物、环境或职业因素；
- 行为、生活方式、治疗、药物或医疗服务使用；
- 政策、项目实施、接种资格或其他制度因素；
- 社会经济条件、地理区域、历史时期或特定时间窗口；
- 出生队列，以及其他可以被清晰操作化和分类的因素。

摘要初筛需要记录暴露的名称、暴露组与参考组、暴露发生或划分的时间窗口、测量/识别方法，以及暴露定义是否足以支持组间比较。出生队列只是暴露的一种形式，不需要单独建立新的研究类型。

#### 比较结构与匹配

比较结构至少包括：

- 并行组：暴露组与未暴露组，或病例组与对照组；
- 历史队列：当前出生队列/时期与更早时期或未暴露队列；
- 自身前后比较：同一对象干预前与干预后；
- 政策前后或时间序列比较。

自身前后和政策前后设计不能默认当作传统 cohort，应保留独立的设计标记。

“是否匹配”不是 cohort 或 case-control 的必选条件：

- Cohort 可以在研究设计阶段匹配暴露组与未暴露组，也可以在分析阶段使用倾向评分匹配、分层、回归或加权方法控制混杂。
- Case-control 通常先确定病例，再从相同基础人群中选择对照；可以在选择对照时匹配，也可以不匹配而使用统计调整。匹配后必须使用与匹配设计一致的分析方法。

因此，摘要阶段使用 `comparison_and_confounding_control`，评价比较组来源、组间可比性、匹配或其他混杂控制方法，而不是简单要求“必须匹配”。

#### Cohort 核心评价点

第一版暂定使用以下五项：

| `criterion_id` | 核心评价点 | 摘要阶段判定范围 |
|---|---|---|
| `cohort_pico_and_population` | 人群、结局和数据来源 | 研究对象、结局、数据库/登记系统和人群覆盖是否清楚 |
| `cohort_exposure_assignment` | 暴露定义与分配层级 | 暴露是个体实际接触、治疗、政策资格、出生队列或其他形式，定义是否清楚 |
| `cohort_comparator_confounding` | 比较队列与混杂控制 | 参考队列来源、匹配/调整变量和敏感性分析是否报告 |
| `cohort_outcome_and_time` | 结局确认与观察窗口 | 结局来源是否可靠，暴露到结局的观察时间是否匹配研究问题 |
| `cohort_statistical_reporting` | 统计结果完整性 | 是否报告效应量、置信区间、分母、模型和关键结果稳定性 |

#### 示例：英格兰 HPV 国家接种计划观察性队列

对 “The effects of the national HPV vaccination programme in England...” 这篇摘要，暂定记录为：

| 核心点 | 状态 | 分值 | 摘要依据 |
|---|---:|---:|---|
| 人群、结局和数据来源 | `favorable` | `+1` | “a population-based cancer registry”；研究对象为英格兰 20～64 岁居民，结局为 cervical cancer 和 CIN3 |
| 暴露定义与分配层级 | `favorable` | `+1` | 按疫苗提供年龄和出生队列划分 “three vaccinated cohorts”，与更早的 “not eligible” 队列比较 |
| 比较队列与混杂控制 | `favorable` | `+1` | 调整 cervical screening policy 和 historical events，并比较不同混杂调整模型 |
| 结局确认与观察窗口 | `favorable` | `+1` | 使用登记诊断数据，分析 2006～2019 年记录并报告 13.7 million-years follow-up |
| 统计结果完整性 | `favorable` | `+1` | 报告相对风险下降、95% CI、不同模型结果和预期减少病例数 |

需要同时写入 `evidence_gaps`：这里的暴露是按出生队列和疫苗提供机会定义的队列层级暴露，不等同于每个个体实际接种；“early effect”也不能自动外推为终身保护。

#### Case-control 核心评价点

第一版暂定使用以下五项：

| `criterion_id` | 核心评价点 | 摘要阶段判定范围 |
|---|---|---|
| `case_definition` | 病例定义 | 是否明确新发病例、诊断标准和病例数量 |
| `control_source` | 对照来源 | 对照是否来自与病例相同或可比的基础人群，是否明确没有目标疾病 |
| `case_control_comparability` | 比较组可比性与混杂控制 | 是否匹配，或是否使用分层、多因素模型、倾向评分等方法控制混杂 |
| `case_control_exposure` | 暴露确认与时间顺序 | 病例和对照是否用相同方法测量，暴露定义和发生时间是否清楚 |
| `case_control_statistics` | 统计结果完整性 | 是否报告 OR、95% CI、分母、趋势检验和关键亚组结果 |

#### 示例：HPV 与口咽癌病例对照研究

对 “Case-control study of human papillomavirus and oropharyngeal cancer” 这篇摘要，暂定记录为：

| 核心点 | 状态 | 分值 | 摘要依据 |
|---|---:|---:|---|
| 病例定义 | `favorable` | `+1` | “100 patients with newly diagnosed oropharyngeal cancer” |
| 对照来源 | `unclear` | `-0.5` | 报告 200 名 “control patients without cancer”，但摘要没有说明其是否来自与病例相同的基础人群 |
| 比较组可比性与混杂控制 | `unclear` | `-0.5` | 报告使用 “multivariate logistic-regression models”，但没有列出具体调整变量，也没有说明是否匹配 |
| 暴露确认与时间顺序 | `unclear` | `-0.5` | 报告口腔 HPV、血清阳性、性伴侣数量和烟酒使用，但摘要没有说明病例/对照的采样方法和暴露时间顺序 |
| 统计结果完整性 | `favorable` | `+1` | 报告 OR、95% CI、趋势 P 值以及烟酒使用分层结果 |

`hospital-based` 应保存为研究场景信息。它不自动等于低质量，但会影响结果对普通人群的可推广性。结论使用“associated”而不是“caused”，与病例对照设计的因果解释边界基本一致。

### 横断面研究摘要初筛规则

横断面研究必须先区分研究目的，再加载对应 Rubric：

- `cross_sectional_analytical`：主要回答暴露、特征与结局之间是否存在关联。
- `cross_sectional_prevalence`：主要估计特定人群在特定时间点或时间窗内的患病率、流行率或状态分布。

两者共同要求明确目标人群、抽样或招募方式、研究场景、暴露/特征、结局和测量时点。“暴露”继续使用广义定义。横断面设计通常不能确认暴露先于结局；摘要若使用因果措辞，必须记录时序不明和因果解释受限。

#### 分析型横断面核心评价点

| `criterion_id` | 核心评价点 | 摘要阶段判定范围 |
|---|---|---|
| `cross_sectional_population_sampling` | 人群与抽样 | 目标人群、来源、纳入方式和研究场景是否清楚，样本能否支持拟研究的关联 |
| `cross_sectional_exposure_measurement` | 暴露测量 | 暴露定义、分类或测量方法是否清楚且对各组一致 |
| `cross_sectional_outcome_measurement` | 结局测量 | 结局定义和测量方法是否清楚，是否使用合理、可靠的工具或标准 |
| `cross_sectional_confounding_control` | 混杂控制 | 是否识别重要混杂因素，并通过设计或多变量分析控制；匹配不是必需条件 |
| `cross_sectional_statistical_reporting` | 统计报告 | 是否报告适合关联问题的效应量、精确度指标和调整结果，如 OR、PR、回归系数及 95% CI |

#### 患病率横断面核心评价点

| `criterion_id` | 核心评价点 | 摘要阶段判定范围 |
|---|---|---|
| `prevalence_sampling_frame` | 抽样框与目标人群 | 抽样框是否覆盖目标人群，来源、地区和时间范围是否清楚 |
| `prevalence_recruitment_coverage` | 招募覆盖 | 招募方式、响应率或最终纳入人数是否足以判断选择性参与风险 |
| `prevalence_sample_size_precision` | 样本量与精度 | 样本量是否足以估计目标患病率，是否报告置信区间或其他精度指标 |
| `prevalence_condition_measurement` | 疾病或状态测量 | 病例定义、诊断标准或测量工具是否有效、可靠并一致应用 |
| `prevalence_statistics` | 患病率统计 | 是否报告分子、分母或样本基数、患病率和 95% CI；复杂抽样时是否说明加权或设计校正 |

不要强制患病率研究报告关联效应量，也不要强制分析型横断面研究报告患病率估计。摘要没有响应率等信息时按证据缺失处理，不推断实际招募质量。

### 证据综合研究摘要初筛规则

Review 和 Meta 分析属于同一个证据综合家族，但不是完全相同的研究类型。Meta 分析是定量合成方法，通常是系统评价的一个亚型；运行时按以下细类路由：

```text
evidence_synthesis
├── systematic_review
├── meta_analysis
├── qualitative_systematic_review
├── umbrella_review
└── narrative_review
```

#### 系统评价通用核心点

`systematic_review`、`meta_analysis`、`qualitative_systematic_review` 和 `umbrella_review` 共享五个核心维度：

| 核心点 | 摘要阶段判定范围 |
|---|---|
| 问题与纳入标准 | 研究问题、对象/现象、干预或暴露、对照、结局、研究类型及纳入排除标准是否可识别 |
| 检索覆盖 | 是否报告主要数据库、检索时间范围和补充检索，使覆盖范围具备基本可判断性 |
| 质量评价与资料提取 | 是否评价纳入研究的质量/偏倚风险，并说明资料提取或复核过程 |
| 合成方法 | 合成方式是否与研究问题和数据类型匹配，是否处理研究间差异 |
| 结论支持度 | 结论是否与合成结果一致，并考虑证据质量、异质性和重要局限 |

#### 各亚型附加规则

- `meta_analysis`：在通用五项外，评价效应量、95% CI 和模型，异质性指标及解释，适用时的敏感性/亚组/Meta 回归，以及纳入研究数量允许时的小样本效应或发表偏倚。确实不适用的发表偏倚条目使用 `not_applicable`，不能机械扣分。
- `qualitative_systematic_review`：评价系统检索、研究质量评价、编码/主题合成过程和解释可信度；不要求合并效应量、I² 或发表偏倚检验。
- `umbrella_review`：主要纳入单位是系统评价而非原始研究；除通用项外评价所纳入评价的质量、评价间研究重叠和冲突结果处理，不能把原始研究数量直接当作独立证据量。
- `narrative_review`：评价主题范围、证据选择透明度、论述平衡性和结论支持度。只有摘要明确报告系统检索、预设纳入标准和规范合成方法时，才归为系统评价。

PRISMA 2020 及其扩展是报告规范，只能辅助判断报告完整性。不能把“遵循 PRISMA”直接等同于低偏倚风险或高方法学质量，也不能因摘要未展示全部 PRISMA 条目就宣称正式质量不合格。

### 回答问题能力

检查摘要是否提供了与用户问题直接相关的研究对象、样本量、主要结局、效应方向、效应量、置信区间和结论。信息缺失时记录 `evidence_gaps`。

### 文献级分数计算

LLM 只返回相关性分数和逐条质量状态，不直接计算质量归一化分、加权综合分或排名。程序使用同一套纯函数计算，避免模型算术误差和不同节点采用不同公式。

单篇研究的适用质量条目先转换为固定分值：

```text
favorable       = +1
unclear         = -0.5
not_reported    = -1
not_applicable  = 从分子和分母排除
```

当前条目体系没有 `0` 分，也没有 `concern` 状态。适用条目的初步质量分计算为：

```text
quality_signal = sum(applicable criterion scores) / applicable criterion count
preliminary_quality_score = 5 + 5 * quality_signal
```

因此归一化结果落在 0～10，但中间每个条目仍只允许 `+1/-0.5/-1`。若没有任何适用条目，不能除以零或伪造质量分，应标记计算失败并进入人工/降级路径。

文章级 `composite_score` 也是确定性派生值。第一版权重建议为：

```text
相关性          50%
初步方法学质量  25%
回答问题能力    15%
期刊背景         5%
时效性           5%
```

权重必须配置化并在人工标注集上校准。核查 Agent 修正任一条目后，程序必须从修正后的底层分量完整重算 `quality_score` 和 `composite_score`，不能直接对已加权的 0～10 总分执行“原总分减原条目分再加新条目分”。

核查后的固定纳入阈值为：

```text
corrected_composite_score <= 3  -> 不纳入最终推荐候选
corrected_composite_score > 3   -> 保留，并携带所有核查扣分说明
```

核查失败本身不是删除条件。低分文章只有在重算后降到 3 分及以下才剔除；仍高于 3 分的文章继续进入集合级综合评估。

### 期刊背景与时效性

JCR、SCI、中科院分区、预警信息等由本地期刊数据表提供，不能让 LLM 猜测。期刊背景只能作为辅助信号，建议权重不超过 5%～10%，不能替代研究设计和结果评价。

期刊和时效性信号必须连同来源保存，供程序计算综合分和核查，不进入模型的自由猜测范围。

## 研究类型与评价工具

以下是计划采用的工具映射。正式实现前必须核对本地保存的说明文档、版本和授权限制。

| 研究类型 | 计划参考工具 | 摘要阶段重点 |
|---|---|---|
| 随机对照试验（RCT） | Cochrane RoB 2.0（全文参考） | 设计识别、PICO 清晰度、多中心场景、干预/观察时间、统计结果完整性、招募周期合理性 |
| 队列 | Newcastle-Ottawa Scale（NOS，全文参考） | 人群/数据来源、暴露定义、比较组与混杂、结局/观察窗口、统计完整性 |
| 病例对照 | Newcastle-Ottawa Scale（NOS，全文参考） | 病例定义、对照来源、组间可比性与混杂、暴露确认、统计完整性 |
| 分析型横断面 | AHRQ/JBI 横断面相关工具 | 人群与抽样、暴露测量、结局测量、混杂控制、关联统计 |
| 患病率横断面 | JBI 患病率研究工具 | 抽样框、招募覆盖、样本量/精度、状态测量、患病率统计 |
| 诊断准确性研究 | JBI 诊断试验质量工具 | 抽样、金标准、数据收集和分析 |
| 经济学评价 | JBI 经济学评价工具 | 问题界定、干预与对照、成本与效果、测量与分析 |
| 质性研究 | JBI 质性研究工具 | 研究问题、数据收集、分析、结论与推荐 |
| 系统评价 | JBI 系统评价工具 | 问题/纳入标准、检索、质量评价/提取、合成、结论支持度 |
| Meta-analysis | JBI 系统评价工具及定量合成规范 | 系统评价通用项、效应模型、异质性、敏感性/亚组、发表偏倚 |
| 定性系统评价 | JBI 系统评价/质性综合工具 | 系统检索、质量评价、编码/主题合成、解释可信度 |
| 伞状评价 | JBI umbrella review 相关工具 | 评价质量、重叠管理、冲突证据和集合结论 |
| 叙述性综述 | 项目自定义摘要 Rubric | 主题范围、证据选择透明度、论述平衡性、结论支持度 |

`PRISMA 2020` 是系统评价报告规范，不是独立的质量或偏倚风险评分工具。它可以用于检查报告完整性，但不能替代 RoB 或 JBI 等评价工具。

当前已收集的参考材料位于 `docs/qualityevaldocs/`，包括 RoB 2 手册、NOS 说明和 JBI 各类检查表。实现时不直接把完整原文复制到 Prompt，而是提炼为项目自己的结构化 Rubric，并保留工具名称和版本信息。

## 结构化输出契约

### 文章级初筛 `ArticleAssessmentBatch`

同一研究类型的一次模型调用返回一个 batch，但结果分成两部分：

```text
ArticleAssessmentBatch
├── task_id
├── group_id
├── study_type
├── article_ids: UUID[]
├── relevance_assessments[]
│   ├── article_id
│   ├── score: 0..10
│   ├── level: high | partial | low
│   ├── reason
│   ├── matched_dimensions[]
│   └── evidence_gaps[]
└── quality_assessments[]
    ├── article_id
    ├── study_type
    ├── criteria[]
    │   ├── criterion_id
    │   ├── status
    │   ├── score
    │   ├── evidence_quote
    │   ├── reason
    │   └── evidence_source
    ├── strengths[]
    ├── limitations[]
    ├── evidence_gaps[]
    └── confidence: 0..1
```

`relevance_assessments` 和 `quality_assessments` 必须分别完整覆盖 `article_ids`。两部分分开返回便于独立校验，随后由程序按 UUID 合并并计算单篇综合分；分开字段不代表要分两次调用。

### 核查输出

核查 Agent 必须返回每篇文章的原始总分和每个被核查项目的结果。即使全部通过，也要保留通过记录，不能只返回失败项：

```text
VerificationBatch
├── task_id
├── group_id
└── article_verifications[]
    ├── article_id: UUID
    ├── original_total_score
    ├── status: passed | failed
    └── item_verifications[]
        ├── item_id                 # relevance 或 criterion_id
        ├── original_status
        ├── original_score
        ├── corrected_status
        ├── corrected_score
        ├── evidence_quote_valid
        ├── reason_supported
        ├── issue
        └── verification_evidence
```

质量项目的 `corrected_status/corrected_score` 继续受固定映射约束，不能出现 0 或 `concern`。相关性修正使用其独立的 0～10 契约。核查输出是修正建议，不覆盖原始初筛记录；程序据此生成 `verified_assessment`。

### 集合级综合评估输出

综合评估 Agent 只接收 `corrected_composite_score > 3` 的候选集合，返回 query-conditioned 的覆盖评价和每篇文章的覆盖贡献分：

```text
PortfolioEvaluation
├── query_coverage
│   ├── required_concepts[]
│   ├── explicit_constraints[]
│   └── keyword_coverage[]
├── joint_query_coverage
├── coverage_gaps[]
├── redundancy_and_conflicts[]
├── article_contributions[]
│   ├── article_id: UUID
│   ├── contribution_role
│   ├── covered_concepts[]
│   ├── coverage_contribution_score
│   └── reason
└── recommendation
```

覆盖状态只能是 `direct`、`partial`、`indirect`、`absent` 或 `conflicting`。综合评估不能修改文章已经核查后的相关性、质量或综合分，也不能输出最终排序或最终推荐 UUID。

`coverage_contribution_score` 使用 0～10 分，只表示该文献对当前候选集合回答本次 query 的边际贡献：

- 8～10：直接联合覆盖主要概念，并提供集合中难以替代的关键人群、结局或比较证据；
- 6～7：直接覆盖主要问题，但与其他核心文献部分重复，或只补足一个重要维度；
- 3～5：只提供部分或间接覆盖，能够补充背景但不能单独回答主要问题；
- 0～2：与 query 关系很弱、几乎完全重复，或没有可识别的额外覆盖价值。

该分数必须附带 `covered_concepts`、贡献角色和基于 UUID 的理由。同一篇文献在不同 query 下可以得到不同贡献分；不能把它解释为文章固有质量，也不能仅因研究类型不同而奖励“形式多样性”。

## 三类 Agent 的职责边界

### 文章级 Quality Evaluator

输入同类型文章摘要和公共 Prompt，逐篇评价相关性与研究类型 Rubric。它必须引用摘要原文并说明理由，但不负责归一化质量分、综合分、阈值筛选或排序。

公共 XML Prompt 使用三个运行时插槽：

```text
__STUDY_DOMAIN__
__RUBRIC_RULES__
__OUTPUT_SCHEMA__
```

只维护一个公共 Prompt；不同研究类型通过固定注册表注入领域名称、Rubric 和对应 schema，不为每种类型复制完整 Prompt。

### 核查 Agent

核查 Agent 的目标不是重新做一遍独立质量评价，而是审计上一层评分是否有真实、充分且对应正确的依据。它逐项检查：

1. `evidence_quote` 是否确实出现在声明的 title、abstract 或显式元数据中。
2. `reason` 是否忠实解释该引文，而不是扩写或反转原意。
3. 引文是否足以支持原始状态；“提到统计”不等于“统计报告完整”。
4. `status` 与固定分值是否一致。
5. 摘要未报告的信息是否被错误打成 `favorable`。

核查通过时保留原值；核查失败时给出原始值、修正值、问题和核查证据。引用真实但不足以支持理由仍然属于失败。摘要完全未报告时应修正为 `not_reported = -1`；存在相关信息但不足以判断时为 `unclear = -0.5`。核查 Agent 不计算修正后的加权总分，也不自行决定删除文章。

### 集合级综合评估 Agent

该 Agent 评价的是保留文献作为一个集合能否满足用户 query，不重复进行单篇方法学评分。其第一版工作为：

1. 从用户 query、Intent 或 keywords 中提取 `required_concepts`。
2. 只把用户明确指定的时间、地区、研究类型、剂量、比较组、随访等作为 `explicit_constraints`。
3. 分别判断每个关键词的单独覆盖和主要概念在同一篇研究中的联合覆盖。
4. 识别与 query 直接相关的补充广度、证据空白、重复和冲突。
5. 给每篇候选文章一个 `coverage_contribution_score` 和集合角色。

例如 query 为“司美格鲁肽在老年肥胖人群中对心血管疾病的影响”，必需概念是司美格鲁肽、老年肥胖人群和心血管疾病。三篇文章分别提到三个关键词不等于联合问题得到回答；优先寻找同一篇研究同时覆盖三者。用户没有限制时间或研究类型时，不评价或惩罚时间覆盖、研究类型覆盖；地区、剂量、比较组等同理。

## 程序筛选与排序

小规模候选集的分数计算、筛选和排序全部由 Python 纯函数执行，不使用 Redis 的有序集合或服务端脚本。处理顺序为：

```text
原始 ArticleAssessmentBatch
-> UUID 合并 relevance + quality
-> 计算 original quality/composite score
-> 应用 VerificationBatch 修正
-> 从修正后的底层分量重算 corrected score
-> corrected_composite_score <= 3 的 UUID 进入 excluded_candidates
-> corrected_composite_score > 3 的 UUID 进入 retained_candidates
-> Portfolio Agent 计算 coverage_contribution_score
-> 程序稳定排序
```

排序键依次为：

1. `coverage_contribution_score` 降序；
2. 核查后的 `corrected_composite_score` 降序；
3. `publication_date` 降序，缺失日期排最后；
4. canonical UUID 升序，保证完全并列时结果可复现。

不同研究设计的质量分只能表示其各自 Rubric 下的摘要信号，不能解释成固定的跨设计证据等级。集合贡献分用于表达 query 覆盖价值，核查后综合分用于同贡献水平下的质量优先级，两者不得相互覆盖。

## State 与 Redis 设计

### State 保留原则

Reducer 字段只追加，不在原列表中删除元素。筛选通过普通派生字段表达：

```text
quality_evaluation_results   # reducer：保留所有原始 batch
verification_results         # reducer：保留所有核查结果
verified_assessments         # 普通字段：UUID -> 修正后单篇结果
excluded_candidates          # 普通字段：UUID + 分数 + 排除原因
retained_candidates          # 普通字段：核查后 > 3 的候选
portfolio_evaluation         # 普通字段：集合覆盖评价
ranked_candidates            # 普通字段：确定性排序结果
```

因此不需要从 `quality_evaluation_results` 或 `evaluated_evidence` 中物理删除低分文章，也不需要把删减后的列表再覆盖回 `evidence_data`。原始结果、核查结果、派生候选集同时保留，既便于溯源，也避免 reducer 合并时被旧数据重新加入。

`messages` 只承载主 Agent 的必要对话消息。每个评价节点在局部组装 system/user messages，调用完成后只把结构化输出写入 state；abstract 继续以 PostgreSQL 为事实来源。

### Redis 边界

Redis 仅作为短期编排缓存，例如任务状态、分组 UUID、节点进度和可恢复检查点。当前质量评价链路不使用 Redis：

- 计算质量分或综合分；
- 执行 `<= 3` 阈值筛选；
- 对候选文章排序；
- 保存摘要、Prompt 或完整模型响应；
- 充当 PostgreSQL 文献事实来源。

候选规模通常只有几十条，Python 内存处理更透明、易测且不需要维护 Redis 与 graph state 的双重真相。

## Summarizer 边界与最终输出

Summarizer 是 Quality Evaluator 下游的独立节点，不属于本 Skill。它接收排序后的候选列表、集合覆盖评价、检索过程指标和经验证的文章元数据，选择最终推荐 UUID 并组织用户可读结果。第一版不规定固定的推荐篇数，由模型根据 query 覆盖、文章互补性和证据缺口从程序提供的候选池中选择合适数量；不能恢复 `corrected_composite_score <= 3` 的文章。

推荐数量不设固定产品上限。第一版把全部通过阈值并完成程序排序的 `ranked_candidates` 提供给模型，由模型选择全部、部分或能够覆盖 query 的更小互补集合；没有任何文章适合推荐时，允许返回空集合并说明原因。如果真实运行中候选摘要超过模型上下文预算，后续应增加分批候选压缩与最终合并，而不是静默截断为固定的前 N 篇。

最终输出包含：

1. 用户 query 和识别出的关键词。
2. 实际搜索数据库、原始检索数量、去重后数量、分数分布和最终推荐数量。
3. 按程序排序呈现的推荐文献；每篇包括 title、authors、journal、摘要概括、推荐理由、核查后综合分及扣分/不足说明。
4. 最终推荐 UUID，供主 Agent 后续引用和全文下载流程使用。UUID 必须是候选池的子集，最终顺序由程序按照已确定的排序恢复。
5. 如存在可验证的全文资源，将全文链接放在该文献条目的最后一项。

全文链接只能来自 PostgreSQL `full_text_resources`，优先顺序为：可下载的开放获取 PDF，其次 XML/HTML，再其次其他开放获取全文页面。DOI 页面或 PubMed 题录页不能冒充全文链接；没有可验证全文资源时不输出链接。

## Skill 组成与动态加载

截至 2026-08-19，Quality Evaluator 代码结构如下：

```text
skills/quality_evaluator/
├── skill.md                         # 已有：公共工作流、约束和路由说明
├── schema.py                        # 已实现：初筛、核查和 Portfolio 严格模型
├── subgraph.py                      # 已实现：组级 Pipeline 与集合级 fan-in
├── rubric_registry.py               # 已实现：StudyType -> Rubric 固定映射
├── prompt_loader.py                 # 已实现：安全注入 Prompt 插槽
├── evaluator.py                     # 已实现：三个结构化 LLM 调用适配器
├── scoring.py                       # 已实现：重算、筛选和稳定排序纯函数
├── repository.py                    # 已实现：按 UUID 批量补全文献 JSON
└── prompts/
│   ├── core_prompt.md               # 已有：文章级初筛 XML Prompt
│   ├── verification_prompt.md       # 已有：逐项核查 XML Prompt
│   └── portfolio_evaluation_prompt.md # 已有：集合覆盖 XML Prompt
```

Markdown 文件不会被 LangGraph 自动执行。运行代码必须负责枚举校验、Rubric 注册、Prompt 读取与插槽替换、数据库补全、结构化 LLM 调用和 Pydantic 校验。不能把用户输入直接拼成文件路径。

## LangGraph 编排

完整目标流程为：

```text
检索结果标准化、去重并持久化 PostgreSQL
-> 按 study_type 对 canonical UUID 分组
-> Send(group, quality_evaluator)
-> repository 按 UUID hydrate article JSON
-> 组级 Pipeline 调用文章级 Evaluator 返回 ArticleAssessmentBatch
-> 同一组内调用核查 Agent 返回逐项修正建议
-> 程序重算 corrected quality/composite score
-> fan-in 保留原始评价、核查结果和派生候选集
-> retained_candidates 进入 Portfolio Evaluation Agent
-> 程序按四级排序键稳定排序
-> 下游 Summarizer 在有限候选池内选择最终 UUID，并生成用户输出
-> 主 Agent / 用户
```

没有搜索结果时直接返回空结果摘要；没有任何文章通过阈值时跳过 Portfolio Agent，由 Summarizer 说明没有满足最低分要求的推荐文献。单个评价分支失败时保留失败记录，不以虚构分数继续；可按配置重试或缩小批次。

## 数据校验、监测与评测闭环

### 数据校验

- 研究类型、criterion ID、质量状态、相关性等级、核查状态和覆盖状态全部使用枚举。
- 分数限制在约定范围，置信度限制在 0～1，状态与固定分值强一致。
- 每个输入 UUID 在各阶段恰好出现一次，batch 中两个评价列表必须与输入集合完全相等。
- `evidence_quote` 必须来自题名、摘要或显式元数据；`not_reported` 的引文固定为“摘要未报告”。
- 核查修正不覆盖原始记录，程序从修正底层项完整重算派生分数。
- LLM 结构化解析失败时有限重试；仍失败则标记节点失败，不伪造分数。
- 排序函数对相同输入必须产生完全相同的 UUID 顺序。

### 运行监测

至少记录：

- `task_id`、`search_run_id`、`group_id`、研究类型和文章数；
- 每个模型节点的成功数、失败数、延迟、Token、重试和估算成本；
- 结构化解析失败、UUID 不一致、越界分数和缺失字段数量；
- 核查通过率、失败率、被修正条目数和最常见失败 criterion；
- 核查前后质量分/综合分变化分布，以及因 `<= 3` 被排除的数量；
- retained 数量、各 required concept 的覆盖状态、联合覆盖缺口、重复和冲突数量；
- 排序稳定性和最终推荐数量。

日志不得记录完整摘要、完整 Prompt、模型原始响应、API Key 或其他敏感信息。证据原文保存在受控结构化结果中，不写入普通运行日志。

### 离线评测

建立按研究类型分层的小型人工标注集，至少评价：

- 研究类型和横断面/证据综合亚型路由准确率；
- 相关性、质量条目与人工判断的一致性；
- 同一文章跨批次评分稳定性；
- `unclear` 与 `not_reported` 的区分准确率；
- 引文存在性、理由支持度和核查 Agent 的纠错准确率；
- 核查前后误纳入/误排除率；
- 高相关文献排序一致性和完全并列时的确定性；
- required concept 单独覆盖、联合覆盖和 coverage gap 的人工一致性；
- Summarizer 最终 UUID 是否全部来自排序后保留集合，全文链接是否来自数据库资源表。

## 分阶段实施计划

截至 2026-08-19，Task 1～5 和 Task 7 已完成并通过全仓回归；Task 6 Summarizer 属于 Quality Evaluator 下游，本次未实现。以下任务保留为实现记录和后续维护检查表。

### Task 1：补齐核查、集合评价和派生分数 schema

文件：

- 修改 `skills/quality_evaluator/schema.py`
- 修改 `tests/test_quality_evaluator_schema.py`

步骤：

1. 先为核查 item/batch、修正后文章结果、集合覆盖、文章贡献和排序候选编写失败测试。
2. 运行 `pytest tests/test_quality_evaluator_schema.py -q`，确认新测试因模型不存在或校验缺失而失败。
3. 实现严格 Pydantic 模型、枚举、UUID 集合一致性和状态/分值映射校验。
4. 再次运行同一命令，预期全部通过。

### Task 2：实现数据库补全和 Prompt 装配

文件：

- 新建 `skills/quality_evaluator/repository.py`
- 新建 `skills/quality_evaluator/rubric_registry.py`
- 新建 `skills/quality_evaluator/prompt_loader.py`
- 新建 `tests/test_quality_evaluator_repository.py`
- 新建 `tests/test_quality_evaluator_prompt_loader.py`

步骤：

1. 为 UUID 批量查询、缺失/重复 UUID 拒绝、输出顺序稳定、未知 study type 拒绝和三个插槽完全替换编写测试。
2. 运行两个新测试文件，确认失败。
3. 使用 SQLAlchemy 查询 `ArticleORM`，从固定枚举注册表读取 Rubric；不接受用户路径。
4. 验证不存在未替换的 `__...__` 标记后再允许模型调用。
5. 运行两个新测试文件，预期全部通过。

### Task 3：实现评分重算、阈值筛选和稳定排序

文件：

- 新建 `skills/quality_evaluator/scoring.py`
- 新建 `tests/test_quality_evaluator_scoring.py`

步骤：

1. 为 `not_applicable` 分母排除、无适用条目、核查修正后完整重算、`3` 边界、`3.0001` 边界和四级排序键编写参数化测试。
2. 运行 `pytest tests/test_quality_evaluator_scoring.py -q`，确认失败。
3. 实现无副作用纯函数，不读写 Redis 或数据库。
4. 再次运行同一命令，预期全部通过且并列 UUID 顺序固定。

### Task 4：实现三个结构化模型调用适配器

文件：

- 新建 `skills/quality_evaluator/evaluator.py`
- 新建 `tests/test_quality_evaluator_runtime.py`

步骤：

1. 使用 fake structured LLM 覆盖文章级初筛、核查和集合评价三种调用。
2. 测试每次调用只产生一个 system message 和一个结构化 user message，并测试解析失败的有限重试。
3. 实现 Prompt 加载、schema 绑定、响应校验和错误对象；不在适配器内排序或筛选。
4. 运行 `pytest tests/test_quality_evaluator_runtime.py -q`，预期全部通过。

### Task 5：接入 LangGraph state 和子图

文件：

- 修改 `agent/state.py`
- 修改 `agent/graph.py`
- 实现 `skills/quality_evaluator/subgraph.py`
- 修改 `tests/test_agent_graph.py`
- 新建 `tests/test_quality_evaluator_subgraph.py`

步骤：

1. 先测试原始 reducer 结果不会被删除、派生候选字段可覆盖更新、空候选跳过 Portfolio Agent、部分分支失败可追溯。
2. 运行相关测试，确认当前 pending evaluator 流程不满足新契约。
3. 按目标编排连接 hydrate、evaluate、verify、recompute/filter、portfolio 和 sort 节点。
4. 确认摘要没有写入 `messages` 或 Redis，所有 join 使用 UUID。
5. 运行 `pytest tests/test_agent_graph.py tests/test_quality_evaluator_subgraph.py -q`，预期全部通过。

### Task 6：接入下游 Summarizer

文件：

- 在实际总结模块中新增 Summarizer；若当前仍无总结模块，先新建 `agent/summarizer.py`
- 修改 `agent/graph.py`
- 新建 `tests/test_evidence_summarizer.py`

步骤：

1. 测试最终 UUID 是 retained/ranked 候选池的子集，不会恢复被阈值排除的 UUID；测试模型选择全部候选、选择较小互补集合和返回空集合三种情况。
2. 测试检索过程统计和每篇展示字段完整。
3. 测试全文链接只来自 `full_text_resources`，并符合 PDF、XML/HTML、其他 OA 的优先级；DOI/PubMed 页不作为全文链接。
4. 实现总结节点并运行 `pytest tests/test_evidence_summarizer.py tests/test_agent_graph.py -q`，预期全部通过。

### Task 7：回归验证

依次运行：

```powershell
pytest tests/test_quality_evaluator_prompt.py tests/test_quality_verification_prompt.py tests/test_quality_portfolio_prompt.py -q
pytest tests/test_quality_evaluator_schema.py tests/test_quality_evaluator_repository.py tests/test_quality_evaluator_prompt_loader.py tests/test_quality_evaluator_scoring.py tests/test_quality_evaluator_runtime.py -q
pytest tests/test_quality_evaluator_subgraph.py tests/test_evidence_summarizer.py tests/test_agent_graph.py -q
pytest -q
```

前三组必须全部通过。全仓测试若受本机依赖或数据库服务阻塞，必须记录具体失败命令和环境原因，不能把“未运行”写成“通过”。

## 当前不实现

- 不在摘要阶段声称完成正式 RoB、NOS、JBI 或证据确定性评价。
- 不下载、解析全文，也不做全文方法学评价；全文质量评价是后续第二阶段。
- 不让 LLM 猜期刊分区、影响因子或预警信息。
- 不使用 Redis 计算、筛选或排序。
- 不使用 Reward Model 或强化学习优化排序。
- 不为每篇文章启动独立长期 Agent。
- 不把 Summarizer 的展示逻辑放进 Quality Evaluator Skill。

## 开放决策

以下事项不影响第一版摘要初筛主链路，实施前按给定默认值推进，并在获得正式工具授权或标注集后校准：

1. 评价工具版本：第一版按 `docs/qualityevaldocs/` 当前归档版本提炼 Rubric；后续确认 NOS、AHRQ、JBI 等具体版本与可复用范围后记录版本号。
2. 综合分权重：先采用本文 50/25/15/5/5 配置，积累人工标注后校准，不把权重硬编码进 Prompt。
3. Summarizer 数量：不设置固定推荐篇数，也不在第一版静默截断为固定的前 N 篇。模型在全部 `ranked_candidates` 中自行决定推荐全部、部分或空集合；未来候选量超过上下文预算时采用显式分批压缩流程。
4. 最终展示：后端保留结构化 `FinalEvidenceSummary` 作为内部契约，再转换为 Chat 页面中的一条普通简体中文助手消息；不让 LLM 直接生成 HTML，也不把 LLM 返回的 JSON 原样展示给用户。文献全文资源作为消息中的可点击链接或下载操作项呈现。
5. 全文阶段记录：未来设计时再确定正式领域评价、原始全文证据、工具版本和人工复核记录的数据模型；不提前复用摘要 schema 冒充全文评价。
