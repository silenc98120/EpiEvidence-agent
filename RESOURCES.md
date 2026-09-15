# Langfuse 可观测性实践 Resources

## Knowledge

- [Langfuse 官方文档：Python SDK](https://langfuse.com/docs/observability/sdk/python/overview)
  用于核对 Python SDK 的初始化、Trace、Span、Generation 和结果刷新方式；在实际写入 SDK 调用前以此页面为准。
- [Langfuse 官方文档：LangChain 追踪](https://langfuse.com/docs/integrations/langchain/tracing)
  用于理解 LangChain 模型调用如何自动或半自动记录 Generation；适用于意图识别、质量评价和最终汇总。
- [LangGraph 官方文档：Retry policies](https://langchain-ai.github.io/langgraph/how-tos/retry-policies/)
  用于区分图节点级重试与 HTTP、LLM、Celery 自身重试，避免重复调用。
- 当前项目调用链：`backend/app/runtime/task_runtime.py`、`backend/agent/graph.py`、`backend/agent/nodes.py`
  用于将通用观测概念落到 EpiEvidence 的 TaskCoordinator、节点、并行检索与质量评价批次。

## Wisdom (Communities)

- [Langfuse GitHub Discussions](https://github.com/langfuse/langfuse/discussions)
  用于核对真实项目中的 SDK 升级、异步上下文、追踪断链和自托管部署经验。

## Gaps

- 当前执行环境的官方文档读取请求被本地审批服务异常拒绝；后续 SDK 用法以本机已安装的 `langfuse==4.15.2` 类型签名为准，并在可访问官方页面后再做一次版本核对。
