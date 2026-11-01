"""在花费 LLM 成本前验证生产持久化结果。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from math import isclose
from typing import Any

from pydantic import ValidationError

from backend.skills.quality_evaluator.rubric_registry import get_rubric
from backend.skills.quality_evaluator.schema import StudyType, article_assessment_model_for
from backend.skills.quality_evaluator.schema import PortfolioEvaluation
from backend.skills.quality_evaluator.evidence import resolve_portfolio_evidence
from backend.skills.quality_evaluator.scoring import calculate_preliminary_quality
from backend.app.core.evidence_spans import resolve_span_ids

from ..config import EvaluationConfig
from ..enums import BatchEligibility, JudgeType
from ..schemas import BatchCheckResult, CheckIssue, DryRunReport, TaskEvaluationSnapshot


def _rubric_hash(study_type: StudyType) -> str:
    rubric = get_rubric(study_type)
    payload = json.dumps(
        {"study_type": rubric.study_type.value, "domain": rubric.domain, "rules": rubric.rules},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _issue(code: str, message: str) -> CheckIssue:
    return CheckIssue(code=code, message=message)


def check_batch(snapshot: TaskEvaluationSnapshot, batch: Any, config: EvaluationConfig) -> BatchCheckResult:
    if batch.status == "failed":
        return BatchCheckResult(batch_id=batch.batch_id, classification=BatchEligibility.FAILED, article_count=len(batch.article_ids))
    if batch.status in {"pending", "processing"}:
        return BatchCheckResult(batch_id=batch.batch_id, classification=BatchEligibility.PENDING, article_count=len(batch.article_ids))

    issues: list[CheckIssue] = []
    if batch.status != "completed":
        issues.append(_issue("unsupported_status", f"不支持的 Batch status：{batch.status}"))

    if batch.study_type == "portfolio":
        payload = (batch.input_payload or {}).get("input", {})
        candidate_articles = payload.get("candidate_articles", [])
        if not candidate_articles:
            issues.append(_issue("missing_portfolio_input", "portfolio 批次缺少冻结候选文章输入"))
        if batch.assessment_payload is None:
            issues.append(_issue("missing_portfolio_payload", "portfolio assessment_payload 为空"))
        elif candidate_articles:
            try:
                data = resolve_portfolio_evidence(dict(batch.assessment_payload), candidate_articles)
                evaluation = PortfolioEvaluation.model_validate(data)
                expected = {str(item["article_id"]) for item in candidate_articles}
                if {str(item) for item in evaluation.candidate_article_ids} != expected:
                    issues.append(_issue("portfolio_articles_mismatch", "portfolio 候选文章集合与冻结输入不一致"))
            except (ValidationError, KeyError, TypeError, ValueError) as exc:
                issues.append(_issue("invalid_portfolio_payload", str(exc)))
        return BatchCheckResult(
            batch_id=batch.batch_id,
            classification=BatchEligibility.INVALID if issues else BatchEligibility.ELIGIBLE,
            article_count=len(batch.article_ids),
            issues=issues,
            rubric_contract_version=None,
            rubric_sha256=None,
        )
    if not batch.article_ids:
        issues.append(_issue("empty_article_ids", "article_ids 不能为空"))
    if len(batch.article_ids) != len(set(batch.article_ids)):
        issues.append(_issue("duplicate_article_ids", "article_ids 存在重复 UUID"))
    missing = sorted(set(batch.article_ids) - set(snapshot.articles), key=str)
    if missing:
        issues.append(_issue("missing_articles", "articles 表缺少：" + ", ".join(map(str, missing))))

    study_type: StudyType | None = None
    rubric_sha256: str | None = None
    try:
        study_type = StudyType(batch.study_type)
        rubric_sha256 = _rubric_hash(study_type)
    except ValueError:
        issues.append(_issue("unknown_study_type", f"未注册 study_type：{batch.study_type}"))

    if batch.assessment_payload is None:
        issues.append(_issue("missing_assessment_payload", "assessment_payload 为空"))
    if batch.score_payload is None:
        issues.append(_issue("missing_score_payload", "score_payload 为空"))

    assessment = None
    if study_type is not None and batch.assessment_payload is not None:
        try:
            assessment = article_assessment_model_for(study_type).model_validate(batch.assessment_payload)
        except ValidationError as exc:
            issues.append(_issue("invalid_assessment_payload", str(exc)))

    if assessment is not None:
        if assessment.task_id != str(snapshot.task.task_id):
            issues.append(_issue("task_id_mismatch", "assessment task_id 与请求 Task 不一致"))
        if assessment.group_id != batch.group_id:
            issues.append(_issue("group_id_mismatch", "assessment group_id 与 Batch 不一致"))
        if assessment.study_type.value != batch.study_type:
            issues.append(_issue("study_type_mismatch", "assessment study_type 与 Batch 不一致"))
        if set(assessment.article_ids) != set(batch.article_ids):
            issues.append(_issue("assessment_articles_mismatch", "assessment article 集合与 Batch 不一致"))
        quality_by_id = {item.article_id: item for item in assessment.quality_assessments}
        frozen_articles = (batch.input_payload or {}).get("batch", {}).get("articles", [])
        frozen_spans = {str(item["article_id"]): item.get("evidence_spans", {}) for item in frozen_articles}
        if frozen_spans:
            for item in assessment.relevance_assessments:
                _validate_span_references(item.evidence_span_ids, str(item.article_id), frozen_spans, issues)
            for item in assessment.quality_assessments:
                for criterion in item.criteria:
                    _validate_span_references(criterion.evidence_span_ids, str(item.article_id), frozen_spans, issues)
        score_rows = batch.score_payload or []
        score_ids = []
        for row in score_rows:
            try:
                article_id = next(key for key in quality_by_id if str(key) == str(row["article_id"]))
                stored = float(row["quality_score"])
                calculated = calculate_preliminary_quality(item.score for item in quality_by_id[article_id].criteria)
                if not isclose(stored, calculated, abs_tol=1e-9):
                    issues.append(_issue("quality_score_mismatch", f"article {article_id}：stored={stored}, calculated={calculated}"))
                score_ids.append(article_id)
            except (KeyError, TypeError, ValueError, StopIteration) as exc:
                issues.append(_issue("invalid_score_payload", f"score row 不合法：{exc}"))
        if len(score_ids) != len(set(score_ids)):
            issues.append(_issue("duplicate_score_articles", "score_payload article_id 重复"))
        if set(score_ids) != set(batch.article_ids):
            issues.append(_issue("score_articles_mismatch", "score_payload article 集合与 Batch 不一致"))

    return BatchCheckResult(
        batch_id=batch.batch_id,
        classification=BatchEligibility.INVALID if issues else BatchEligibility.ELIGIBLE,
        article_count=len(batch.article_ids),
        issues=issues,
        rubric_contract_version=config.rubric_contract_version if study_type else None,
        rubric_sha256=rubric_sha256,
    )


def _validate_span_references(span_ids: list[str], article_id: str, frozen_spans: dict[str, Any], issues: list[CheckIssue]) -> None:
    if not span_ids:
        return
    if article_id not in frozen_spans:
        issues.append(_issue("missing_frozen_spans", f"article {article_id} 缺少批次冻结 spans"))
        return
    try:
        resolve_span_ids(frozen_spans[article_id], span_ids)
        if any(not span_id.startswith(article_id + ":") for span_id in span_ids):
            raise ValueError("span ID 所属文章不一致")
    except (KeyError, ValueError) as exc:
        issues.append(_issue("invalid_evidence_span", f"article {article_id}：{exc}"))


def build_dry_run_report(snapshot: TaskEvaluationSnapshot, config: EvaluationConfig, max_llm_calls: int | None) -> DryRunReport:
    results = [check_batch(snapshot, batch, config) for batch in snapshot.batches]
    counts = Counter(item.classification.value for item in results)
    eligible_ids = {item.batch_id for item in results if item.classification == BatchEligibility.ELIGIBLE}
    eligible_batches = [batch for batch in snapshot.batches if batch.batch_id in eligible_ids]
    distribution = Counter(batch.study_type for batch in eligible_batches)
    calls = {judge.value: len(eligible_batches) for judge in JudgeType}
    estimated = sum(calls.values())
    return DryRunReport(
        task_id=snapshot.task.task_id,
        task_status=snapshot.task.status,
        batch_counts={item.value: counts[item.value] for item in BatchEligibility},
        eligible_article_count=sum(len(batch.article_ids) for batch in eligible_batches),
        study_type_distribution=dict(sorted(distribution.items())),
        planned_calls=calls,
        estimated_llm_calls=estimated,
        max_llm_calls=max_llm_calls,
        within_call_budget=max_llm_calls is None or estimated <= max_llm_calls,
        judge_provider=config.judge_provider,
        judge_model=config.judge_model,
        prompt_versions=config.prompt_versions,
        batch_results=results,
    )
