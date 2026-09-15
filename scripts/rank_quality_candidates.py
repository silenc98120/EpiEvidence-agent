"""对指定测试任务的去重文章做质量评价前综合排序。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.intent_recognizer import IntentRecognizer
from backend.app.core.llm_provider import create_chat_model, normalize_openai_base_url
from backend.app.core.quality_candidate_selector import (
    CandidateArticle,
    OpenAICompatibleEmbeddingClient,
    QualityCandidateSelector,
    ResearchProfile,
)
from backend.app.db.models import ArticleORM, SearchRunORM, SourceRecordORM, TaskORM
from backend.app.db.session import build_async_engine
from scripts.run_agent_test_set import load_test_set


DEFAULT_CASE_ID = "semaglutide-obesity-older-adults"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="试运行质量评价前候选文章综合排序")
    parser.add_argument("--case-id", default=DEFAULT_CASE_ID, help="test_set 中的研究型 query ID")
    parser.add_argument("--task-id", help="要读取的既有任务 UUID；未指定时匹配最新同 query 任务")
    parser.add_argument("--output", type=Path, help="JSON 报告路径")
    return parser.parse_args()


async def _find_task_id(
    session: AsyncSession,
    *,
    task_id: str | None,
    user_query: str,
) -> UUID:
    if task_id:
        parsed = UUID(task_id)
        task = await session.get(TaskORM, parsed)
        if task is None:
            raise LookupError(f"任务不存在: {parsed}")
        return parsed
    statement = (
        select(TaskORM.task_id)
        .where(TaskORM.user_query == user_query)
        .order_by(TaskORM.created_at.desc())
        .limit(1)
    )
    resolved = await session.scalar(statement)
    if resolved is None:
        raise LookupError("找不到与测试 query 完全相同的已持久化任务；请显式传入 --task-id")
    return resolved


async def _load_articles(session: AsyncSession, task_id: UUID) -> list[CandidateArticle]:
    statement = (
        select(ArticleORM)
        .join(SourceRecordORM, SourceRecordORM.article_id == ArticleORM.article_id)
        .join(SearchRunORM, SearchRunORM.search_run_id == SourceRecordORM.search_run_id)
        .where(SearchRunORM.task_id == task_id)
        .order_by(ArticleORM.article_id)
    )
    rows = (await session.scalars(statement)).unique().all()
    return [
        CandidateArticle(
            article_id=str(row.article_id),
            title=row.title,
            abstract=row.abstract,
            publication_date=row.publication_date,
            publication_year=row.publication_year,
            study_design=row.study_design,
        )
        for row in rows
    ]


def _case(case_id: str) -> dict[str, Any]:
    for case in load_test_set():
        if case.get("id") == case_id:
            return case
    raise LookupError(f"test_set 中不存在 case_id: {case_id}")


def _embedding_client() -> OpenAICompatibleEmbeddingClient:
    model = os.getenv("EMBEDDING_MODEL", "").strip()
    if not model:
        raise RuntimeError(
            "缺少 EMBEDDING_MODEL。请配置独立的 embedding 模型，"
            "不要复用 LLM_MODEL。"
        )
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 EMBEDDING_API_KEY，且无法回退到 LLM_API_KEY")
    base_url = normalize_openai_base_url(
        os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL")
    )
    return OpenAICompatibleEmbeddingClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


async def run(args: argparse.Namespace) -> Path:
    load_dotenv()
    case = _case(args.case_id)
    user_query = str(case["query"])
    embedding_client = _embedding_client()
    engine = build_async_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            task_id = await _find_task_id(session, task_id=args.task_id, user_query=user_query)
            articles = await _load_articles(session, task_id)
        if not articles:
            raise LookupError(f"任务 {task_id} 没有已持久化的去重文章")

        intent = await IntentRecognizer(create_chat_model()).recognize(user_query)
        profile = ResearchProfile.from_intent(intent.model_dump(mode="json"))
        selector = QualityCandidateSelector(embedding_client)
        scores = await selector.rank(profile=profile, articles=articles)

        report = {
            "trial": "quality-candidate-ranking-v1",
            "task_id": str(task_id),
            "case_id": args.case_id,
            "user_query": user_query,
            "intent_analysis": intent.model_dump(mode="json"),
            "embedding_model": os.environ["EMBEDDING_MODEL"],
            "reference_date": date.today().isoformat(),
            "weights": {
                "keyword_similarity": 0.50,
                "embedding_similarity": 0.40,
                "recency": 0.10,
            },
            "article_count": len(scores),
            "ranking": [score.as_dict(rank) for rank, score in enumerate(scores, start=1)],
        }
        output = args.output or Path("artifacts") / f"quality-candidate-ranking-{task_id}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return output
    finally:
        await embedding_client.close()
        await engine.dispose()


def main() -> None:
    # 该独立脚本不会经过 FastAPI/Celery 的启动路径；Windows 下 psycopg async
    # 必须在 asyncio.run 创建事件循环之前切到 Selector policy。
    from backend.app.runtime.checkpoint import configure_psycopg_event_loop_policy

    configure_psycopg_event_loop_policy()
    output = asyncio.run(run(parse_args()))
    print(f"候选排序报告已生成: {output}")


if __name__ == "__main__":
    main()
