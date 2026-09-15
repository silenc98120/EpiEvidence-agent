# Agent test_set

`agent_queries.json` 保存可以从前端入口重复提交的最小测试集：

- 研究型 query：检验 `intent-recognizer` 是否识别为 `new_research`，并继续进入 MeSH、检索和质量评价流程；
- 简单问候：检验是否识别为 `simple_chat`，不应启动文献检索。

运行方式：

```powershell
.\.venv\Scripts\python.exe scripts/run_agent_test_set.py --timeout 180
```

脚本会输出每条任务的 `task_id`、状态、阶段序列、checkpoint 中的 `intent_analysis`、错误和最终结果。API key 不会写入输出。

如果任务在 `intent-recognizer` 失败，可追加 `--verbose` 查看 LLM 异常摘要：

```powershell
.\.venv\Scripts\python.exe scripts/run_agent_test_set.py --timeout 180 --verbose
```
