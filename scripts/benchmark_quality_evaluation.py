"""复用本地 RCT 题录运行真实单篇质量子图，输出不含密钥的耗时报告。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from uuid import UUID, uuid4
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from loguru import logger
from backend.app.runtime.checkpoint import configure_psycopg_event_loop_policy
from backend.app.db.session import ManagedSessionFactory
from backend.app.db.models import ArticleORM, TaskORM, QualityEvaluationBatchORM
from backend.app.db.repositories.tasks import ResearchTaskRepository
from backend.app.db.repositories.quality_evaluation_results import QualityEvaluationBatchRepository
from backend.app.core.llm_provider import create_eval_chat_model
from backend.skills.quality_evaluator.evaluator import QualityEvaluatorRuntime
from backend.skills.quality_evaluator.repository import ArticleRepository
from backend.skills.quality_evaluator.schema import StudyType
from backend.agent.subgraphs.quality_evaluation import build_quality_evaluation_subgraph


class Timings:
    def __init__(self):
        self.nodes = defaultdict(list)

    def wrap_node(self, *, name, node, **kwargs):
        async def observed(state):
            started = time.perf_counter()
            try:
                return await node(state)
            finally:
                self.nodes[name].append(round((time.perf_counter() - started) * 1000, 3))
        return observed


async def benchmark(limit, concurrency, reuse_task=None):
    os.environ["QUALITY_EXPERT_CONCURRENCY"] = str(concurrency)
    factory = ManagedSessionFactory()
    task_id = UUID(reuse_task) if reuse_task else uuid4()
    started = time.perf_counter()
    report = {"task_id": str(task_id), "concurrency": concurrency, "batch_size": 1}
    observations = Timings()
    try:
        async with factory() as session:
            articles = (await session.scalars(select(ArticleORM).where(
                ArticleORM.study_design == StudyType.RCT.value,
                ArticleORM.abstract.is_not(None)
            ).order_by(ArticleORM.updated_at.desc(), ArticleORM.article_id).limit(limit))).all()
        if len(articles) != limit:
            raise RuntimeError("本地没有足够的 RCT 摘要可用于本次样本")
        query = "随机对照试验中，干预措施相对对照对主要临床结局的效果如何？"
        tasks = ResearchTaskRepository(factory)
        await tasks.ensure_task(task_id=task_id, query=query, conversation_id=None)
        model = create_eval_chat_model()
        runtime = QualityEvaluatorRuntime(model=model, repository=ArticleRepository(factory))
        repository = QualityEvaluationBatchRepository(factory)
        graph = build_quality_evaluation_subgraph(evaluator_runtime=runtime,
            batch_repository=repository, langfuse_runtime=observations)
        state = {"task_id": str(task_id), "user_query": query, "intent_analysis": {}, "search_plan": {},
            "search_results": [{"article_id": str(a.article_id), "study_design": a.study_design} for a in articles]}
        result = await graph.ainvoke(state)
        report["workflow_ms"] = round((time.perf_counter() - started) * 1000, 3)
        report["retained_count"] = len(result["retained_candidates"])
        report["failures"] = result["quality_evaluation_failures"]
        report["portfolio_completed"] = result["portfolio_evaluation"] is not None
        replay = time.perf_counter()
        await graph.ainvoke(state)
        report["replay_ms"] = round((time.perf_counter() - replay) * 1000, 3)
        report["status"] = "completed"
        await tasks.mark_completed(task_id)
    except Exception as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        import traceback
        report["error_location"] = [(Path(f.filename).name, f.lineno, f.name) for f in traceback.extract_tb(exc.__traceback__)]
        report["workflow_ms"] = round((time.perf_counter() - started) * 1000, 3)
    finally:
        async with factory() as session:
            rows = (await session.scalars(select(QualityEvaluationBatchORM).where(QualityEvaluationBatchORM.task_id == task_id))).all()
            report["batches"] = [{"batch_id": r.batch_id, "kind": r.input_payload["kind"],
                "status": r.status, "attempt_count": r.attempt_count,
                "metrics": r.metrics_payload, "error_type": r.error_type} for r in rows]
        report["nodes_ms"] = dict(observations.nodes)
        await factory.dispose()
        directory = Path("artifacts/quality-benchmarks")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{task_id}-{datetime.now():%Y%m%dT%H%M%S}.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k not in {"batches", "nodes_ms"}}, ensure_ascii=False))
        print("REPORT", target)
    return report["status"] == "completed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--reuse-task", help="复用本脚本同一查询的旧任务，比较下游阶段；不覆盖已有报告")
    options = parser.parse_args()
    configure_psycopg_event_loop_policy()
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    raise SystemExit(0 if asyncio.run(benchmark(options.limit, options.concurrency, options.reuse_task)) else 1)
