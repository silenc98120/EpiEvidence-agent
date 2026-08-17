# 多源文献检索设计

## 设计范围

本设计将 EpiEvidence 从 PubMed、Europe PMC 双来源检索扩展为可控的多源检索流程。第一版新增 Semantic Scholar、PMC、bioRxiv 和 medRxiv，同时保留数据来源追踪信息，并统一转换为与具体平台无关的数据契约。

当前 MVP 仍然根据题录和摘要推荐文献。系统可以发现 PMC 全文链接，但全文下载、正文解析和正式方法学质量评价不在本阶段范围内。

## 已确认的产品决策

- PubMed 和 Europe PMC 是默认主检索源。
- Semantic Scholar 是补充检索源，仅在主检索证据不足时调用。
- PMC 关键词检索也是补充检索源；当用户要求开放获取或全文，或者主检索证据不足时调用。
- PMC OA 全文链接解析是独立操作，在跨库去重后执行。
- 只有用户允许纳入预印本时，才检索 bioRxiv 和 medRxiv。
- 每个数据源的单次检索最多返回 100 篇文献。
- 默认按照相关性排序；用户明确要求近期证据时，按照发表时间倒序排列。
- 用户明确指定的时间范围属于硬性过滤条件。用户没有提出时间要求时，系统不得擅自添加时间限制。
- 正常情况下由 LLM 判断当前证据是否充分。数量阈值只用于监测和 LLM 失败时的降级判断。

## 预印本策略

数据源选择发生在构建 `SearchPlan` 之前。

```text
用户明确要求最新、前沿或预印本证据
  -> include_preprints

用户明确要求仅纳入同行评议或正式发表文献
  -> peer_reviewed_only

用户没有表达偏好
  -> 暂停流程并追问用户
```

每个研究任务只追问一次。补充检索默认继承已经确认的策略，除非用户明确修改要求。

第一版只区分两种文献范围策略：

- `peer_reviewed_only`
- `include_preprints`

`Review`、`Clinical Trial`、`Randomized Controlled Trial` 等具体出版类型继续作为来源元数据保存，并为未来的 `SearchPlan` 过滤条件预留空间。它们不与预印本策略混用。

## 数据源选择

`SourceRouter` 根据结构化意图结果和确定性规则选择数据源。LLM 可以识别用户是否要求指定来源、开放获取、全文、近期证据或预印本，但不能自由生成数据源列表。

```text
IntentRecognitionResult
-> SourceRouter
   -> SourceSelectionDecision
      -> 主检索源
      -> 允许使用的补充检索源
      -> 预印本策略
      -> 是否需要追问
      -> 选择原因
-> SearchPlanBuilder
```

数据源策略如下：

| 数据源 | 角色 | 启用条件 |
|---|---|---|
| PubMed | 主检索源 | 所有研究型检索默认启用 |
| Europe PMC | 主检索源 | 所有研究型检索默认启用 |
| Semantic Scholar | 补充检索源 | LLM 判断主检索证据不足 |
| PMC Searcher | 补充检索源 | 用户要求开放获取或全文，或者主检索证据不足 |
| PMC OA Resolver | 全文资源解析器 | 去重后，对保留的 PMCID 调用 |
| bioRxiv | 预印本来源 | 仅限 `include_preprints` |
| medRxiv | 预印本来源 | 仅限 `include_preprints` |

LLM 的推荐不能覆盖已经确认的预印本策略。路由节点必须校验推荐来源是否属于 `allowed_supplementary_sources`。

## SearchPlan 扩展

与具体数据库无关的检索计划需要增加文献范围、排序方式和时间范围：

```python
class PublicationScope(StrEnum):
    PEER_REVIEWED_ONLY = "peer_reviewed_only"
    INCLUDE_PREPRINTS = "include_preprints"


class SearchSortMode(StrEnum):
    RELEVANCE = "relevance"
    NEWEST = "newest"


class PublicationDateRange(BaseModel):
    start_date: date | None = None
    end_date: date | None = None


class SourceSelectionDecision(BaseModel):
    publication_scope: PublicationScope | None
    primary_sources: list[SearchSource]
    allowed_supplementary_sources: list[SearchSource]
    requires_clarification: bool
    clarification_question: str | None
    reasons: list[str]
```

`SearchPlan` 保存 `publication_scope`、`sort_mode`、可选时间范围、选中的数据源，以及固定的 `max_results_per_source=100`。Pydantic 必须拒绝超过 100 的值。

各平台的 Compiler 将支持的约束转换为平台原生语法或请求参数。如果某个平台无法表达某项约束，系统必须在统一化之后再次校验并过滤记录，不能静默忽略该约束。

## 各数据源连接器

### PubMed

PubMed 继续作为摘要和题录来源。ESearch 支持相关性排序、日期排序和日期参数，EFetch 返回 XML 题录与摘要。PubMed Searcher 不得把 PMCID 直接解释为已经存在可下载全文。

### Europe PMC

Europe PMC 继续作为默认题录和摘要来源，并保留平台报告的开放获取状态与全文资源线索。它同时作为 bioRxiv 和 medRxiv 的关键词发现索引。

### Semantic Scholar

连接器使用官方 Academic Graph API。正常补充发现使用 `/graph/v1/paper/search`。该接口按照相关性返回结果，并支持普通文本、日期或年份、出版类型、研究领域和开放 PDF 等过滤参数。

由于相关性检索接口不支持布尔表达式，Semantic Scholar Compiler 从每个 `SearchPlan` 概念组选择代表性英文词，生成简化的普通文本查询。

当用户明确要求按照时间倒序检索时，可以使用 `/paper/search/bulk` 并传入 `sort=publicationDate:desc`。该接口支持布尔匹配，但不提供相关性排序。

API Key 是可选配置。没有 API Key 时仍允许调用，但连接器必须正确处理更严格的限流。

### bioRxiv 和 medRxiv

bioRxiv 官方 API 支持按日期范围、类别或 DOI 查询，但不支持一般关键词检索。因此逻辑连接器采用以下流程：

```text
通过 Europe PMC 进行带预印本限制的关键词发现
-> 识别候选 DOI 和预印本服务器
-> 有界调用 bioRxiv 官方 API，按 DOI 校验并补充信息
-> 生成平台专属结果模型
-> 转换为 EvidenceRecord
```

这两个连接器返回的统一记录必须具有 `peer_review_status="preprint"`，并明确写入 `preprint_server`。官方 API 返回的版本号和首次发布信息保留在平台专属数据中。

如果无法通过官方 API 确认服务器，系统不能把该记录改写成同行评议文献。

### PMC

PMC 拆分为两个职责明确的组件：

- `PMCSearcher` 通过 NCBI ESearch 的 `db=pmc` 进行补充关键词检索，再有界获取题录和摘要。
- `PMCOAResolver` 接收 PMCID 并调用 PMC OA Web Service，返回平台报告的 license、撤稿标记和可用资源链接与格式。

OA Service 不是关键词搜索 API。系统只在去重后对保留的候选文献调用它，避免对几百篇重复文献逐条解析全文链接。

PubMed 和 PMC 虽然共享 NCBI 基础设施，但在系统中仍然属于不同数据源。

## 统一数据契约

每个连接器保留经过 Pydantic 校验的平台专属响应模型，再由独立 Normalizer 转换成统一数据契约。

```python
class PeerReviewStatus(StrEnum):
    PEER_REVIEWED = "peer_reviewed"
    PREPRINT = "preprint"
    UNKNOWN = "unknown"


class EvidenceRecord(BaseModel):
    source: SearchSource
    source_record_id: str
    search_run_id: str
    doi: str | None
    pmid: str | None
    pmcid: str | None
    title: str
    abstract: str | None
    authors: list[EvidenceAuthor]
    first_author: str | None
    journal_title: str | None
    journal_abbreviation: str | None
    publication_date: str | None
    publication_year: int | None
    publication_types: list[str]
    study_design: str | None
    peer_review_status: PeerReviewStatus
    preprint_server: str | None
    is_open_access: bool | None
    has_full_text: bool
    full_text_resources: list[FullTextResourceCandidate]
    citation_count: int | None
    landing_url: str | None
    retrieved_at: datetime
```

缺失的单值字段使用 `None`，缺失的集合使用 `[]`。来源报告的出版类型、后续推断的研究设计、同行评议状态和开放获取状态必须保持为不同概念。OA 状态不能作为文献质量信号。

外层 `UnifiedSearchResult` 包含以下字段：

- 数据源和 `search_run_id`；
- 结束状态；
- 命中数量和实际召回数量；
- 分页数量和重试次数；
- 无效记录数量和调用耗时；
- 统一记录列表；
- 经过脱敏处理的错误信息。

统一状态值为：

- `success_with_results`
- `success_empty`
- `partial_success`
- `failed`

平台原始 payload 不放入 `EvidenceRecord`。搜索执行层在 Graph fan-in 之前，将它们写入 `source_records.raw_payload`。

## 排序与时间规则

- 每个数据源最多返回 100 篇文献。
- 默认排序方式为 `relevance`。
- 平台支持时，明确的日期范围直接作为请求约束；统一化之后还要在本地再次校验。
- 用户明确要求最新或近期证据时，使用 `newest` 排序。
- 用户只说“最新”但没有提供时间范围时，系统应当追问具体时间窗口，而不是自行假设。
- 不同数据库的相关性排名和分数不能直接横向比较。
- 跨库最终排序必须在统一化、去重和摘要相关性评价之后进行。

## LLM 证据充分性评价

正常情况下，是否扩大检索范围由 LLM 根据语义覆盖情况判断，而不是只根据数量阈值决定。

```python
class SufficiencyDecision(BaseModel):
    is_sufficient: bool
    confidence: float
    coverage_gaps: list[str]
    recommended_sources: list[SearchSource]
    recommended_search_adjustments: list[str]
    short_reason: str
```

评价器接收以下上下文：

- 用户原始 query；
- 结构化 intent；
- `SearchPlan`；
- 各数据源的命中数量；
- 去重后的有效摘要数量；
- 初步相关性评价的高、中、低分布；
- 高相关候选文献的精简结构化摘要；
- 当前允许使用的补充数据源。

评价器不接收所有平台原始 payload，也不接收几百篇完整摘要。

`SufficiencyDecision` 必须通过 Pydantic 校验。LLM 推荐的数据源必须与允许使用的来源集合求交集，且 `peer_reviewed_only` 始终排除 bioRxiv 和 medRxiv。

配置项 `min_unique_abstracts=20` 和 `min_high_relevance=5` 作为诊断指标写入监测记录。只有在 LLM 调用失败，或者在有限重试后仍无法获得有效结构化输出时，才使用这两个阈值进行降级路由。

## LangGraph 流程

```text
intent_node
-> source_router_node
   -> 预印本偏好未确定：interrupt，向用户追问
   -> 偏好已确定：继续
-> mesh_normalizer_node
-> search_plan_node
-> compile_queries_node
-> 主检索 fan-out：PubMed + Europe PMC
-> 平台结果统一化和持久化
-> 主检索 fan-in 和去重
-> 初步摘要相关性评价
-> LLM sufficiency_router
   -> 证据充分：进入最终排序
   -> 证据不足：对允许的补充来源 fan-out
-> 补充结果统一化和持久化
-> 第二次 fan-in、去重和最终排序
```

`EvidenceState` 只保存结构化决策、`search_run_id`、`article_id` 和小体积检索摘要，不保存各平台的原始结果集合。

`EvidenceState` 直接继承 LangGraph 已有的消息 reducer，不再把 `messages` 重新声明为 `list[str]`。

fan-in 使用基于 ID 的幂等 reducer，不能继续使用简单的 `operator.add`。节点重试时不得重复添加文献或检索摘要。

## 数据持久化

- 每调用一次数据源，就创建一条 `search_runs` 记录。
- 平台原始记录写入 `source_records.raw_payload`。
- 统一化和去重后的文献写入或关联到 `articles`。
- 发现的 OA 和可下载资源写入 `full_text_resources`。
- 持久化层使用固定的 SQLAlchemy Repository 方法和事务，不使用 text-to-SQL。

在保存预印本文献之前，`articles` 表需要增加同行评议状态和预印本来源字段。具体数据库迁移步骤放入后续实现计划。

## 失败处理

- 单个数据源失败时，不取消已经成功的同级搜索，整批任务标记为部分成功。
- PubMed 和 Europe PMC 两个主检索源都失败时，停止检索并返回可重试的任务错误。
- 单条记录格式无效时，跳过该记录并累计 `invalid_record_count`，不能因此让整个有效页面失败。
- 网络超时、HTTP 429 和 HTTP 5xx 使用有限次数的指数退避重试。
- 运行日志不得写入 API Key、完整医学检索式、摘要或原始响应正文。
- LLM 充分性判断失败时，使用已记录的数量阈值降级，并明确记录本次降级。

## 监测、数据校验与评测

运行监测需要记录：

- 数据源和 `search_run_id`；
- 选中的预印本和来源策略；
- 检索结束状态和各类数量；
- 调用耗时、分页数量和重试次数；
- 无效记录、成功标准化和去重数量；
- LLM Token 使用量；
- 充分性判断置信度；
- 是否触发降级判断。

Pydantic 负责校验每个检索请求、平台响应边界、统一文献记录、`SearchPlan` 扩展字段和 LLM 充分性判断。

验证分为六层：

1. 对 `SourceRouter` 和各 Compiler 编写单元测试，覆盖预印本、数据源、排序和日期规则。
2. 对每个 Connector 编写 mock HTTP 测试，覆盖分页、限流、部分失败和 100 篇硬上限。
3. 编写平台专属模型到 `EvidenceRecord` 的数据契约测试。
4. 使用 PostgreSQL 编写 Repository 事务和去重集成测试。
5. 编写 LangGraph 分支测试，覆盖追问 interrupt、主检索与补充检索 fan-out、单源失败、充分性路由、降级行为和 fan-in 幂等性。
6. 建立小型离线评测集，包含证据充分和证据不足的研究问题，用于监测不必要扩检和遗漏覆盖缺口。

## 实现顺序

1. 增加共享枚举、`SearchPlan` 扩展字段、统一文献契约和数据库字段迁移。
2. 实现 `SourceRouter` 和追问状态。
3. 为现有 PubMed 和 Europe PMC 结果增加 Normalizer。
4. 实现 Semantic Scholar Connector 和 Compiler。
5. 实现 PMC Searcher 和 PMC OA Resolver。
6. 实现 bioRxiv/medRxiv 发现与官方 API 补充流程。
7. 增加 Repository 和各平台持久化服务。
8. 实现主检索 fan-out/fan-in 和确定性去重。
9. 实现初步摘要相关性评价与 LLM Sufficiency Evaluator。
10. 实现条件补充检索 fan-out 和最终合并排序。

## 本阶段不实现

- 将全文文件下载到本地。
- 解析 JATS XML、PDF、EPUB 或 HTML 全文。
- 正式的偏倚风险和证据质量评价。
- 证据综合与学术报告生成。
- 基于 Reward Model 或强化学习的排序。
