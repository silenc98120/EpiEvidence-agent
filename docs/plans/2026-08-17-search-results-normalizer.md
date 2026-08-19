# 多来源检索结果标准化实现计划

## 目标

把 PubMed、Europe PMC、Semantic Scholar、PMC、bioRxiv 和 medRxiv 的平台专属文章模型转换为统一 `EvidenceRecord`，并把平台检索结果转换为 `UnifiedSearchResult`。

本模块不负责跨来源去重、PostgreSQL 写入、Redis 缓存、研究类型推断、分组或质量评价。

## 统一模型

文件：`app/core/search_results_normalizer.py`

定义：

- `UnifiedSearchStatus`
- `PeerReviewStatus`
- `UnifiedFullTextFormat`
- `EvidenceAuthor`
- `FullTextResourceCandidate`
- `EvidenceRecord`
- `UnifiedSearchError`
- `UnifiedSearchResult`
- `SearchResultsNormalizer`

字段规则：

- DOI 去除 `https://doi.org/` 和 `doi:` 前缀并转为小写。
- PMID 只允许数字；PMCID 统一为大写 `PMC` 加数字。
- 标题和摘要折叠多余空白，但保留摘要段落换行。
- 缺失单值字段使用 `None`，集合使用空列表。
- normalizer 不根据摘要猜测研究设计。
- bioRxiv/medRxiv 固定为 `preprint`；其他来源默认 `unknown`，不因为来源名称直接声称经过同行评议。
- TGZ 在统一资源格式中映射为 `other`，原始格式保留在 `original_style`。
- 来源 `article_id` 只用于追踪。跨来源 canonical ID 由后续去重器生成。

## 来源适配

为六个来源分别实现私有适配函数：

1. PubMed：作者、题录、摘要和标识符；不声称可下载全文。
2. Europe PMC：保留 OA 状态、引用次数和所有全文候选资源。
3. Semantic Scholar：保留开放 PDF、引用次数和平台作者 ID。
4. PMC：保留全文页面状态；下载资格仍为未知，等待 OA Resolver。
5. bioRxiv/medRxiv：保留服务器、版本线索、JATS XML 和正式发表 DOI。

## 批量结果

`SearchResultsNormalizer.normalize_result(result)` 根据结果模型类型分派适配器，逐条转换并累计新增的无效记录数。

结束状态规则：

- 平台失败继续为 `failed`。
- 平台成功但全部为空继续为 `success_empty`。
- 有单条转换失败时为 `partial_success`。
- 不允许把未知平台对象静默转换。

## 验证

文件：`tests/test_search_results_normalizer.py`

源代码完成后，覆盖：

- 六个来源映射到统一结构；
- DOI、PMID、PMCID 和文本标准化；
- OA/full-text 语义不被扩大；
- 预印本状态；
- 批量部分失败和未知类型拒绝；
- 完整项目测试无回归。
