# EpiEvidence Agent

面向流行病学与临床研究的多源文献检索和摘要初筛 Agent。用户用自然语言提出研究问题后，系统会识别研究意图、标准化 MeSH 术语、生成检索方案、并行检索多个数据源，并依据摘要可见信息给出中文文献推荐。

> [!IMPORTANT]
> EpiEvidence 是研究辅助工具，不提供诊断或治疗建议。当前质量评价属于“基于摘要和书目信息的初筛”，不能替代阅读全文、正式偏倚风险评价或专业人员判断。

## 当前能力

- 结构化识别 `simple_chat`、`clarification_answer`、`new_research` 和 `supplementary_search`，提取研究方向与中英文候选关键词。
- 使用本地 SQLite MeSH 索引匹配英文候选词，返回 preferred term 与 entry terms。
- 生成数据库无关的 `SearchPlan`，再用确定性 compiler 编译各来源查询式。
- 通过 LangGraph `Send` fan-out 并行检索 PubMed、Europe PMC、PMC 和 Semantic Scholar，再 fan-in 汇总结果。
- 将异构题录统一为同一数据契约，并根据 DOI、PMID、PMCID 或题名组合进行保守去重。
- 将检索事实写入 PostgreSQL，仅在 LangGraph State 和 Redis 中传递任务决策与文章 ID。
- 在进入 LLM 评价前，综合关键词、embedding 相似度和时效性筛选候选文献。
- 按研究设计分批执行 Quality Evaluator：类型化 rubric、LLM 初评、verifier 核查、程序重算与集合级排序。
- 由 Evidence Summarizer 自主选择值得推荐的文献数量，生成普通中文聊天回复。
- FastAPI 提供任务生命周期接口，并以 AG-UI 风格事件通过 SSE 推送运行阶段和最终文本。
- 使用 PostgreSQL LangGraph checkpoint 支持稳定的 `thread_id` 与任务恢复；使用 Langfuse 记录主任务、节点和模型调用 traces。

## 处理流程

```text
POST /api/tasks
  → API 层意图路由
      ├─ simple_chat → 轻量 Chatbot → 完成
      └─ 研究型消息
          → MeSH 标准化
          → Search Plan + 数据源选择
          → 多源并行检索（fan-out）
          → 标准化、去重、PostgreSQL 持久化（fan-in）
          → 候选综合排序
          → 按研究设计分组
          → Celery 分批质量初评与核查
          → Evidence Summarizer
          → 中文回复 + SSE 完成事件
```

## 数据源

| 数据源 | 主要用途 | 当前状态 |
| --- | --- | --- |
| PubMed | 生物医学题录与摘要 | 默认启用 |
| Europe PMC | 题录、摘要与开放获取信息 | 默认启用 |
| PMC | 全文库题录与 OA 资源发现 | 默认启用 |
| Semantic Scholar | 跨学科补充召回与引用信息 | 默认启用 |
| bioRxiv / medRxiv | 预印本发现与校验 | 已有连接器，未注册到默认研究图 |
| Google Scholar | 补充学术搜索 | 暂未接入 |

单个检索源的第一版设计上限为 100 条。预印本不是 peer-reviewed evidence，后续启用时必须保留明确标识。

## 技术架构

| 层 | 技术与职责 |
| --- | --- |
| API | FastAPI；任务创建、查询、取消、恢复及 SSE 事件流 |
| Agent 编排 | LangGraph；状态管理、节点路由、fan-out/fan-in 和 checkpoint |
| LLM | LangChain；OpenAI-compatible API 或 Ollama，Pydantic 结构化输出 |
| 任务执行 | Celery + Redis broker；按研究设计派发质量评价批次 |
| 事实存储 | PostgreSQL + SQLAlchemy 2.0 + Alembic；任务、检索、文献、质量结果 |
| 工作缓存 | Redis；短期状态、小型决策结果和文章 ID 索引 |
| 术语索引 | SQLite；本地 MeSH preferred term 与 entry terms 查询 |
| 可观测性 | Langfuse + Loguru；trace、节点指标、模型调用与运行日志 |

## 项目结构

```text
backend/
├─ agent/                         LangGraph State、主图节点、质量评价子图和汇总器
├─ app/
│  ├─ api/                        FastAPI 路由、AG-UI 事件和任务服务
│  ├─ cache/                      Redis 工作缓存
│  ├─ core/                       意图、MeSH、检索计划、标准化、去重和候选排序
│  ├─ db/                         SQLAlchemy 模型、Session 与 Repository
│  ├─ observability/              Langfuse 运行时包装
│  ├─ runtime/                    任务协调和 PostgreSQL checkpoint
│  ├─ tools/article_search/       各文献数据源检索与 OA 资源工具
│  └─ workers/                    Celery 配置、任务契约与质量评价 Worker
├─ alembic/                       数据库迁移
└─ skills/quality_evaluator/      rubric、Schema、Prompt、评分与评价流程
scripts/                          MeSH 建库、真实任务 smoke run 和候选排序脚本
test_set/                         可重复提交的最小真实 query 集
tests/                            按模块与纵向闭环组织的测试
```

`frontend/` 当前是独立的前端实验目录，尚不是 EpiEvidence 默认启动流程的一部分。

## 本地运行

### 1. 准备环境

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/)、PostgreSQL、Redis，以及可用的 OpenAI-compatible LLM 与 embedding API。

```powershell
uv sync
Copy-Item .env.example .env
```

修改 `.env`，至少提供：

```dotenv
LLM_PROVIDER=zai
LLM_MODEL=glm-5.3-flash
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_API_KEY=your-api-key
LLM_TEMPERATURE=0.0
LLM_TIMEOUT_SECONDS=120

EMBEDDING_MODEL=your-embedding-model
EMBEDDING_API_KEY=your-embedding-api-key
EMBEDDING_BASE_URL=https://your-embedding-endpoint/v1

DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/epi_evidence_db
CHECKPOINTS_DATABASE_URL=postgresql://user:password@localhost:5432/epi_evidence_checkpoints
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/0
```

> [!NOTE]
> `EMBEDDING_MODEL` 必须支持 embedding，不能填写聊天模型。`DATABASE_URL` 是业务事实库；`CHECKPOINTS_DATABASE_URL` 是 LangGraph checkpoint 使用的 psycopg URL。

Langfuse 是可选配置：

```dotenv
LANGFUSE_PUBLIC_KEY=your-public-key
LANGFUSE_SECRET_KEY=your-secret-key
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_TRACING_ENVIRONMENT=local
```

真实密钥只应保存在 `.env`；该文件已被 `.gitignore` 排除。

### 2. 构建 MeSH 索引

将 `MESH.csv.gz` 放到仓库根目录的 `data/`。脚本直接读取 gzip，无需手动解压：

```powershell
uv run python scripts/build_mesh_index.py
Copy-Item data/mesh.sqlite3 backend/data/mesh.sqlite3
```

建库脚本当前生成 `data/mesh.sqlite3`，而默认研究图读取 `backend/data/mesh.sqlite3`，因此现阶段需要执行上面的复制命令。后续应统一这两个路径。

### 3. 初始化 PostgreSQL

分别创建业务数据库和 checkpoint 数据库，确认 `.env` 中的连接地址可用，然后执行业务表迁移：

```powershell
uv run alembic upgrade head
```

应用不会自动创建业务表。

### 4. 启动 Worker 与 API

启动 Redis 后，在一个终端启动质量评价 Worker：

```powershell
uv run celery -A backend.app.workers.celery_app:celery_app worker -l INFO -Q default,quality-rct,quality-cohort,quality-case-control,quality-cross-sectional-analytical,quality-cross-sectional-prevalence,quality-systematic-review,quality-meta-analysis,quality-qualitative-systematic-review,quality-umbrella-review,quality-narrative-review
```

在另一个终端启动 API：

```powershell
uv run uvicorn backend.app.api.main:app --reload
```

- OpenAPI 文档：`http://127.0.0.1:8000/docs`
- 健康检查：`http://127.0.0.1:8000/health`

## API 快速体验

```powershell
$body = @{ query = "司美格鲁肽对肥胖人群的减重效果如何？" } | ConvertTo-Json
$task = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/tasks" -ContentType "application/json" -Body $body
Invoke-RestMethod "http://127.0.0.1:8000/api/tasks/$($task.task_id)"
```

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建研究任务 |
| `GET` | `/api/tasks` | 查询当前进程的任务列表 |
| `GET` | `/api/tasks/{task_id}` | 查询任务快照与最终结果 |
| `GET` | `/api/tasks/{task_id}/events` | 订阅 AG-UI 风格 SSE 事件 |
| `POST` | `/api/tasks/{task_id}/cancel` | 取消本进程中的运行任务 |
| `POST` | `/api/tasks/{task_id}/resume` | 恢复失败或取消的任务 |

> [!NOTE]
> 第一版 API 是无 JWT 的本地研究工作台。任务快照和 SSE 订阅者由当前 API 进程维护；PostgreSQL 保存跨进程研究事实，checkpoint 支持图恢复，但这还不是多实例生产部署方案。

## 验证

```powershell
uv run python -m pytest -q
uv run python scripts/run_agent_test_set.py --timeout 180
uv run python scripts/verify_langfuse.py
```

测试套件默认不会调用真实 LLM 或外部文献服务；`run_agent_test_set.py` 是真实 smoke run，其结果取决于本地 PostgreSQL、Redis、Worker、模型配置和外部 API 可用性。

## 当前边界

- 初筛主要依据标题、摘要、研究设计、期刊与时间等可见信息；尚未实现完整全文解析和正式全文偏倚风险评价。
- PMC 与 Europe PMC 已能发现开放获取资源；“用户选择后下载、解析并精读全文”的闭环仍待完成。
- 未知或尚未注册 rubric 的研究设计会保留在检索结果中，但不会伪装成已完成质量评价。
- bioRxiv、medRxiv 连接器尚未进入默认数据源选择与主图编排。
- 候选 embedding 排序已接入主图，但仍是可解释的第一版权重方案，不是训练得到的 reward model。
- `main.py` 仍是脚手架入口；实际 API 入口为 `backend.app.api.main:app`。

项目当前重点是稳定“自然语言研究问题 → 多源摘要检索 → 可追踪的质量初筛 → 中文文献推荐”这一后端闭环。
