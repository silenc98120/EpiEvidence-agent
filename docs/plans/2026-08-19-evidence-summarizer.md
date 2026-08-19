# Evidence Summarizer 实现计划

## 目标

在 Quality Evaluator 的确定性排序之后增加独立 Summarizer 节点。模型从全部 `ranked_candidates` 中自行决定最终推荐数量，输出严格结构化草稿；程序校验 UUID、恢复既定排序、补充 PostgreSQL 题录与全文资源，并把最终简体中文回复作为一条 `AIMessage` 追加到 LangGraph `MessagesState.messages`。第一版不静默截断为固定的前 N 篇。

用户最终只看到普通 Chat 消息，不看到内部 JSON，也不接收 HTML。

## Task 1：实现 Summarizer Schema、Prompt 与运行时（已完成）

文件：

- 新建 `agent/summarizer.py`
- 新建 `app/prompts/evidence_summarizer_prompt_template.py`

步骤：

1. 定义严格 Pydantic 模型：文章草稿、Summarizer 草稿、搜索概览、最终推荐文章和最终总结。
2. 校验模型选择的 UUID 是 ranked 候选子集，文章草稿与选择列表完全一致且不重复。
3. 编写 system prompt，禁止模型修改分数、排序、全文链接和医学事实；允许选择任意数量或空集合。
4. 实现结构化 LLM 调用、有限重试、Token/延迟日志和失败异常。
5. 实现 PostgreSQL hydrate：按 UUID 读取文章和全文资源，并按 PDF、XML/HTML、其他可下载 OA 资源选择首选链接。
6. 实现确定性最终结果组装和简体中文 Chat 文本渲染。

## Task 2：接入 EvidenceState 与 LangGraph（已完成）

文件：

- 修改 `agent/state.py`
- 修改 `agent/graph.py`

步骤：

1. 在 State 中增加内部 `final_summary` 字段；最终用户回复继续使用父类 `messages`。
2. 新增 Summarizer 节点工厂，接收 `ranked_candidates`、`portfolio_evaluation`、`search_metrics`、query 和 intent。
3. 空候选时跳过 LLM，生成明确的无推荐结果消息。
4. 成功时返回 `final_summary`、`task_stage=completed` 和 `[AIMessage(...)]`，由 `add_messages` reducer 将其追加为最后一条消息。
5. 失败时生成可重试的助手消息并递增 `summary_retry_count`，不得伪造推荐文献。
6. 将 `quality_evaluation_fan_in -> END` 改为 `quality_evaluation_fan_in -> evidence_summarizer -> END`。

## Task 3：补充必要测试并回归（已完成）

文件：

- 新建 `tests/test_evidence_summarizer.py`
- 修改 `tests/test_agent_graph.py`

步骤：

1. 覆盖任意数量选择、空选择、未知 UUID、重复 UUID和排序恢复。
2. 覆盖真实分数由程序写入、全文链接来源校验和资源优先级。
3. 覆盖最终 `AIMessage` 是消息列表最后一条，并且 JSON 只保存在结构化 metadata/state 中。
4. 覆盖无候选与 LLM 失败的可读消息。
5. 运行 Summarizer 与 Graph 定向测试，再运行全仓测试。定向测试结果：`19 passed`。全仓备用解释器结果：`124 passed, 2 failed`；两个失败属于既有 Quality Evaluator 运行时测试在备用解释器中的动态模型兼容问题。

## 完成标准

- 模型可自行决定推荐 0 到全部候选中的任意数量。
- 最终推荐 UUID 全部来自 `ranked_candidates`，顺序与程序排序一致。
- 分数、检索统计和全文链接均由程序提供。
- `messages[-1]` 是最终 `AIMessage`，其 `content` 是普通简体中文回复。
- 不输出 HTML，不把内部结构化 JSON 直接显示给用户。
