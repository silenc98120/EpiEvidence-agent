"""质量评估子图的分组与批次准备阶段。

本模块只负责 Agent 侧编排数据的准备：把规范化检索结果按研究类型分组，
再稳定地切成可提交给 Celery Worker 的独立批次。单个批次的质量评估仍由
``backend.app.workers.celery_tasks`` 执行。
"""

from __future__ import annotations

import asyncio
import inspect
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from sqlalchemy import select

from backend.agent.state import EvidenceState
from backend.app.cache import RedisWorkCache
from backend.app.db.models import QualityEvaluationBatchORM
from backend.app.db.session import build_session_factory
from backend.app.observability import LangfuseRuntime
from backend.app.workers.celery_app import celery_app
from backend.skills.quality_evaluator.repository import ArticleRepository
from backend.skills.quality_evaluator.schema import (
    HydratedArticleBatch,
    QualityEvaluationJob,
    StudyType,
)
from backend.skills.quality_evaluator.scoring import partition_by_threshold
from backend.skills.quality_evaluator.subgraph import GroupEvaluationResult


class QualityEvaluationSubgraphState(TypedDict, total=False):
    """质量评估子图内部需要的最小可序列化状态。"""

    task_id: str
    search_results: list[dict[str, Any]]
    article_groups: dict[str, list[str]]
    quality_groups: list[dict[str, Any]]
    quality_batches: list[dict[str, Any]]
    quality_batch_results: list[dict[str, Any]]
    quality_batch_status: dict[str, str]
    quality_batch_failures: list[dict[str, Any]]
    all_quality_batches_terminal: bool
    retained_articles: list[dict[str, Any]]
    excluded_articles: list[dict[str, Any]]
    ranked_articles: list[dict[str, Any]]
    truncated_candidates: list[dict[str, Any]]
    quality_candidates_truncated: bool
    quality_reason_summaries: list[dict[str, Any]]
    quality_reason_failures: list[dict[str, Any]]
    quality_candidate_limit: int
    portfolio_evaluation: dict[str, Any] | None
    quality_screening_threshold: float
    quality_batches_timed_out: bool
    user_query: str
    retained_candidates: list[dict[str, Any]]
    excluded_candidates: list[dict[str, Any]]
    quality_evaluation_failures: list[dict[str, Any]]
    ranked_candidates: list[dict[str, Any]]
    task_stage: str


QUALITY_TASK_BY_STUDY_TYPE: dict[StudyType, str] = {
    StudyType.RCT: "quality.evaluate_rct_batch",
    StudyType.COHORT: "quality.evaluate_cohort_batch",
    StudyType.CASE_CONTROL: "quality.evaluate_case_control_batch",
    StudyType.CROSS_SECTIONAL_ANALYTICAL: (
        "quality.evaluate_cross_sectional_analytical_batch"
    ),
    StudyType.CROSS_SECTIONAL_PREVALENCE: (
        "quality.evaluate_cross_sectional_prevalence_batch"
    ),
    StudyType.SYSTEMATIC_REVIEW: "quality.evaluate_systematic_review_batch",
    StudyType.META_ANALYSIS: "quality.evaluate_meta_analysis_batch",
    StudyType.QUALITATIVE_SYSTEMATIC_REVIEW: (
        "quality.evaluate_qualitative_systematic_review_batch"
    ),
    StudyType.UMBRELLA_REVIEW: "quality.evaluate_umbrella_review_batch",
    StudyType.NARRATIVE_REVIEW: "quality.evaluate_narrative_review_batch",
}


QUALITY_QUEUE_BY_STUDY_TYPE: dict[StudyType, str] = {
    StudyType.RCT: "quality-rct",
    StudyType.COHORT: "quality-cohort",
    StudyType.CASE_CONTROL: "quality-case-control",
    StudyType.CROSS_SECTIONAL_ANALYTICAL: "quality-cross-sectional-analytical",
    StudyType.CROSS_SECTIONAL_PREVALENCE: "quality-cross-sectional-prevalence",
    StudyType.SYSTEMATIC_REVIEW: "quality-systematic-review",
    StudyType.META_ANALYSIS: "quality-meta-analysis",
    StudyType.QUALITATIVE_SYSTEMATIC_REVIEW: (
        "quality-qualitative-systematic-review"
    ),
    StudyType.UMBRELLA_REVIEW: "quality-umbrella-review",
    StudyType.NARRATIVE_REVIEW: "quality-narrative-review",
}


QualityBatchDispatcher = Callable[[str, dict[str, Any], str], Any]
QualityBatchReader = Callable[[str, Sequence[str]], Awaitable[list[dict[str, Any]]]]
QualityBatchHydrator = Callable[
    [QualityEvaluationJob, str], Awaitable[HydratedArticleBatch]
]
QualityReasonSummarizer = Callable[
    [Sequence[Mapping[str, Any]]], Awaitable[Sequence[Mapping[str, Any]]]
]


def _clean_group_value(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(
        r"[^A-Za-z0-9\u4e00-\u9fff]+",
        "_",
        str(value).strip().casefold(),
    )
    cleaned = cleaned.strip("_")
    return cleaned or None


def record_article_id(record: Mapping[str, Any]) -> str:
    """读取规范化记录中的稳定文章 ID。"""

    for field_name in ("article_id", "canonical_article_id", "source_article_id"):
        value = record.get(field_name)
        if value:
            return str(value)
    value = record.get("source_record_id")
    if value:
        return str(value)
    raise ValueError("统一文献记录缺少 article_id")


def study_group_id(record: Mapping[str, Any]) -> str:
    """将检索记录映射到受支持的稳定研究类型值。"""

    study_design = _clean_group_value(record.get("study_design")) or ""
    publication_types = "_".join(
        _clean_group_value(value) or ""
        for value in record.get("publication_types", [])
    )
    normalized = "_".join(part for part in (study_design, publication_types) if part)
    if study_design in {study_type.value for study_type in StudyType}:
        return study_design

    aliases = (
        (("qualitative_systematic", "qualitative_evidence_synthesis"), StudyType.QUALITATIVE_SYSTEMATIC_REVIEW),
        (("umbrella_review", "review_of_reviews"), StudyType.UMBRELLA_REVIEW),
        (("meta_analysis", "metaanalysis"), StudyType.META_ANALYSIS),
        (("systematic_review",), StudyType.SYSTEMATIC_REVIEW),
        (("randomized", "randomised"), StudyType.RCT),
        (("case_control",), StudyType.CASE_CONTROL),
        (("cohort",), StudyType.COHORT),
    )
    for needles, study_type in aliases:
        if any(needle in normalized for needle in needles):
            return study_type.value
    if "cross_sectional" in normalized:
        if "prevalence" in normalized:
            return StudyType.CROSS_SECTIONAL_PREVALENCE.value
        return StudyType.CROSS_SECTIONAL_ANALYTICAL.value
    if "narrative_review" in normalized or "review" in normalized:
        return StudyType.NARRATIVE_REVIEW.value
    return "unknown"


def group_quality_articles(
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """按研究类型分组，并保留每组稳定的文章顺序。"""

    groups: dict[str, list[str]] = {}
    group_details: dict[str, dict[str, Any]] = {}
    for record in records:
        article_id = record_article_id(record)
        group_id = study_group_id(record)
        groups.setdefault(group_id, []).append(article_id)
        group_details.setdefault(
            group_id,
            {
                "group_id": group_id,
                "study_design": group_id,
                "article_ids": [],
            },
        )["article_ids"].append(article_id)
    return groups, list(group_details.values())


def split_quality_batch(
    group: Mapping[str, Any],
    *,
    max_batch_size: int = 10,
) -> list[dict[str, Any]]:
    """将一个研究类型分组稳定地切成可独立执行的批次。"""

    if max_batch_size < 1:
        raise ValueError("max_batch_size 必须大于 0")

    article_ids = list(group.get("article_ids", []))
    if not article_ids:
        return []

    batch_count = (len(article_ids) + max_batch_size - 1) // max_batch_size
    base_size, remainder = divmod(len(article_ids), batch_count)
    batches: list[dict[str, Any]] = []
    cursor = 0
    group_id = str(group["group_id"])
    for index in range(batch_count):
        current_size = base_size + (1 if index < remainder else 0)
        current_article_ids = article_ids[cursor : cursor + current_size]
        cursor += current_size
        batches.append(
            {
                **group,
                "batch_id": f"{group_id}:batch-{index + 1:03d}",
                "batch_index": index,
                "article_ids": current_article_ids,
            }
        )
    return batches


def quality_grouping_node(work_cache: RedisWorkCache | None = None):
    """创建质量评估子图的研究类型分组节点。"""

    async def group_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        groups, quality_groups = group_quality_articles(
            state.get("search_results", [])
        )
        if work_cache is not None:
            for group in quality_groups:
                await work_cache.replace_group_article_ids(
                    state["task_id"],
                    group["group_id"],
                    group["article_ids"],
                )
        return {
            "article_groups": groups,
            "quality_groups": quality_groups,
            "task_stage": "articles_grouped",
        }

    return group_node


def quality_batcher_node(*, max_batch_size: int = 10):
    """创建质量评估子图的批次切分节点。"""

    async def batch_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        batches = [
            batch
            for group in state.get("quality_groups", [])
            for batch in split_quality_batch(
                group,
                max_batch_size=max_batch_size,
            )
        ]
        return {
            "quality_batches": batches,
            "task_stage": "quality_batches_created",
        }

    return batch_node


def _dispatch_quality_batch(
    task_name: str,
    payload: dict[str, Any],
    queue_name: str,
) -> Any:
    """通过固定 Celery task 名称提交一个已经切好的批次。"""

    return celery_app.send_task(
        task_name,
        kwargs={"payload": payload},
        queue=queue_name,
        routing_key=queue_name,
    )


def _build_quality_batch_payload(
    batch: Mapping[str, Any],
    *,
    task_id: str,
) -> dict[str, Any]:
    try:
        study_type = StudyType(str(batch["study_design"]))
        group_id = str(batch["group_id"])
        batch_id = str(batch["batch_id"])
        article_ids = list(batch["article_ids"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("质量评估 batch 缺少合法的任务、研究类型或文章 ID") from exc

    if study_type not in QUALITY_TASK_BY_STUDY_TYPE:
        raise ValueError(f"暂未配置 {study_type.value} 的固定 Celery task")
    return {
        "batch_id": batch_id,
        "job": {
            "task_id": task_id,
            "group_id": group_id,
            "study_type": study_type.value,
            "article_ids": [str(article_id) for article_id in article_ids],
        },
    }


def submit_quality_batches_node(
    dispatcher: QualityBatchDispatcher | None = None,
):
    """将质量评估子图生成的 batch 提交给固定研究类型 Celery task。"""

    send = dispatcher or _dispatch_quality_batch

    async def submit_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        submitted: list[dict[str, Any]] = []
        for raw_batch in state.get("quality_batches", []):
            batch = dict(raw_batch)
            if batch.get("celery_task_id"):
                submitted.append(batch)
                continue
            payload = _build_quality_batch_payload(
                batch,
                task_id=str(state["task_id"]),
            )
            study_type = StudyType(payload["job"]["study_type"])
            task_name = QUALITY_TASK_BY_STUDY_TYPE[study_type]
            queue_name = QUALITY_QUEUE_BY_STUDY_TYPE[study_type]
            result = send(task_name, payload, queue_name)
            if hasattr(result, "id"):
                celery_task_id = str(result.id)
            else:
                celery_task_id = str(result)
            submitted.append(
                {
                    **batch,
                    "celery_task_id": celery_task_id,
                    "task_name": task_name,
                    "queue_name": queue_name,
                    "status": "submitted",
                }
            )
        return {
            "quality_batches": submitted,
            "task_stage": "quality_batches_submitted",
        }

    return submit_node


_batch_session_factory: Any | None = None


def _get_batch_session_factory() -> Any:
    global _batch_session_factory
    if _batch_session_factory is None:
        _batch_session_factory = build_session_factory()
    return _batch_session_factory


async def _read_quality_batch_results(
    task_id: str,
    batch_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """从 PostgreSQL 读取当前任务的批次状态和已持久化结果。"""

    if not batch_ids:
        return []
    session_factory = _get_batch_session_factory()
    statement = select(QualityEvaluationBatchORM).where(
        QualityEvaluationBatchORM.task_id == task_id,
        QualityEvaluationBatchORM.batch_id.in_(list(batch_ids)),
    )
    async with session_factory() as session:
        rows = list((await session.scalars(statement)).all())
    return [
        {
            "batch_id": row.batch_id,
            "group_id": row.group_id,
            "study_type": row.study_type,
            "status": row.status,
            "attempt_count": row.attempt_count,
            "article_ids": [str(article_id) for article_id in row.article_ids],
            "assessment_payload": row.assessment_payload,
            "verification_payload": row.verification_payload,
            "score_payload": row.score_payload,
            "retained_article_ids": [
                str(article_id) for article_id in row.retained_article_ids
            ],
            "excluded_article_ids": [
                str(article_id) for article_id in row.excluded_article_ids
            ],
            "error_type": row.error_type,
            "error_message": row.error_message,
        }
        for row in rows
    ]


def read_quality_batch_results_node(
    reader: QualityBatchReader | None = None,
):
    """读取 Celery Worker 写入 PostgreSQL 的批次结果。"""

    load_results = reader or _read_quality_batch_results

    async def read_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        task_id = str(state["task_id"])
        batches = list(state.get("quality_batches", []))
        batch_ids = [str(batch["batch_id"]) for batch in batches]
        results = await load_results(task_id, batch_ids)
        status_by_batch = {item["batch_id"]: item["status"] for item in results}
        failures = [
            item
            for item in results
            if item["status"] == "failed"
        ]
        all_terminal = len(results) == len(batch_ids) and all(
            item["status"] in {"completed", "failed"} for item in results
        )
        return {
            "quality_batch_results": results,
            "quality_batch_status": status_by_batch,
            "quality_batch_failures": failures,
            "all_quality_batches_terminal": all_terminal,
            "task_stage": (
                "quality_batches_ready"
                if all_terminal
                else "quality_batches_waiting"
            ),
        }

    return read_node


def poll_quality_batches_node(
    reader: QualityBatchReader | None = None,
    *,
    poll_interval_seconds: float = 2.0,
    max_poll_interval_seconds: float = 10.0,
    timeout_seconds: float = 900.0,
):
    """在子图内部轮询 PostgreSQL，直到所有 batch 结束或超时。"""

    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds 必须大于 0")
    if max_poll_interval_seconds < poll_interval_seconds:
        raise ValueError("max_poll_interval_seconds 不能小于 poll_interval_seconds")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds 必须大于 0")

    load_results = reader or _read_quality_batch_results

    async def poll_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        task_id = str(state["task_id"])
        batches = list(state.get("quality_batches", []))
        batch_ids = [str(batch["batch_id"]) for batch in batches]
        deadline = time.monotonic() + timeout_seconds
        interval = poll_interval_seconds

        while True:
            results = await load_results(task_id, batch_ids)
            status_by_batch = {
                item["batch_id"]: item["status"] for item in results
            }
            failures = [
                item for item in results if item["status"] == "failed"
            ]
            all_terminal = len(results) == len(batch_ids) and all(
                item["status"] in {"completed", "failed"} for item in results
            )
            if all_terminal:
                return {
                    "quality_batch_results": results,
                    "quality_batch_status": status_by_batch,
                    "quality_batch_failures": failures,
                    "all_quality_batches_terminal": True,
                    "quality_batches_timed_out": False,
                    "task_stage": "quality_batches_ready",
                }
            if time.monotonic() >= deadline:
                return {
                    "quality_batch_results": results,
                    "quality_batch_status": status_by_batch,
                    "quality_batch_failures": failures,
                    "all_quality_batches_terminal": False,
                    "quality_batches_timed_out": True,
                    "task_stage": "quality_evaluation_timeout",
                }
            await asyncio.sleep(interval)
            interval = min(interval * 1.5, max_poll_interval_seconds)

    return poll_node


def _group_result_from_batch(
    *,
    task_id: str,
    batch: Mapping[str, Any],
) -> GroupEvaluationResult:
    """将批次表中的 JSON 载荷恢复为质量评估领域结果。"""

    try:
        job = QualityEvaluationJob(
            task_id=task_id,
            group_id=str(batch["group_id"]),
            study_type=StudyType(str(batch["study_type"])),
            article_ids=[str(article_id) for article_id in batch["article_ids"]],
        )
        return GroupEvaluationResult.from_dict(
            {
                "job": job.model_dump(mode="json"),
                "assessment": batch["assessment_payload"],
                "verification": batch["verification_payload"],
                "verified_scores": batch["score_payload"],
                "retained_article_ids": [],
                "excluded_article_ids": [],
            }
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"批次 {batch.get('batch_id', '<unknown>')} 的质量评估结果结构不合法"
        ) from exc


def _article_result_payload(
    *,
    article: Any,
    study_type: StudyType,
    score: Any,
    quality_assessment: Any,
    relevance_assessment: Any,
) -> dict[str, Any]:
    """组合主图和 summarizer 所需的文章元数据、分数和评分概述。"""

    evidence_gaps = list(getattr(quality_assessment, "evidence_gaps", []))
    evidence_gaps.extend(getattr(relevance_assessment, "evidence_gaps", []))
    return {
        "article_id": str(article.article_id),
        "study_type": study_type.value,
        "title": article.title,
        "authors": article.authors,
        "first_author": article.first_author,
        "publication_year": article.publication_year,
        "abstract": article.abstract,
        "score": {
            "relevance_score": score.relevance_score,
            "quality_score": score.quality_score,
            "answerability_score": score.answerability_score,
            "journal_score": score.journal_score,
            "recency_score": score.recency_score,
            "composite_score": score.composite_score,
        },
        "score_summary": {
            "strengths": list(getattr(quality_assessment, "strengths", [])),
            "limitations": list(getattr(quality_assessment, "limitations", [])),
            "evidence_gaps": evidence_gaps,
            "relevance_reason": getattr(relevance_assessment, "reason", ""),
            "answerability_reason": getattr(
                relevance_assessment,
                "answerability_reason",
                "",
            ),
            "verification_issues": list(score.verification_issues),
        },
    }


def organize_quality_results_node(
    *,
    hydrator: QualityBatchHydrator | None = None,
    threshold: float = 3.0,
):
    """筛除低分文章并整理成主图可消费的完整文章对象。"""

    if threshold < 0:
        raise ValueError("质量评估筛选阈值不能小于 0")

    if hydrator is None:
        article_repository = ArticleRepository(_get_batch_session_factory())

        async def hydrate(
            job: QualityEvaluationJob,
            user_query: str,
        ) -> HydratedArticleBatch:
            return await article_repository.hydrate(job, user_query=user_query)

        load_articles: QualityBatchHydrator = hydrate
    else:
        load_articles = hydrator

    async def organize_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        retained_articles: list[dict[str, Any]] = []
        excluded_articles: list[dict[str, Any]] = []
        failures = list(state.get("quality_batch_failures", []))
        task_id = str(state["task_id"])
        user_query = str(state.get("user_query", "")).strip() or "质量评估任务"

        for batch in state.get("quality_batch_results", []):
            if batch.get("status") != "completed":
                continue
            try:
                group = _group_result_from_batch(task_id=task_id, batch=batch)
                decision = partition_by_threshold(
                    group.verified_scores,
                    threshold=threshold,
                )
                hydrated = await load_articles(group.job, user_query)
                article_by_id = {
                    article.article_id: article for article in hydrated.articles
                }
                relevance_by_id = {
                    item.article_id: item
                    for item in group.assessment.relevance_assessments
                }
                quality_by_id = {
                    item.article_id: item
                    for item in group.assessment.quality_assessments
                }
                for score in decision.retained:
                    retained_articles.append(
                        _article_result_payload(
                            article=article_by_id[score.article_id],
                            study_type=group.job.study_type,
                            score=score,
                            quality_assessment=quality_by_id[score.article_id],
                            relevance_assessment=relevance_by_id[score.article_id],
                        )
                    )
                for score in decision.excluded:
                    excluded_articles.append(
                        _article_result_payload(
                            article=article_by_id[score.article_id],
                            study_type=group.job.study_type,
                            score=score,
                            quality_assessment=quality_by_id[score.article_id],
                            relevance_assessment=relevance_by_id[score.article_id],
                        )
                    )
            except Exception as exc:
                failures.append(
                    {
                        **batch,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )

        return {
            "retained_articles": retained_articles,
            "excluded_articles": excluded_articles,
            "quality_batch_failures": failures,
            "quality_screening_threshold": threshold,
            "task_stage": "quality_screened",
        }

    return organize_node


def rank_and_limit_quality_candidates_node(
    *,
    max_candidates_before_truncation: int = 50,
    max_candidates_for_summarizer: int = 25,
):
    """对通过筛选的文章稳定排序，并限制发送给主图的候选窗口。"""

    if max_candidates_before_truncation < 1:
        raise ValueError("max_candidates_before_truncation 必须大于 0")
    if max_candidates_for_summarizer < 1:
        raise ValueError("max_candidates_for_summarizer 必须大于 0")
    if max_candidates_for_summarizer > max_candidates_before_truncation:
        raise ValueError("max_candidates_for_summarizer 不能大于截断阈值")

    async def rank_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        retained = list(state.get("retained_articles", []))
        seen: set[str] = set()
        for article in retained:
            article_id = str(article.get("article_id", ""))
            if not article_id:
                raise ValueError("保留候选缺少 article_id")
            if article_id in seen:
                raise ValueError(f"保留候选 article_id 重复: {article_id}")
            seen.add(article_id)

        def sort_key(article: Mapping[str, Any]) -> tuple[Any, ...]:
            score = article.get("score", {})
            composite = float(
                article.get(
                    "corrected_composite_score",
                    score.get("composite_score", 0.0),
                )
            )
            coverage = float(article.get("coverage_contribution_score", 0.0))
            year = article.get("publication_year")
            year_value = int(year) if year is not None else 0
            return (
                -composite,
                -coverage,
                year is None,
                -year_value,
                str(article["article_id"]),
            )

        ranked = sorted(retained, key=sort_key)
        should_truncate = len(ranked) > max_candidates_before_truncation
        selected = (
            ranked[:max_candidates_for_summarizer]
            if should_truncate
            else ranked
        )
        truncated = ranked[len(selected) :] if should_truncate else []
        return {
            "ranked_articles": selected,
            "truncated_candidates": truncated,
            "quality_candidates_truncated": should_truncate,
            "quality_candidate_limit": (
                max_candidates_for_summarizer if should_truncate else len(selected)
            ),
            "task_stage": "quality_candidates_ranked",
        }

    return rank_node


def _fallback_quality_summary(article: Mapping[str, Any]) -> dict[str, Any]:
    """评分理由 LLM 不可用时，使用 Worker 已有字段生成保守降级结果。"""

    raw = article.get("score_summary", {})
    if not isinstance(raw, Mapping):
        raw = {}
    dimension_reasons = []
    for dimension, key in (
        ("relevance", "relevance_reason"),
        ("answerability", "answerability_reason"),
    ):
        reason = str(raw.get(key, "")).strip()
        if reason:
            dimension_reasons.append({"dimension": dimension, "summary": reason})
    strengths = [str(item) for item in raw.get("strengths", [])]
    limitations = [str(item) for item in raw.get("limitations", [])]
    evidence_gaps = [str(item) for item in raw.get("evidence_gaps", [])]
    critical_defects = [str(item) for item in raw.get("verification_issues", [])]
    parts = [*strengths, *limitations, *evidence_gaps]
    return {
        "article_id": str(article["article_id"]),
        "summary_status": "fallback",
        "dimension_reasons": dimension_reasons,
        "strengths": strengths,
        "limitations": limitations,
        "critical_defects": critical_defects,
        "evidence_gaps": evidence_gaps,
        "summary_text": "；".join(parts) or "评分理由概括暂不可用。",
    }


def _normalize_quality_summary(
    summary: Any,
    *,
    expected_article_id: str,
) -> dict[str, Any]:
    if hasattr(summary, "model_dump"):
        summary = summary.model_dump(mode="json")
    if not isinstance(summary, Mapping):
        raise ValueError("评分理由整理结果必须是对象")
    result = dict(summary)
    if str(result.get("article_id", "")) != expected_article_id:
        raise ValueError("评分理由整理结果的 article_id 与输入不一致")
    dimension_reasons = result.get("dimension_reasons", [])
    if not isinstance(dimension_reasons, list):
        raise ValueError("评分理由整理结果字段 dimension_reasons 格式不合法")
    normalized_reasons: list[dict[str, str]] = []
    for item in dimension_reasons:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="json")
        if not isinstance(item, Mapping):
            raise ValueError("评分理由整理结果字段 dimension_reasons 格式不合法")
        dimension = str(item.get("dimension", "")).strip()
        explanation = str(item.get("summary", "")).strip()
        if not dimension or not explanation:
            raise ValueError("评分理由整理结果字段 dimension_reasons 格式不合法")
        normalized_reasons.append({"dimension": dimension, "summary": explanation})
    result["dimension_reasons"] = normalized_reasons
    for field in ("strengths", "limitations", "critical_defects", "evidence_gaps"):
        value = result.get(field, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"评分理由整理结果字段 {field} 格式不合法")
        result[field] = value
    result["summary_status"] = str(result.get("summary_status", "completed"))
    result["summary_text"] = str(result.get("summary_text", "")).strip()
    if not result["summary_text"]:
        raise ValueError("评分理由整理结果缺少 summary_text")
    return result


def summarize_quality_reasons_node(
    summarizer: QualityReasonSummarizer | None = None,
    *,
    batch_size: int = 10,
):
    """让 LLM 归纳评分理由；失败时回退到 Worker 已有结构化理由。"""

    if batch_size < 1:
        raise ValueError("评分理由整理 batch_size 必须大于 0")

    async def summarize_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        articles = list(state.get("ranked_articles", []))
        updated_articles: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        failures = list(state.get("quality_reason_failures", []))
        for start in range(0, len(articles), batch_size):
            chunk = articles[start : start + batch_size]
            chunk_summaries: list[dict[str, Any]]
            if summarizer is None:
                chunk_summaries = [_fallback_quality_summary(article) for article in chunk]
            else:
                try:
                    response = summarizer(chunk)
                    if inspect.isawaitable(response):
                        response = await response
                    if not isinstance(response, Sequence) or isinstance(response, (str, bytes)):
                        raise ValueError("评分理由整理器必须返回对象列表")
                    if len(response) != len(chunk):
                        raise ValueError("评分理由整理结果未完整覆盖当前 batch")
                    chunk_summaries = [
                        _normalize_quality_summary(
                            item,
                            expected_article_id=str(article["article_id"]),
                        )
                        for article, item in zip(chunk, response, strict=True)
                    ]
                except Exception as exc:
                    failures.append(
                        {
                            "article_ids": [str(article["article_id"]) for article in chunk],
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        }
                    )
                    chunk_summaries = [_fallback_quality_summary(article) for article in chunk]

            for article, summary in zip(chunk, chunk_summaries, strict=True):
                updated = {**article, "quality_summary": summary}
                updated_articles.append(updated)
                summaries.append(summary)

        return {
            "ranked_articles": updated_articles,
            "quality_reason_summaries": summaries,
            "quality_reason_failures": failures,
            "task_stage": "quality_reasons_summarized",
        }

    return summarize_node


def quality_evaluation_exit_node(
    *,
    parent_success_node: str | None = None,
    parent_timeout_node: str | None = None,
):
    """将子图最终结果返回父图；未嵌入父图时返回普通 state 更新。"""

    async def exit_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any] | Command:
        update = {
            "retained_candidates": list(state.get("retained_articles", [])),
            "excluded_candidates": list(state.get("excluded_articles", [])),
            "truncated_candidates": list(state.get("truncated_candidates", [])),
            "quality_candidates_truncated": bool(
                state.get("quality_candidates_truncated", False)
            ),
            "quality_evaluation_failures": list(
                state.get("quality_batch_failures", [])
            ),
            "ranked_candidates": [
                {
                    "article_id": article["article_id"],
                    "title": article.get("title"),
                    "authors": article.get("authors", []),
                    "first_author": article.get("first_author"),
                    "publication_year": article.get("publication_year"),
                    "abstract": article.get("abstract"),
                    "study_type": article.get("study_type"),
                    "corrected_composite_score": article["score"][
                        "composite_score"
                    ],
                    "coverage_contribution_score": float(
                        article.get("coverage_contribution_score", 0.0)
                    ),
                    "quality_summary": article.get(
                        "quality_summary",
                        _fallback_quality_summary(article),
                    ),
                    "verification_issues": article["score_summary"][
                        "verification_issues"
                    ],
                }
                for article in state.get(
                    "ranked_articles",
                    state.get("retained_articles", []),
                )
            ],
            "portfolio_evaluation": state.get("portfolio_evaluation"),
            "quality_reason_failures": list(
                state.get("quality_reason_failures", [])
            ),
            "quality_candidate_limit": state.get("quality_candidate_limit", 0),
            "task_stage": state.get("task_stage", "quality_screened"),
        }
        target = (
            parent_timeout_node
            if state.get("quality_batches_timed_out")
            else parent_success_node
        )
        if target is None:
            return update
        return Command(
            graph=Command.PARENT,
            goto=target,
            update=update,
        )

    return exit_node


def _quality_node_input_metrics(
    name: str,
    state: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "task_id": state.get("task_id"),
        "node": name,
        "search_result_count": len(state.get("search_results", [])),
        "quality_group_count": len(state.get("quality_groups", [])),
        "quality_batch_count": len(state.get("quality_batches", [])),
        "completed_batch_count": sum(
            batch.get("status") == "completed"
            for batch in state.get("quality_batch_results", [])
            if isinstance(batch, Mapping)
        ),
        "failed_batch_count": len(state.get("quality_batch_failures", [])),
    }


def _quality_node_output_metrics(
    name: str,
    result: Any,
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"node": name, "result_type": type(result).__name__}
    return {
        "node": name,
        "task_stage": result.get("task_stage"),
        "group_count": len(result.get("quality_groups", [])),
        "batch_count": len(result.get("quality_batches", [])),
        "batch_result_count": len(result.get("quality_batch_results", [])),
        "batch_failure_count": len(result.get("quality_batch_failures", [])),
        "batches_timed_out": bool(result.get("quality_batches_timed_out", False)),
        "retained_article_count": len(result.get("retained_articles", [])),
        "excluded_article_count": len(result.get("excluded_articles", [])),
        "ranked_article_count": len(result.get("ranked_articles", [])),
        "truncated_candidate_count": len(result.get("truncated_candidates", [])),
        "quality_candidates_truncated": bool(
            result.get("quality_candidates_truncated", False)
        ),
        "reason_failure_count": len(result.get("quality_reason_failures", [])),
        "screening_threshold": result.get("quality_screening_threshold"),
    }


def build_quality_evaluation_subgraph(
    *,
    work_cache: RedisWorkCache | None = None,
    max_batch_size: int = 10,
    checkpointer: Any | None = None,
    dispatcher: QualityBatchDispatcher | None = None,
    reader: QualityBatchReader | None = None,
    hydrator: QualityBatchHydrator | None = None,
    reason_summarizer: QualityReasonSummarizer | None = None,
    screening_threshold: float = 3.0,
    max_candidates_before_truncation: int = 50,
    max_candidates_for_summarizer: int = 25,
    reason_summary_batch_size: int = 10,
    poll_interval_seconds: float = 2.0,
    max_poll_interval_seconds: float = 10.0,
    wait_timeout_seconds: float = 900.0,
    parent_success_node: str | None = None,
    parent_timeout_node: str | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """编译质量评估子图的分组、批次提交和结果读取阶段。

    ``dispatcher`` 与 ``reader`` 用于测试替换；生产环境默认分别使用
    Celery Broker 和 PostgreSQL 批次表。
    """

    def add_observed_node(name: str, node: Callable[..., Any]) -> None:
        if langfuse_runtime is None:
            graph.add_node(name, node)
            return
        graph.add_node(
            name,
            langfuse_runtime.wrap_node(
                name=name.replace("_", "-"),
                as_type="agent" if name == "submit_quality_batches" else "chain",
                node=node,
                input_metrics=lambda state: _quality_node_input_metrics(name, state),
                output_metrics=lambda result: _quality_node_output_metrics(name, result),
            ),
        )

    graph = StateGraph(QualityEvaluationSubgraphState)
    add_observed_node("quality_grouping", quality_grouping_node(work_cache))
    add_observed_node(
        "quality_batcher",
        quality_batcher_node(max_batch_size=max_batch_size),
    )
    graph.add_edge(START, "quality_grouping")
    graph.add_edge("quality_grouping", "quality_batcher")
    add_observed_node(
        "submit_quality_batches",
        submit_quality_batches_node(dispatcher),
    )
    add_observed_node(
        "poll_quality_batches",
        poll_quality_batches_node(
            reader,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_interval_seconds=max_poll_interval_seconds,
            timeout_seconds=wait_timeout_seconds,
        ),
    )
    add_observed_node(
        "organize_quality_results",
        organize_quality_results_node(
            hydrator=hydrator,
            threshold=screening_threshold,
        ),
    )
    add_observed_node(
        "rank_and_limit_quality_candidates",
        rank_and_limit_quality_candidates_node(
            max_candidates_before_truncation=max_candidates_before_truncation,
            max_candidates_for_summarizer=max_candidates_for_summarizer,
        ),
    )
    add_observed_node(
        "summarize_quality_reasons",
        summarize_quality_reasons_node(
            reason_summarizer,
            batch_size=reason_summary_batch_size,
        ),
    )
    add_observed_node(
        "quality_evaluation_exit",
        quality_evaluation_exit_node(
            parent_success_node=parent_success_node,
            parent_timeout_node=parent_timeout_node,
        ),
    )
    graph.add_edge("quality_batcher", "submit_quality_batches")
    graph.add_edge("submit_quality_batches", "poll_quality_batches")
    graph.add_edge("poll_quality_batches", "organize_quality_results")
    graph.add_edge("organize_quality_results", "rank_and_limit_quality_candidates")
    graph.add_edge(
        "rank_and_limit_quality_candidates",
        "summarize_quality_reasons",
    )
    graph.add_edge("summarize_quality_reasons", "quality_evaluation_exit")
    graph.add_edge("quality_evaluation_exit", END)
    return graph.compile(checkpointer=checkpointer)


__all__ = [
    "QualityEvaluationSubgraphState",
    "group_quality_articles",
    "quality_grouping_node",
    "quality_batcher_node",
    "rank_and_limit_quality_candidates_node",
    "summarize_quality_reasons_node",
    "build_quality_evaluation_subgraph",
    "record_article_id",
    "split_quality_batch",
    "study_group_id",
]
