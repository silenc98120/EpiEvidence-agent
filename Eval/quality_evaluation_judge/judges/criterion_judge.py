"""Criterion Judge：条目细粒度审查。

逐篇、逐条审查生产 Evaluator 的质量条目判定，与原始摘要交叉比较。
judge_expected_quality_score 由 Python 根据 expected_status / production_status 重算。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from backend.skills.quality_evaluator.scoring import calculate_preliminary_quality

from ..client import JudgeLLMClient
from ..enums import JudgeType, JudgeVerdict
from ..prompt_loader import LoadedPrompt
from ..schemas import (
    ArticleCriterionVerdicts,
    ArticleSnapshot,
    BatchSnapshot,
    CriterionItemVerdict,
    CriterionJudgeOutput,
)


# 固定分值映射
_STATUS_SCORE: dict[str, float | None] = {
    "favorable": 1.0,
    "unclear": -0.5,
    "not_reported": -1.0,
    "not_applicable": None,
}


def _build_user_message(
    batch: BatchSnapshot,
    articles: dict[UUID, ArticleSnapshot],
) -> str:
    """组装 Criterion Judge 的 user message，包含文章原文和生产评价结果。"""
    article_items = []
    for article_id in batch.article_ids:
        article = articles[article_id]
        article_items.append(
            {
                "article_id": str(article.article_id),
                "title": article.title,
                "abstract": article.abstract or "（无摘要）",
            }
        )

    payload = {
        "batch_id": batch.batch_id,
        "study_type": batch.study_type,
        "articles": article_items,
        "production_assessment": batch.assessment_payload,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def compute_expected_quality_score(
    verdicts: list[CriterionItemVerdict],
    production_criteria: list[dict[str, Any]],
) -> float | None:
    """根据 Judge verdict 计算期望质量分。

    规则：
    - agree / partially_agree / insufficient_evidence → 使用 production_status
    - disagree → 使用 expected_status
    - 任一条目的最终 status 无法确定 → 返回 None
    """
    production_by_id = {item["criterion_id"]: item for item in production_criteria}
    scores: list[float | None] = []

    for verdict in verdicts:
        if verdict.verdict == JudgeVerdict.DISAGREE:
            if verdict.expected_status is None:
                return None  # disagree 但未给出期望 status，无法计算
            status = verdict.expected_status
        else:
            prod = production_by_id.get(verdict.criterion_id)
            if prod is None:
                return None
            status = prod.get("status", "")

        score = _STATUS_SCORE.get(status)
        if score is None and status != "not_applicable":
            return None  # 未知 status
        scores.append(score)

    try:
        return calculate_preliminary_quality(scores)
    except ValueError:
        return None


async def run_criterion_judge(
    *,
    client: JudgeLLMClient,
    prompt: LoadedPrompt,
    batch: BatchSnapshot,
    articles: dict[UUID, ArticleSnapshot],
) -> tuple[CriterionJudgeOutput, dict[UUID, float | None]]:
    """执行 Criterion Judge 并返回 LLM 输出 + Python 重算的 quality_score 字典。"""
    user_message = _build_user_message(batch, articles)
    result = await client.invoke(
        system_prompt=prompt.text,
        user_message=user_message,
        output_schema=CriterionJudgeOutput,
        judge_type=JudgeType.CRITERION,
        batch_id=batch.batch_id,
        study_type=batch.study_type,
    )

    # Python 后处理：重算每篇文章的期望质量分
    quality_scores: dict[UUID, float | None] = {}
    assessment = batch.assessment_payload or {}
    quality_assessments = assessment.get("quality_assessments", [])
    prod_criteria_by_article: dict[str, list[dict[str, Any]]] = {
        item["article_id"]: item.get("criteria", []) for item in quality_assessments
    }

    for article_verdicts in result.article_criterion_verdicts:
        prod_criteria = prod_criteria_by_article.get(str(article_verdicts.article_id), [])
        score = compute_expected_quality_score(article_verdicts.verdicts, prod_criteria)
        quality_scores[article_verdicts.article_id] = score

    return result, quality_scores
