# 质量评价问题目录

> 版本：`0.2`。本目录是 Rubric 化设计稿。当前运行代码仍以 `legacy_criterion_id` 作为输出字段；完成 Schema 迁移前，模型继续输出现有 `criterion_id`。

## 目录规则

### 问题卡片契约

目录中的每个问题都必须能够独立生成一个数据库评价项和一个 Prompt 判定项。最低字段如下：

| 字段 | 要求 |
| --- | --- |
| `question_id` | 全局唯一、稳定，不用标题或列表下标替代 |
| `question_text` | 一个可独立回答的命题，只评价一个对象 |
| `dimension_id` | 恰好一个主评分维度 |
| `applicable_study_types` | 明确适用范围；不适用时才允许 `not_applicable` |
| `global_order` | 在统一目录中的固定位置 |
| `study_type_order` | 投影到研究类型后的固定位置 |
| `scoring_rubric` | 四个状态的判断条件、正例和反例 |
| `evidence_requirement` | 可接受来源、最小证据和缺失证据处理 |
| `legacy_criterion_id` | 当前实现的兼容映射 |

### 原子性

一个 `question_id` 只评价一个可以用“是/否/信息不足”回答的命题，并且只对应一个主要评价对象。题目中出现“和”“以及”“同时”“是否……并……”时，先检查是否包含多个可独立判定的事实；若包含，必须拆分。

以下内容不能放在同一个原子问题中：

- 人群、数据源、抽样框和研究场景；
- 病例定义和病例数量；
- 暴露定义和暴露测量质量；
- 结局定义和结局测量质量；
- 比较组来源和混杂控制；
- 效应量、分母、置信区间和统计模型；
- 检索数据库、检索时段和补充检索；
- 结论与局限。

当摘要只报告复合命题的一部分时，只影响对应的子问题；不能用一个子问题的证据替另一个子问题评分。

### 评分维度

`dimension_id` 表示“评价什么方面”，不是文章字段，也不自动产生分数。每个问题只能有一个主维度；问题需要引用其他信息时，将其写进证据要求，不改变主维度。

| `dimension_id` | 维度 | 核心对象 | 典型问题 |
| --- | --- | --- | --- |
| `D01` | 研究问题与范围 | 研究要回答什么、覆盖什么 | 研究问题是否明确；主题边界是否明确 |
| `D02` | 研究设计与方法 | 研究采用什么设计和方法组织证据 | 设计是否可识别；分配/比较结构是否可识别 |
| `D03` | 抽样与招募 | 研究对象如何进入研究、抽样框是否覆盖目标人群 | 抽样框是否明确；招募方式是否明确；响应率是否报告 |
| `D04` | 研究人群与场景 | 谁被研究、在哪里研究、数据来自哪里 | 目标人群是否明确；研究场景是否明确；数据源是否明确 |
| `D05` | 暴露与干预 | 接受了什么暴露/干预以及如何定义 | 暴露定义是否明确；干预方案是否明确；剂量/时间是否明确 |
| `D06` | 比较组与混杂 | 与谁比较、组间可比性如何、混杂如何处理 | 对照来源是否可比；混杂控制是否报告 |
| `D07` | 结局与测量 | 测量什么以及用什么标准测量 | 结局定义是否明确；测量工具是否明确；测量时点是否明确 |
| `D08` | 随访与时间 | 暴露/干预和结局的时间关系 | 随访窗口是否明确；招募周期是否可判断 |
| `D09` | 统计与精度 | 结果如何量化及不确定性如何表达 | 效应量是否报告；精度指标是否报告；模型是否明确 |
| `D10` | 证据综合 | 多项研究/评价如何检索、评价和合成 | 检索来源是否报告；合成方法是否明确；重叠是否处理 |
| `D11` | 结论与局限 | 结果是否支持结论、限制是否诚实呈现 | 结论是否受结果支持；局限是否报告；冲突是否解释 |

### 四级评分

所有质量问题共用以下 Rubric。正例和反例只说明证据形态；实际评分必须引用当前文章的原文。

| 状态 | 分值 | 原子判定标准 | 正例 | 反例 |
| --- | ---: | --- | --- | --- |
| `favorable` | `+1` | 摘要明确、充分回答该单一问题 | “community-dwelling adults aged 40–65 years in England” 对应人群问题 | 只有“patients”不能支持人群范围问题 |
| `unclear` | `-0.5` | 摘要出现相关证据，但不足以确认该单一问题 | “multivariable model” 出现，但没有调整变量，不能确认混杂控制 | 摘要完全没提混杂时不应使用该级 |
| `not_reported` | `-1` | 摘要没有该问题的证据 | 没有抽样来源时，抽样框问题为该级 | 摘要明确写出随机抽样却记为该级 |
| `not_applicable` | `null` | 该问题对当前研究确实不适用 | 非 Meta 分析不评价 Meta 异质性 | 仅因摘要缺失而使用该级 |

`not_reported` 的 `evidence_quote` 固定为“摘要未报告”；`unclear` 必须引用实际存在的不足证据；`favorable` 必须引用足以支持该单一命题的最短完整原句。所有状态都必须有 `reason` 和 `evidence_source`。

每个具体问题都要有四种状态的例子：`favorable` 的支持性正例、`unclear` 的不足性边界例、`not_reported` 的缺失信息反例，以及 `not_applicable` 的适用性正例和误用反例。目录表的“适用研究类型”定义 `not_applicable` 的边界；题目适用但摘要没写时必须使用 `not_reported`。

## 统一问题目录

下表每行都是一个原子问题。`dimension_id` 是该问题的唯一主评分维度。问题中的“是否清楚”只评价摘要报告是否足以判断，不推断全文执行情况。

| 全局顺序 | `question_id` | `dimension_id` | 原子评价问题 | 适用研究类型 | `favorable` 正例 | `unclear` 反例/边界例 | `not_reported` 反例 |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | `Q-COM-001` | `D01` | 研究目的是否明确 | 原始研究、所有综述 | 明确说明研究要完成的任务 | 只说“探讨影响”，但任务边界不清 | 没有目的 |
| 2 | `Q-COM-015` | `D01` | 研究问题范围或纳入范围是否明确 | 所有综述；适用时的原始研究 | 明确对象、比较因素、结局和研究设计范围 | 只说“相关研究”，缺少范围要素 | 没有范围信息 |
| 3 | `Q-COM-002` | `D04` | 目标人群或研究单位是否明确 | 有人群/单位的研究 | 给出年龄、地区、来源或分析单位 | 只写“patients”或“participants” | 没有可识别对象 |
| 4 | `Q-COM-003` | `D04` | 研究场景是否明确 | 原始研究 | 指明医院、社区、学校、国家或多中心场景 | 写“multicenter”但未说明场景 | 没有场景 |
| 5 | `Q-COM-004` | `D04` | 数据源或登记系统是否明确 | 使用数据库/登记数据的研究 | 指明 cancer registry、claims database 等 | 说“database study”但未说明数据库 | 没有数据源信息 |
| 6 | `Q-COM-005` | `D07` | 主要结局或研究现象是否明确 | 原始研究、证据综合 | 明确主要结局或研究现象 | 提到结局但主次或名称不清 | 没有结局/现象 |
| 7 | `Q-COM-006` | `D11` | 结论是否得到结果支持 | 有结果和结论的研究 | 结论方向与结果一致且不过度外推 | 有结果但结论比结果更强 | 没有结论或结果依据 |
| 8 | `Q-COM-007` | `D11` | 重要局限是否报告 | 摘要有局限/结论的研究 | 说明关键局限和解释边界 | 有明显限制但只笼统说“有局限” | 没有局限信息 |
| 9 | `Q-COM-008` | `D10` | 主要检索数据库或来源是否报告 | 系统、Meta、定性系统、伞状评价 | 列出主要数据库/来源 | 只说“multiple databases” | 没有检索来源 |
| 10 | `Q-COM-009` | `D10` | 检索时间范围是否报告 | 系统、Meta、定性系统、伞状评价 | 给出起止日期或截止日期 | 只给一个模糊年份 | 没有时间范围 |
| 11 | `Q-COM-010` | `D10` | 补充检索方法是否报告 | 系统、Meta、定性系统、伞状评价 | 说明参考文献、灰色文献或注册平台来源 | 提到“hand search”但范围不清 | 没有补充检索信息 |
| 12 | `Q-COM-011` | `D10` | 纳入研究的质量或偏倚评价是否报告 | 系统、Meta、定性系统评价 | 说明评价工具或评价过程 | 只说“quality assessed” | 没有质量评价信息 |
| 13 | `Q-COM-012` | `D10` | 资料提取方法是否报告 | 系统、Meta、定性系统评价 | 说明提取表、提取变量或提取流程 | 只说“data extracted”但方法不清 | 没有提取信息 |
| 14 | `Q-COM-016` | `D10` | 资料复核或一致性流程是否报告 | 系统、Meta、定性系统评价 | 说明双人复核、仲裁或一致性检查 | 提到复核但没有流程 | 没有复核信息 |
| 15 | `Q-COM-013` | `D10` | 合成方法是否明确 | 系统、Meta、定性系统、伞状评价 | 说明定量、叙述或主题合成方法 | 说“pooled”但方法不明 | 没有合成方法 |
| 16 | `Q-COM-014` | `D10` | 研究间差异或异质性是否被处理 | 系统、Meta、伞状评价 | 说明异质性评估或解释方式 | 提到异质性但没有处理方式 | 没有相关信息 |
| 17 | `Q-DSN-001` | `D02` | 研究设计是否明确 | 原始研究 | 明确写 randomized trial、cohort 等 | 只写“observational study”但不能区分设计 | 没有设计信息 |
| 18 | `Q-DSN-002` | `D02` | 研究分组或比较结构是否明确 | 原始比较研究 | 明确暴露/未暴露、病例/对照或干预/对照 | 有组名但关系不清 | 没有比较结构 |
| 19 | `Q-DSN-003` | `D02` | 分配方式是否明确 | 有分配过程的原始研究，尤其 RCT | 明确 randomized allocation 或其他分配方式 | 写“trial”但没有分配信息 | 没有分配信息 |
| 20 | `Q-SMP-001` | `D03` | 抽样框是否明确 | 患病率横断面、部分原始研究 | 说明目标人群对应的抽样框 | 说明来源但不能判断覆盖范围 | 没有抽样框 |
| 21 | `Q-SMP-002` | `D03` | 抽样或招募方式是否明确 | 原始研究、患病率横断面 | 明确随机、连续、便利或其他招募方式 | 只说“participants enrolled” | 没有抽样/招募方式 |
| 22 | `Q-SMP-003` | `D03` | 响应率是否报告 | 患病率横断面；适用时的调查研究 | 给出受邀人数和响应人数/响应率 | 给出受邀人数但没有响应结果 | 没有响应率信息 |
| 23 | `Q-SMP-005` | `D03` | 最终纳入覆盖是否可判断 | 患病率横断面；适用时的调查研究 | 同时给出目标样本、最终样本和排除流程 | 只有最终样本，不能判断覆盖 | 没有纳入覆盖信息 |
| 24 | `Q-SMP-004` | `D03` | 样本量是否报告 | 所有原始研究；证据综合按纳入研究数 | 给出人数、事件数或纳入研究数 | 只给比例，基数不清 | 没有样本量 |
| 25 | `Q-POP-001` | `D04` | 目标人群范围是否明确 | 所有涉及人群的研究 | 年龄、地区、健康状态和纳入范围清楚 | 只给宽泛人群标签 | 没有范围 |
| 26 | `Q-POP-002` | `D04` | 研究地点或地理范围是否明确 | 地理范围影响解释的研究 | 指明国家、地区或多中心地点 | 只说“population-based” | 没有地点 |
| 27 | `Q-EXP-001` | `D05` | 暴露或干预名称是否明确 | RCT、Cohort、Case-control、分析型横断面 | 清楚命名药物、行为、政策或治疗 | 只写“exposure”或“intervention” | 没有暴露/干预 |
| 28 | `Q-EXP-002` | `D05` | 暴露或干预的定义是否明确 | RCT、Cohort、Case-control、分析型横断面 | 说明剂量、资格或操作定义 | 有名称但定义规则不清 | 没有定义 |
| 29 | `Q-EXP-003` | `D05` | 暴露分类或分组规则是否明确 | RCT、Cohort、Case-control、分析型横断面 | 说明类别、阈值或分组规则 | 有定义但分类方法不清 | 没有分类 |
| 30 | `Q-EXP-004` | `D05` | 暴露测量方法是否明确 | Cohort、Case-control、分析型横断面 | 说明测量、记录或识别方法 | 提到测量但方法不清 | 没有测量信息 |
| 31 | `Q-EXP-005` | `D05` | 干预实施方式是否明确 | RCT | 说明给药、接种或干预流程 | 说明干预名称但流程不清 | 没有实施信息 |
| 32 | `Q-CMP-001` | `D06` | 比较组或参考组来源是否明确 | RCT、Cohort、Case-control、分析型横断面 | 说明对照/参考组如何选取 | 有组名但来源不明 | 没有比较组来源 |
| 33 | `Q-CMP-002` | `D06` | 组间可比性策略是否报告 | Cohort、Case-control、分析型横断面 | 说明平衡、匹配或可比性处理 | 说“matched”但未说明变量 | 没有可比性策略 |
| 34 | `Q-CMP-003` | `D06` | 混杂因素识别是否报告 | Cohort、Case-control、分析型横断面 | 列出关键混杂因素 | 只说“confounding considered” | 没有混杂因素信息 |
| 35 | `Q-CMP-004` | `D06` | 混杂控制方法是否报告 | Cohort、Case-control、分析型横断面 | 说明回归、分层、加权或敏感性分析 | 只说“adjusted analysis” | 没有控制方法 |
| 36 | `Q-OUT-001` | `D07` | 结局定义或诊断标准是否明确 | 所有原始研究 | 给出病例定义、量表或诊断标准 | 有结局名称但标准不清 | 没有定义/标准 |
| 37 | `Q-OUT-002` | `D07` | 结局测量工具或数据来源是否明确 | 所有原始研究 | 说明实验、量表、登记或临床测量来源 | 只说“assessed” | 没有工具/来源 |
| 38 | `Q-OUT-003` | `D07` | 结局测量时点是否明确 | RCT、Cohort、横断面及时间敏感研究 | 给出随访月数或调查时间点 | 给出“follow-up”但无时点 | 没有时点 |
| 39 | `Q-TIM-001` | `D08` | 暴露/干预先于结局的时间关系是否可判断 | RCT、Cohort、Case-control；横断面记录时序限制 | 明确暴露、干预和结局的先后 | 有时间信息但顺序不清 | 没有时间关系 |
| 40 | `Q-TIM-002` | `D08` | 随访窗口是否与研究问题匹配 | RCT、Cohort | 给出随访期并能联系目标结局 | 有随访期但无法判断是否足够 | 没有随访窗口 |
| 41 | `Q-TIM-003` | `D08` | 招募周期是否可结合研究背景判断 | RCT；其他研究仅在 Rubric 明确要求时 | 给出周期及判断所需研究背景 | 只给日期，不能判断合理性 | 没有招募周期 |
| 42 | `Q-STA-001` | `D09` | 适合研究问题的效应量是否报告 | RCT、Cohort、Case-control、分析型横断面、Meta | 报告 RR/OR/HR/PR/回归系数等适合指标 | 只给 P 值或百分比，缺效应量 | 没有结果量化 |
| 43 | `Q-STA-002` | `D09` | 结果的分母、事件数或样本基数是否报告 | 需要基数解释的原始研究和 Meta | 给出分母、事件数或纳入研究数 | 有比例但基数不清 | 没有基数 |
| 44 | `Q-STA-003` | `D09` | 不确定性或精度指标是否报告 | RCT、Cohort、Case-control、横断面、Meta | 报告 95% CI、SE 或精度界限 | 只报告点估计或 P 值 | 没有精度指标 |
| 45 | `Q-STA-004` | `D09` | 统计模型是否明确 | 有模型分析的研究 | 指明 logistic、Cox、随机效应等模型 | 只说“model used” | 没有模型信息 |
| 46 | `Q-STA-005` | `D09` | 调整方案是否明确 | 需要调整的观察性研究或 Meta | 明确调整变量/协变量及其进入分析方式 | 只说“adjusted” | 没有调整方案 |
| 47 | `Q-PRE-001` | `D09` | 患病率估计值是否报告 | 患病率横断面 | 给出 prevalence/proportion | 只给疾病人数，未给估计值 | 没有患病率 |
| 48 | `Q-PRE-002` | `D09` | 患病率估计的精度是否报告 | 患病率横断面 | 给出 95% CI 或设计精度 | 给出患病率但无精度 | 没有精度 |
| 49 | `Q-MTA-001` | `D09` | Meta 分析的合并效应量是否报告 | Meta 分析 | 给出 pooled effect | 只说存在总体效应，未给数值 | 没有合并效应量 |
| 50 | `Q-MTA-005` | `D09` | Meta 分析的统计模型是否明确 | Meta 分析 | 说明固定或随机效应模型 | 给出合并效应但模型不清 | 没有模型信息 |
| 51 | `Q-MTA-002` | `D09` | Meta 分析异质性指标是否报告 | Meta 分析 | 报告 I²、tau² 或 Q | 只说存在异质性，无指标 | 没有异质性指标 |
| 52 | `Q-MTA-003` | `D09` | Meta 分析敏感性分析是否在适用时报告 | Meta 分析 | 研究数量允许且报告敏感性分析及限制 | 提到敏感性分析但方法不清 | 研究数量允许但未报告 |
| 53 | `Q-MTA-006` | `D09` | Meta 分析亚组或 Meta 回归是否在适用时报告 | Meta 分析 | 研究数量和问题允许且报告分析 | 提到亚组但方法或解释不清 | 没有相关信息 |
| 54 | `Q-MTA-004` | `D09` | Meta 分析小样本效应是否在适用时评价 | Meta 分析 | 研究数量允许且报告小样本效应检验/图形 | 提到检验但方法或数量不清 | 研究数量不足或未报告 |
| 55 | `Q-MTA-007` | `D09` | Meta 分析发表偏倚是否在适用时评价 | Meta 分析 | 研究数量允许且报告发表偏倚评价 | 提到发表偏倚但方法不清 | 研究数量不足或未报告 |
| 56 | `Q-NAR-001` | `D01` | 叙述性综述的主题范围是否明确 | 叙述性综述 | 明确主题、对象和边界 | 主题宽泛，目标对象不清 | 没有范围 |
| 57 | `Q-NAR-002` | `D10` | 叙述性综述的证据选择来源是否透明 | 叙述性综述 | 说明来源和选择原则 | 提到检索但没有选择逻辑 | 没有来源/选择信息 |
| 58 | `Q-NAR-003` | `D11` | 叙述性综述是否平衡呈现不同方向证据 | 叙述性综述 | 呈现支持与不支持证据 | 只呈现单一方向 | 没有可判断讨论 |
| 59 | `Q-NAR-004` | `D11` | 叙述性综述的重要局限是否报告 | 叙述性综述 | 明确说明关键局限和外推边界 | 只笼统说“有局限” | 没有局限信息 |
| 60 | `Q-QUA-001` | `D10` | 定性系统评价的编码或主题生成过程是否明确 | 定性系统评价 | 说明编码、主题生成和研究者复核 | 提到主题分析但过程不清 | 没有合成过程 |
| 61 | `Q-QUA-002` | `D11` | 定性系统评价的解释是否得到资料支持 | 定性系统评价 | 解释与资料和反例一致 | 方向明确但证据链不完整 | 没有解释支持信息 |
| 62 | `Q-QUA-003` | `D11` | 定性系统评价的解释局限是否报告 | 定性系统评价 | 说明资料局限、反思性或外推边界 | 只给解释，不说明限制 | 没有局限信息 |
| 63 | `Q-UMB-001` | `D10` | 伞状评价纳入的系统评价质量是否评价 | 伞状评价 | 明确使用工具评价纳入综述 | 提到质量评价但对象/工具不清 | 没有评价信息 |
| 64 | `Q-UMB-002` | `D10` | 伞状评价中的原始研究重叠是否处理 | 伞状评价 | 报告重叠识别、校正或解释 | 提到重叠但没有处理 | 没有重叠信息 |
| 65 | `Q-UMB-003` | `D11` | 伞状评价中的冲突结果是否解释 | 伞状评价 | 结合人群、结局、方法或质量解释冲突 | 只说结果不一致 | 没有冲突处理 |

## 研究类型映射

映射只列出该研究类型实际计分的问题。`legacy_criterion_id` 是当前代码兼容名；未来迁移时，一个 legacy 复合条目必须拆成多个 `question_id`，不能复制一个旧分数。

### RCT

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `rct_pico_clarity` | `Q-COM-002`, `Q-EXP-001`, `Q-EXP-002`, `Q-CMP-001`, `Q-OUT-001` |
| 2 | `rct_multicenter_setting` | `Q-COM-003` |
| 3 | `rct_intervention_timing` | `Q-EXP-001`, `Q-EXP-002`, `Q-EXP-005`, `Q-OUT-003`, `Q-TIM-002` |
| 4 | `rct_statistical_reporting` | `Q-STA-001`, `Q-STA-002`, `Q-STA-003`, `Q-STA-004` |
| 5 | `rct_recruitment_period` | `Q-SMP-004`, `Q-TIM-003` |

### Cohort

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `cohort_pico_and_population` | `Q-COM-002`, `Q-COM-003`, `Q-COM-004`, `Q-OUT-001` |
| 2 | `cohort_exposure_assignment` | `Q-EXP-001`, `Q-EXP-002`, `Q-EXP-003`, `Q-EXP-004` |
| 3 | `cohort_comparator_confounding` | `Q-CMP-001`, `Q-CMP-002`, `Q-CMP-003`, `Q-CMP-004` |
| 4 | `cohort_outcome_and_time` | `Q-OUT-001`, `Q-OUT-002`, `Q-OUT-003`, `Q-TIM-001`, `Q-TIM-002` |
| 5 | `cohort_statistical_reporting` | `Q-STA-001`, `Q-STA-002`, `Q-STA-003`, `Q-STA-004`, `Q-STA-005` |

### Case-control

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `case_definition` | `Q-OUT-001`, `Q-STA-002` |
| 2 | `control_source` | `Q-CMP-001` |
| 3 | `case_control_comparability` | `Q-CMP-002`, `Q-CMP-003`, `Q-CMP-004` |
| 4 | `case_control_exposure` | `Q-EXP-001`, `Q-EXP-002`, `Q-EXP-003`, `Q-EXP-004`, `Q-TIM-001` |
| 5 | `case_control_statistics` | `Q-STA-001`, `Q-STA-002`, `Q-STA-003`, `Q-STA-004`, `Q-STA-005` |

### 分析型横断面

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `cross_sectional_population_sampling` | `Q-COM-002`, `Q-COM-003`, `Q-SMP-001`, `Q-SMP-002` |
| 2 | `cross_sectional_exposure_measurement` | `Q-EXP-001`, `Q-EXP-002`, `Q-EXP-003`, `Q-EXP-004` |
| 3 | `cross_sectional_outcome_measurement` | `Q-OUT-001`, `Q-OUT-002`, `Q-OUT-003` |
| 4 | `cross_sectional_confounding_control` | `Q-CMP-002`, `Q-CMP-003`, `Q-CMP-004` |
| 5 | `cross_sectional_statistical_reporting` | `Q-STA-001`, `Q-STA-002`, `Q-STA-003`, `Q-STA-004`, `Q-STA-005` |

### 患病率横断面

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `prevalence_sampling_frame` | `Q-COM-002`, `Q-POP-002`, `Q-SMP-001` |
| 2 | `prevalence_recruitment_coverage` | `Q-SMP-002`, `Q-SMP-003`, `Q-SMP-005` |
| 3 | `prevalence_sample_size_precision` | `Q-SMP-004`, `Q-PRE-002` |
| 4 | `prevalence_condition_measurement` | `Q-OUT-001`, `Q-OUT-002` |
| 5 | `prevalence_statistics` | `Q-PRE-001`, `Q-PRE-002`, `Q-STA-002` |

### 系统评价

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `systematic_question_inclusion` | `Q-COM-001`, `Q-COM-015` |
| 2 | `systematic_search_coverage` | `Q-COM-008`, `Q-COM-009`, `Q-COM-010` |
| 3 | `systematic_appraisal_extraction` | `Q-COM-011`, `Q-COM-012`, `Q-COM-016` |
| 4 | `systematic_synthesis_method` | `Q-COM-013`, `Q-COM-014` |
| 5 | `systematic_conclusion_support` | `Q-COM-006`, `Q-COM-007` |

### Meta 分析

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `meta_question_inclusion` | `Q-COM-001`, `Q-COM-015` |
| 2 | `meta_search_coverage` | `Q-COM-008`, `Q-COM-009`, `Q-COM-010` |
| 3 | `meta_appraisal_extraction` | `Q-COM-011`, `Q-COM-012`, `Q-COM-016` |
| 4 | `meta_synthesis_method` | `Q-COM-013` |
| 5 | `meta_conclusion_support` | `Q-COM-006`, `Q-COM-007` |
| 6 | `meta_effect_measure_model` | `Q-MTA-001`, `Q-MTA-005` |
| 7 | `meta_heterogeneity` | `Q-MTA-002` |
| 8 | `meta_sensitivity_subgroup` | `Q-MTA-003`, `Q-MTA-006` |
| 9 | `meta_publication_bias` | `Q-MTA-004`, `Q-MTA-007` |

### 定性系统评价

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `qualitative_systematic_question_inclusion` | `Q-COM-001`, `Q-COM-015` |
| 2 | `qualitative_systematic_search_coverage` | `Q-COM-008`, `Q-COM-009`, `Q-COM-010` |
| 3 | `qualitative_systematic_appraisal_extraction` | `Q-COM-011`, `Q-COM-012`, `Q-COM-016` |
| 4 | `qualitative_systematic_thematic_synthesis` | `Q-QUA-001` |
| 5 | `qualitative_systematic_conclusion_support` | `Q-QUA-002`, `Q-QUA-003` |

### 伞状评价

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `umbrella_question_inclusion` | `Q-COM-001`, `Q-COM-015` |
| 2 | `umbrella_search_coverage` | `Q-COM-008`, `Q-COM-009`, `Q-COM-010` |
| 3 | `umbrella_review_appraisal` | `Q-UMB-001` |
| 4 | `umbrella_synthesis_method` | `Q-COM-013`, `Q-COM-014` |
| 5 | `umbrella_conclusion_support` | `Q-COM-006`, `Q-COM-007` |
| 6 | `umbrella_overlap_management` | `Q-UMB-002` |
| 7 | `umbrella_conflicting_evidence` | `Q-UMB-003` |

### 叙述性综述

叙述性综述保留独立问题，不强制套用系统评价的检索完整性问题。

| 顺序 | `legacy_criterion_id` | 规范 `question_id` |
| ---: | --- | --- |
| 1 | `narrative_topic_scope` | `Q-NAR-001` |
| 2 | `narrative_evidence_selection` | `Q-NAR-002` |
| 3 | `narrative_balanced_discussion` | `Q-NAR-003` |
| 4 | `narrative_conclusion_support` | `Q-COM-006`, `Q-COM-007`, `Q-NAR-004` |

## 迁移约束

- 当前运行链路继续输出 `criterion_id`；目录变化不会自动改变现有 Schema、Prompt 或分数。
- Schema 迁移后，`criteria[]` 改为按 `question_id` 输出；程序按研究类型映射选择问题，不要求文章回答全目录。
- 数据库统一使用 `quality_evaluation_items`，唯一键建议为 `article_id + question_id + rubric_version`。
- 同一 legacy 条目映射多个规范问题时，必须在迁移设计中明确拆分后的计分权重；不得把一个复合旧分数复制到多个问题。
- `dimension_id` 只负责分类和分析；是否计入质量分由问题的评分配置决定。
