"""单篇 Send 编排：持久化输入、共享并发、结果对账与集合收敛。"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date
from typing import Annotated, Any, TypedDict
from uuid import UUID, NAMESPACE_URL, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from backend.agent.state import EvidenceState
from backend.app.cache import RedisWorkCache
from backend.app.observability import LangfuseRuntime
from backend.app.core.search_results_normalizer import infer_study_design
from backend.skills.quality_evaluator.execution import BatchExecutor, ModelRequestLimiter
from backend.skills.quality_evaluator.schema import HydratedArticleBatch, QualityEvaluationJob, StudyType, PortfolioEvaluation, article_assessment_model_for
from backend.skills.quality_evaluator.scoring import ArticleScore, rank_candidates


def merge_batch_refs(left: list[dict], right: list[dict]) -> list[dict]:
    return list({item["batch_id"]: item for item in [*left, *right]}.values())


class QualityEvaluationSubgraphState(TypedDict, total=False):
    task_id: str
    user_query: str
    intent_analysis: dict
    search_plan: dict
    search_results: list[dict]
    article_groups: dict
    quality_groups: list[dict]
    unsupported_quality_groups: list[dict]
    quality_batches: list[dict]
    execution_id: str
    quality_batch_results: Annotated[list[dict], merge_batch_refs]
    persisted_results: list[dict]
    quality_batch_status: dict
    quality_batch_failures: list[dict]
    all_quality_batches_terminal: bool
    retained_articles: list[dict]
    excluded_articles: list[dict]
    ranked_articles: list[dict]
    truncated_candidates: list[dict]
    quality_candidates_truncated: bool
    quality_reason_summaries: list[dict]
    quality_reason_failures: list[dict]
    quality_candidate_limit: int
    portfolio_evaluation: dict | None
    portfolio_degraded: bool
    portfolio_order: list[str]
    quality_screening_threshold: float
    quality_batches_timed_out: bool
    retained_candidates: list[dict]
    excluded_candidates: list[dict]
    quality_evaluation_failures: list[dict]
    ranked_candidates: list[dict]
    task_stage: str


QualityBatchHydrator = Callable[..., Awaitable[HydratedArticleBatch]]
QualityReasonSummarizer = Callable[[Sequence[Mapping[str, Any]]], Awaitable[Sequence[Mapping[str, Any]]]]


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


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
    inferred = infer_study_design(
        study_design=study_design or None,
        publication_types=list(record.get("publication_types", [])),
        abstract=record.get("abstract"),
    )
    if inferred:
        return inferred
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
    max_batch_size: int = 1,
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


def quality_batcher_node(*, max_batch_size: int = 1):
    """创建质量评估子图的批次切分节点。"""

    configured_size = int(os.getenv("QUALITY_BATCH_SIZE", str(max_batch_size)))
    if configured_size != 1:
        raise ValueError("当前质量评估要求 QUALITY_BATCH_SIZE=1")

    async def batch_node(
        state: QualityEvaluationSubgraphState | EvidenceState,
    ) -> dict[str, Any]:
        supported_groups: list[dict[str, Any]] = []
        unsupported_groups: list[dict[str, Any]] = []
        for group in state.get("quality_groups", []):
            try:
                StudyType(str(group["study_design"]))
            except (KeyError, TypeError, ValueError):
                unsupported_groups.append(
                    {
                        **dict(group),
                        "status": "not_evaluated",
                        "reason": "unsupported_study_design",
                    }
                )
            else:
                supported_groups.append(dict(group))
        batches = [
            batch
            for group in supported_groups
            for batch in split_quality_batch(
                group,
                max_batch_size=configured_size,
            )
        ]
        return {
            "quality_batches": batches,
            "unsupported_quality_groups": unsupported_groups,
            "task_stage": "quality_batches_created",
        }

    return batch_node


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
        "evidence_spans": getattr(article, "evidence_spans", {}),
        "evidence_span_version": getattr(article, "evidence_span_version", None),
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
        },
        "quality_assessment": quality_assessment.model_dump(mode="json"),
        "relevance_assessment": relevance_assessment.model_dump(mode="json"),
    }


def prepare_quality_plan_node(runtime: Any, repository: Any, hydrator: Any = None):
    """一次读取每个分组，冻结影响评价的输入；登记成功后才允许分发。"""
    async def prepare(state):
        inputs = []
        load = hydrator or runtime.repository.hydrate
        hydration = {}
        unsupported = {g["group_id"] for g in state["unsupported_quality_groups"]}
        for group in state["quality_groups"]:
            if group["group_id"] in unsupported:
                continue
            # 数据库 hydrate 可合并读取；每个持久化评估输入仍拆成单篇。
            batch_size = int(os.getenv("QUALITY_BATCH_SIZE", "1"))
            if batch_size != 1:
                raise ValueError("当前质量评估要求 QUALITY_BATCH_SIZE=1")
            for chunk in split_quality_batch(group, max_batch_size=5):
                job = QualityEvaluationJob(task_id=state["task_id"], group_id=group["group_id"],
                    study_type=StudyType(group["study_design"]), article_ids=chunk["article_ids"])
                started = time.perf_counter()
                hydrated = await load(job, user_query=state["user_query"])
                elapsed = round((time.perf_counter() - started) * 1000, 3)
                for article in hydrated.articles:
                    single_job = job.model_copy(update={"article_ids": [article.article_id]})
                    batch = HydratedArticleBatch(job=single_job, user_query=hydrated.user_query, articles=[article])
                    inputs.append({"kind": "article", "evaluator_version": runtime.contract_version, "batch": batch.model_dump(mode="json"),
                        "intent_analysis": state.get("intent_analysis", {}), "search_plan": state.get("search_plan", {})})
                    hydration[str(article.article_id)] = {"hydrate_batch_ms": elapsed, "hydrate_batch_size": len(hydrated.articles), "attempts": []}
        # 配置或评价规则变化不能复用旧评价。
        version = {"evaluator_version": runtime.contract_version, "inputs": inputs}
        execution_id = str(uuid5(NAMESPACE_URL, _fingerprint(version)))
        batches = []
        for snapshot in inputs:
            job = snapshot["batch"]["job"]
            batches.append({"batch_id": f"{execution_id}:{job['article_ids'][0]}",
                "group_id": job["group_id"], "study_design": job["study_type"],
                "article_ids": job["article_ids"], "input_payload": snapshot,
                "metrics_payload": hydration[job["article_ids"][0]]})
        await repository.register_plan(state["task_id"], execution_id, batches)
        return {"execution_id": execution_id, "quality_batches": [
            {k: v for k, v in b.items() if k not in {"input_payload", "metrics_payload"}} for b in batches],
            "task_stage": "quality_plan_registered"}
    return prepare


def fork_quality_experts():
    def route(state):
        queues = {}
        for batch in state["quality_batches"]:
            queues.setdefault(batch["study_design"], deque()).append(batch)
        sends = []
        while queues:
            for kind in list(queues):
                batch = queues[kind].popleft()
                sends.append(Send(f"expert_{kind}", {"task_id": state["task_id"],
                    "batch_id": batch["batch_id"], "study_type": kind}))
                if not queues[kind]:
                    del queues[kind]
        return sends or "collect_expert_results"
    return route


def collect_expert_results_node(repository):
    """以完整预期集合对账；节点回传的引用不作为结果事实。"""
    async def collect(state):
        ordered_ids = [b["batch_id"] for b in state["quality_batches"]]
        expected = set(ordered_ids)
        rows = await repository.read_batches(state["task_id"], ordered_ids)
        actual = {r["batch_id"] for r in rows}
        if actual != expected:
            raise RuntimeError("质量评估持久化计划缺失，无法完成对账")
        by_id = {r["batch_id"]: r for r in rows}
        rows = [by_id[key] for key in ordered_ids]
        unfinished = [r for r in rows if r["status"] in {"pending", "processing"}]
        if unfinished:
            from datetime import datetime, timezone
            if any(r["status"] == "processing" and r["lease_expires_at"] > datetime.now(timezone.utc) for r in unfinished):
                raise RuntimeError("质量评估批次仍持有有效租约，请在到期后恢复任务")
            return Command(goto=[Send(f"expert_{r['study_type']}", {"task_id": state["task_id"],
                "batch_id": r["batch_id"], "study_type": r["study_type"]}) for r in unfinished])
        failures = [{"batch_id": r["batch_id"], "article_ids": r["article_ids"],
            "status": "not_evaluated", "error_type": r["error_type"]} for r in rows if r["status"] != "completed"]
        failures.extend(state.get("unsupported_quality_groups", []))
        if expected and not any(r["status"] == "completed" for r in rows):
            raise RuntimeError("全部文章质量评估失败，无法生成推荐")
        # 只把 JSON 快照送入 checkpoint，避免 ORM 时间戳泄漏到图契约。
        return Command(goto="organize_quality_results", update={"persisted_results": [{k: r[k] for k in ("batch_id", "status", "input_payload", "assessment_payload", "score_payload")} for r in rows],
            "quality_batch_failures": failures, "quality_batch_status": {r["batch_id"]: r["status"] for r in rows},
            "all_quality_batches_terminal": True, "task_stage": "quality_batches_ready"})
    return collect


def organize_quality_results_node(*, threshold=3.0):
    async def organize(state):
        retained, excluded = [], []
        for row in state["persisted_results"]:
            if row["status"] != "completed":
                continue
            batch = HydratedArticleBatch.model_validate(row["input_payload"]["batch"])
            assessment = article_assessment_model_for(batch.job.study_type).model_validate(row["assessment_payload"])
            relevance = {a.article_id: a for a in assessment.relevance_assessments}
            quality = {a.article_id: a for a in assessment.quality_assessments}
            articles = {a.article_id: a for a in batch.articles}
            for payload in row["score_payload"]:
                score = ArticleScore(**{**payload, "article_id": UUID(payload["article_id"])})
                article = _article_result_payload(article=articles[score.article_id], study_type=batch.job.study_type,
                    score=score, quality_assessment=quality[score.article_id], relevance_assessment=relevance[score.article_id])
                published = articles[score.article_id].publication_date
                article["publication_date"] = published.isoformat() if published else None
                (retained if score.composite_score > threshold else excluded).append(article)
        return {"retained_articles": retained, "excluded_articles": excluded,
            "quality_screening_threshold": threshold, "task_stage": "quality_screened"}
    return organize


def evaluate_portfolio_node(executor, repository):
    async def portfolio(state):
        candidates = state["retained_articles"]
        if not candidates:
            return {"portfolio_evaluation": None, "portfolio_degraded": False}

        def portfolio_candidate(article: Mapping[str, Any]) -> dict[str, Any]:
            quality = dict(article["quality_assessment"])
            quality.pop("article_id", None)
            quality.pop("study_type", None)
            quality["criteria"] = [
                {key: value for key, value in criterion.items() if key != "evidence_quote"}
                for criterion in quality["criteria"]
            ]
            relevance = dict(article["relevance_assessment"])
            relevance.pop("article_id", None)
            return {
                "article_id": article["article_id"],
                "title": article["title"],
                "abstract": article["abstract"],
                "evidence_spans": article.get("evidence_spans", {}),
                "study_type": article["study_type"],
                "publication_year": article["publication_year"],
                "publication_date": article["publication_date"],
                "composite_score": article["score"]["composite_score"],
                "quality_assessment": quality,
                "relevance_assessment": relevance,
            }

        payload = {"task_id": state["task_id"], "user_query": state["user_query"],
            "intent_analysis": state.get("intent_analysis", {}), "search_plan": state.get("search_plan", {}),
            "candidate_articles": [portfolio_candidate(a) for a in candidates]}
        # 日期统一转 JSON，数据库与模型使用同一冻结输入。
        payload = json.loads(json.dumps(payload, default=str))
        batch_id = f"{state['execution_id']}:portfolio:{_fingerprint([payload, executor.runtime.prompt_loader.portfolio_prompt(), PortfolioEvaluation.model_json_schema()])[:16]}"
        await repository.register_plan(state["task_id"], state["execution_id"], [{"batch_id": batch_id,
            "group_id": "portfolio", "study_design": "portfolio", "article_ids": [a["article_id"] for a in candidates],
            "input_payload": {"kind": "portfolio", "input": payload}}])
        row = await executor.execute(state["task_id"], batch_id)
        if row["status"] != "completed":
            return {"portfolio_evaluation": None, "portfolio_degraded": True,
                "quality_batch_failures": [*state["quality_batch_failures"],
                {"stage": "portfolio", "status": "degraded", "error_type": row["error_type"] or "not_completed"}]}
        result = PortfolioEvaluation.model_validate(row["assessment_payload"])
        scores = [ArticleScore(article_id=UUID(a["article_id"]), **a["score"]) for a in candidates]
        dates = {UUID(a["article_id"]): date.fromisoformat(str(a["publication_date"])) if a["publication_date"] else None for a in candidates}
        ranked = rank_candidates(scores, result.article_contributions, publication_dates=dates)
        by_id = {str(c.article_id): c.coverage_contribution_score for c in result.article_contributions}
        return {"portfolio_evaluation": result.model_dump(mode="json"),
            "portfolio_degraded": False,
            "portfolio_order": [str(a.article_id) for a in ranked],
            "retained_articles": [{**a, "coverage_contribution_score": by_id[a["article_id"]]} for a in candidates],
            "task_stage": "quality_portfolio_evaluated"}
    return portfolio


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

        if state.get("portfolio_evaluation"):
            order = state["portfolio_order"]
            by_id = {a["article_id"]: a for a in retained}
            ranked = [by_id[article_id] for article_id in order]
        else:
            ranked = sorted(retained, key=lambda a: (-a["score"]["composite_score"], a["article_id"]))
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
    """评分理由 LLM 不可用时，使用 已完成评价字段生成保守降级结果。"""

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
    critical_defects: list[str] = []
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
    """整理已有评分理由；可选摘要器失败时保留原始理由。"""

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
                    "composite_score": article["score"][
                        "composite_score"
                    ],
                    "coverage_contribution_score": article.get("coverage_contribution_score"),
                    "quality_summary": article.get(
                        "quality_summary",
                        _fallback_quality_summary(article),
                    ),
                }
                for article in state.get(
                    "ranked_articles",
                    state.get("retained_articles", []),
                )
            ],
            "portfolio_evaluation": state.get("portfolio_evaluation"),
            "portfolio_degraded": bool(state.get("portfolio_degraded", False)),
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


def build_quality_evaluation_subgraph(
    *,
    evaluator_runtime: Any,
    batch_repository: Any,
    work_cache: RedisWorkCache | None = None,
    checkpointer: Any | None = None,
    hydrator: QualityBatchHydrator | None = None,
    reason_summarizer: QualityReasonSummarizer | None = None,
    screening_threshold: float = 3.0,
    max_candidates_before_truncation: int = 50,
    max_candidates_for_summarizer: int = 25,
    reason_summary_batch_size: int = 10,
    expert_concurrency: int = 5,
    parent_success_node: str | None = None,
    parent_timeout_node: str | None = None,
    langfuse_runtime: LangfuseRuntime | None = None,
):
    """一个应用图共享一个请求限流器；同类型文章也可同时运行。"""
    from .specialized_evaluators import create_specialized_expert_node

    if evaluator_runtime is None or batch_repository is None:
        raise ValueError("质量评估需要 evaluator_runtime 和 batch_repository")
    if int(os.getenv("QUALITY_BATCH_SIZE", "1")) != 1:
        raise ValueError("单篇评估要求 QUALITY_BATCH_SIZE=1")
    concurrency = int(os.getenv("QUALITY_EXPERT_CONCURRENCY", str(expert_concurrency)))
    executor = BatchExecutor(
        evaluator_runtime, batch_repository, ModelRequestLimiter(concurrency),
        threshold=screening_threshold,
    )
    portfolio_concurrency = int(os.getenv("QUALITY_PORTFOLIO_CONCURRENCY", "1"))
    portfolio_deadline = float(os.getenv("QUALITY_PORTFOLIO_DEADLINE_SECONDS", "90"))
    portfolio_executor = BatchExecutor(
        evaluator_runtime, batch_repository, ModelRequestLimiter(portfolio_concurrency),
        threshold=screening_threshold, deadline_seconds=portfolio_deadline,
    )
    graph = StateGraph(QualityEvaluationSubgraphState)

    def add(name: str, node: Callable[..., Any]) -> None:
        if langfuse_runtime is not None:
            node = langfuse_runtime.wrap_node(
                name=name.replace("_", "-"), as_type="chain", node=node,
                input_metrics=lambda state: {"task_id": state.get("task_id")},
                output_metrics=lambda result: (
                    {"stage": result.get("task_stage")} if isinstance(result, dict) else {}
                ),
            )
        graph.add_node(name, node)

    add("quality_grouping", quality_grouping_node(work_cache))
    add("quality_batcher", quality_batcher_node())
    add("prepare_quality_plan", prepare_quality_plan_node(
        evaluator_runtime, batch_repository, hydrator,
    ))
    for kind in StudyType:
        add(f"expert_{kind.value}", create_specialized_expert_node(kind, executor))
        graph.add_edge(f"expert_{kind.value}", "collect_expert_results")
    add("collect_expert_results", collect_expert_results_node(batch_repository))
    add("organize_quality_results", organize_quality_results_node(threshold=screening_threshold))
    add("evaluate_portfolio", evaluate_portfolio_node(portfolio_executor, batch_repository))
    add("rank_and_limit_quality_candidates", rank_and_limit_quality_candidates_node(
        max_candidates_before_truncation=max_candidates_before_truncation,
        max_candidates_for_summarizer=max_candidates_for_summarizer,
    ))
    add("summarize_quality_reasons", summarize_quality_reasons_node(
        summarizer=reason_summarizer, batch_size=reason_summary_batch_size,
    ))
    add("quality_evaluation_exit", quality_evaluation_exit_node(
        parent_success_node=parent_success_node, parent_timeout_node=parent_timeout_node,
    ))
    graph.add_edge(START, "quality_grouping")
    graph.add_edge("quality_grouping", "quality_batcher")
    graph.add_edge("quality_batcher", "prepare_quality_plan")
    graph.add_conditional_edges("prepare_quality_plan", fork_quality_experts())
    flow = [
        "organize_quality_results", "evaluate_portfolio",
        "rank_and_limit_quality_candidates", "summarize_quality_reasons",
        "quality_evaluation_exit", END,
    ]
    for source, target in zip(flow, flow[1:]):
        graph.add_edge(source, target)
    return graph.compile(checkpointer=checkpointer)
