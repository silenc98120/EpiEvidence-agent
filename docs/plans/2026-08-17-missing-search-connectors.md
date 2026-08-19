# 缺失文献检索连接器实现计划

## 目标

在现有 PubMed、Europe PMC 检索器基础上，实现 Semantic Scholar、PMC、bioRxiv 和 medRxiv 四个逻辑检索器。所有来源单次最多返回 100 篇，并沿用异步 HTTP、Pydantic 边界校验、Loguru 监测和有限重试模式。

Google Scholar 不在本计划内。它没有适合当前自动化工作流的稳定官方检索 API，项目现有设计已明确暂缓。

## 任务一：Semantic Scholar

文件：`app/tools/article_search/semantic_scholar.py`

1. 定义请求、作者、开放全文、文章、错误和检索结果模型。
2. 相关性模式调用 `/graph/v1/paper/search`；日期倒序模式调用 `/graph/v1/paper/search/bulk`。
3. 请求固定的元数据字段，最多返回 100 条；可选 API Key 仅写入 `x-api-key` 请求头，不进入日志。
4. 标准化 DOI、PMID、PMCID、作者、期刊、摘要、出版类型、引用次数和开放 PDF。
5. 对 429、5xx、超时和传输错误进行有限指数退避。
6. 在 `tests/test_semantic_scholar_searcher.py` 覆盖请求参数、响应映射、日期排序、空结果和失败。

## 任务二：PMC 关键词检索

文件：`app/tools/article_search/pmc.py`

1. 在现有 OA Resolver/Downloader 之外增加独立的 `PMCSearcher`。
2. ESearch 使用 `db=pmc` 返回有界 PMCID 列表，EFetch 使用 `db=pmc` 获取 JATS XML。
3. 从 JATS 中提取题名、摘要、作者、DOI、PMID、PMCID、期刊、日期、出版类型和 license。
4. 只报告 PMC 中存在全文页面；是否可以下载仍由 `PMCOAResolver` 判断。
5. 在 `tests/test_pmc_searcher.py` 覆盖 ESearch/EFetch、分页、100 条上限、空结果和部分失败。

## 任务三：bioRxiv/medRxiv 官方 DOI 校验

文件：

- `app/tools/article_search/preprint.py`
- `app/tools/article_search/biorxiv.py`
- `app/tools/article_search/medrxiv.py`

1. 定义共享预印本请求和响应模型；请求接收 Europe PMC 已发现的 DOI，不接受自由关键词。
2. 有界并发调用 `api.biorxiv.org/details/{server}/{doi}/na/json`。
3. 保存服务器、版本、发布日期、分类、摘要、license、JATS XML 线索和正式发表 DOI。
4. 任何无法由官方 API 确认的记录都不作为该服务器的预印本文献返回。
5. 在 `tests/test_preprint_searchers.py` 覆盖两个服务器、部分失败、去重和 100 条上限。

## 任务四：统一来源枚举和文档

文件：

- `app/core/search_strategy_generator.py`
- `docs/superpowers/specs/2026-08-17-multi-source-search-design.md`

1. 扩展 `SearchSource`，加入 `semantic_scholar`、`pmc`、`biorxiv` 和 `medrxiv`。
2. 保持 PubMed/Europe PMC 现有编译器行为不变。
3. 更新实现状态，说明四个连接器的能力边界和 Google Scholar 暂缓原因。

## 验证

1. 分别运行新增连接器测试。
2. 运行 `python -m pytest -q`，确认现有意图识别、MeSH、检索策略、Europe PMC、PubMed、PMC 下载和 ORM 测试不回归。
3. 运行 Python 编译检查和 `git diff --check`。
