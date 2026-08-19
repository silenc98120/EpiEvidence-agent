# EpiEvidence Agent

面向流行病学和临床研究的证据检索 Agent。用户只需要提出自然语言问题，系统会完成意图识别、MeSH 术语标准化、多数据库检索、结果去重、摘要初筛和文献推荐，并把最终结果渲染成普通中文对话消息。

> [!IMPORTANT]
> 当前仓库已经实现核心检索与评价编排，但还没有可直接启动的 FastAPI、AG-UI 或 SSE 服务入口。`main.py` 目前仍是占位程序，前端接入属于后续工作。

## 当前流程

```text
用户 query
  ↓
意图识别（LangChain + Pydantic）
  ↓
MeSH 关键词标准化（本地 SQLite）
  ↓
生成数据库无关的 Search Plan
  ↓
按数据源 fan-out 并行检索
  ├─ PubMed
  ├─ Europe PMC
  ├─ PMC
  └─ Semantic Scholar
  ↓
统一结果格式、去重、合并（fan-in）
  ↓
按研究类型分组
  ↓
Quality Evaluator 分组评价、核查、排序
  ↓
Evidence Summarizer 选择推荐文献
  ↓
MessagesState.messages 的最后一条 AIMessage
```

## 已实现能力

- **意图识别**：使用结构化 LLM 输出识别 `simple_chat`、`clarification_answer`、`new_research` 和 `supplementary_search`，同时提取研究方向和候选关键词。
- **MeSH 标准化**：从 `data/MESH.csv.gz` 构建本地 SQLite 索引，对 LLM 提供的英文候选进行精确匹配，并返回首选术语和 entry terms。
- **检索策略**：根据概念组、MeSH 术语和近似词生成中间 `SearchPlan`，再由各数据源的确定性编译器生成查询式。
- **文献检索**：已提供 PubMed、Europe PMC、PMC、Semantic Scholar 检索器；bioRxiv/medRxiv 提供预印本 DOI 发现与校验连接器。每个来源默认最多返回 100 条记录。
- **全文资源发现**：Europe PMC 和 PMC OA 工具支持发现可下载资源，并优先选择 PDF；同时保留 EPUB、HTML、XML 和纯文本等可读格式。
- **结果处理**：统一不同来源的题录和摘要格式，依据 DOI、PMID、PMCID 和标题等信息进行确定性去重。
- **质量评价**：Quality Evaluator 支持按研究类型选择评价规则，包含结构化 LLM 评价、结果核查、程序重算、集合级评价和稳定排序。
- **最终汇总**：Summarizer 自主决定推荐数量，可以推荐 0 到全部候选；内部结果写入 `final_summary`，用户只看到中文对话文本。
- **工作缓存**：Redis 保存任务状态、检索运行文章 ID、分组文章 ID 等短期索引；题录、摘要和全文资源仍以 PostgreSQL 为事实来源。
- **数据模型**：提供 PostgreSQL 基线建表脚本和 SQLAlchemy 2.0 异步 ORM 映射。

## 质量评价边界

第一版主要对摘要和基础书目信息进行初筛，包括相关性、可回答性、研究设计、期刊与时效性信号，以及摘要中能观察到的研究方法和结果线索。正式全文解析、全文偏倚风险评价和完整学术写作不属于当前第一版范围。

## 技术栈

- Python 3.11+
- LangChain、LangGraph、Pydantic
- `httpx` 异步调用 PubMed、Europe PMC、PMC 和 Semantic Scholar 接口
- SQLite：本地 MeSH 索引
- PostgreSQL + SQLAlchemy 2.0 + psycopg 3：任务、检索运行、文献和全文资源持久化
- Redis：短期工作缓存
- Loguru：结构化运行日志
- uv：依赖和虚拟环境管理

## 项目结构

```text
agent/                     LangGraph 状态、节点和 fan-out/fan-in 编排
app/core/                  意图识别、MeSH、Search Plan、结果标准化和去重
app/tools/article_search/  各文献数据库检索器和全文资源工具
app/db/                    SQLAlchemy 基类、ORM 模型和异步 Session 工厂
app/cache/                 Redis 工作缓存
skills/quality_evaluator/  质量评价规则、Schema、提示词和评价子图
database/schema.sql        PostgreSQL 基线表结构
scripts/build_mesh_index.py 构建本地 MeSH SQLite 索引
tests/                     单元测试和 LangGraph 编排测试
```

## 环境配置

先安装依赖：

```powershell
uv sync
```

LLM 客户端通过环境变量配置。请在本地 `.env` 中设置，不要把真实密钥写入代码或提交到 Git：

```dotenv
LLM_PROVIDER=deepseek
LLM_MODEL=your-model
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=your-api-key
LLM_TEMPERATURE=0

# 实际使用 PostgreSQL 时设置
DATABASE_URL=postgresql://user:password@localhost:5432/epi_evidence

# Redis 可选；默认 redis://localhost:6379/0，缓存不可用时主流程默认继续
REDIS_URL=redis://localhost:6379/0
```

支持 OpenAI 兼容接口的 `openai`、`deepseek`、`qwen`、`kimi`、`modelscope` 和 `openrouter`，也支持 `ollama` 本地模型。不同服务商只需要调整 `LLM_PROVIDER`、`LLM_MODEL`、`LLM_BASE_URL` 和 `LLM_API_KEY`。

## 构建 MeSH 索引

源文件放在 `data/MESH.csv.gz`。构建脚本会直接读取 gzip 文件并写入本地 SQLite，不需要手动解压：

```powershell
uv run python scripts/build_mesh_index.py
```

默认生成 `data/mesh.sqlite3`。该文件已加入 `.gitignore`，应在本地按需生成。

## 初始化 PostgreSQL

准备 PostgreSQL 12 或更高版本的数据库和 `DATABASE_URL` 后执行：

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f database/schema.sql
```

基线表包括 `tasks`、`search_runs`、`articles`、`source_records` 和 `full_text_resources`。应用中的 SQLAlchemy 映射位于 `app/db/models.py`；应用启动不会自动建表或执行迁移。

## 运行测试

```powershell
uv run python -m pytest -q
```

也可以只运行某个模块的测试，例如：

```powershell
uv run python -m pytest tests/test_europepmc_searcher.py -v
```

## 当前限制和下一步

- `main.py` 尚未替换为 FastAPI 应用入口。
- AG-UI 事件标签、SSE 推送和前端 Chat 页面尚未接入。
- PostgreSQL Repository 的完整检索结果写入流程仍需接入主图。
- 下载后的全文解析、正文标准化和全文偏倚风险评价仍待实现。
- Google Scholar 暂缓接入；Celery/RabbitMQ 也不属于当前版本的必要依赖。

当前版本的重点是先跑通“自然语言问题 → 多源摘要检索 → 质量筛选 → 中文推荐回复”的后端闭环。
