# Eval

该目录承载与生产主图解耦的离线评测。质量评估 Judge V0 通过 `task_id` 只读提取
PostgreSQL 持久化结果；`--dry-run` 仅验证材料是否可评测，不调用 LLM，也不写入
`results/`。

```powershell
.venv\Scripts\python.exe Eval\scripts\run_quality_evaluation_judge.py `
  --task-id <UUID> --dry-run
```

Judge 模型使用独立的 `JUDGE_*` 环境变量。正式 `--execute` 流程将在后续切片实现。
