"""Ranking Judge：批次质量层级审查。

独立判断同 Batch 内各文章的质量层级排序。
方向冲突由 Python 后处理判定：两篇文章在生产中的优劣关系与 Judge 发生翻转才记为冲突，
tier 粒度差异（如并列）不视为冲突。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from ..client import JudgeLLMClient
from ..enums import JudgeType
from ..prompt_loader import LoadedPrompt
from ..schemas import (
    ArticleSnapshot,
    BatchSnapshot,
    RankedTier,
    RankingJudgeOutput,
)


def _build_user_message(
    batch: BatchSnapshot,
    articles: dict[UUID, ArticleSnapshot],
) -> str:
    """组装 Ranking Judge 的 user message。

    包含文章题名摘要及生产 criteria 状态摘要（不包含 Criterion Judge 输出）。
    """
    assessment = batch.assessment_payload or {}
    quality_assessments = assessment.get("quality_assessments", [])
    criteria_by_article: dict[str, list[dict[str, str]]] = {}
    for qa in quality_assessments:
        aid = qa.get("article_id")
        if aid:
            criteria_by_article[str(aid)] = [
                {"criterion_id": c.get("criterion_id", ""), "status": c.get("status", "")}
                for c in qa.get("criteria", [])
            ]

    article_items = []
    for article_id in batch.article_ids:
        article = articles[article_id]
        article_items.append(
            {
                "article_id": str(article.article_id),
                "title": article.title,
                "abstract": article.abstract or "（无摘要）",
                "criteria_summary": criteria_by_article.get(str(article.article_id), []),
            }
        )

    payload = {
        "batch_id": batch.batch_id,
        "study_type": batch.study_type,
        "articles": article_items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def check_direction_consistency(
    judge_ranking: list[RankedTier],
    production_scores: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    """检测 Judge 独立排序与生产质量分之间的相对方向一致性。

    规则：
    - tier 数字越小表示质量越高（如 tier 1 > tier 2）。
    - 仅当两篇文章在生产中 A > B，而 Judge 给出 A < B 时才记为方向翻转冲突。
    - 并列（tier 相同或生产分相同）不视为冲突。
    """
    # 建立 article_id -> tier 映射
    tier_by_article: dict[str, int] = {}
    for tier in judge_ranking:
        for aid in tier.article_ids:
            tier_by_article[str(aid)] = tier.tier

    # 建立 article_id -> production quality_score 映射
    prod_score_by_article: dict[str, float] = {}
    for item in production_scores:
        aid = str(item.get("article_id", ""))
        score = item.get("quality_score")
        if aid and score is not None:
            try:
                prod_score_by_article[aid] = float(score)
            except (ValueError, TypeError):
                pass

    conflicts: list[str] = []
    article_ids = list(tier_by_article.keys())

    for i in range(len(article_ids)):
        for j in range(i + 1, len(article_ids)):
            id_a = article_ids[i]
            id_b = article_ids[j]

            score_a = prod_score_by_article.get(id_a)
            score_b = prod_score_by_article.get(id_b)
            tier_a = tier_by_article.get(id_a)
            tier_b = tier_by_article.get(id_b)

            if score_a is None or score_b is None or tier_a is None or tier_b is None:
                continue

            # 生产中 A 优于 B (score_a > score_b)，但 Judge 认为 B 优于 A (tier_a > tier_b)
            if score_a > score_b and tier_a > tier_b:
                conflicts.append(
                    f"方向翻转：生产判定 {id_a}（分值 {score_a}）优于 {id_b}（分值 {score_b}），"
                    f"但 Judge 独立判定 {id_b}（Tier {tier_b}）优于 {id_a}（Tier {tier_a}）"
                )
            # 生产中 B 优于 A (score_b > score_a)，但 Judge 认为 A 优于 B (tier_b > tier_a)
            elif score_b > score_a and tier_b > tier_a:
                conflicts.append(
                    f"方向翻转：生产判定 {id_b}（分值 {score_b}）优于 {id_a}（分值 {score_a}），"
                    f"但 Judge 独立判定 {id_a}（Tier {tier_a}）优于 {id_b}（Tier {tier_b}）"
                )

    is_consistent = len(conflicts) == 0
    return is_consistent, conflicts


async def run_ranking_judge(
    *,
    client: JudgeLLMClient,
    prompt: LoadedPrompt,
    batch: BatchSnapshot,
    articles: dict[UUID, ArticleSnapshot],
) -> tuple[RankingJudgeOutput, bool, list[str]]:
    """执行 Ranking Judge 并返回 LLM 输出 + 方向一致性布尔值 + 冲突列表。"""
    user_message = _build_user_message(batch, articles)
    result = await client.invoke(
        system_prompt=prompt.text,
        user_message=user_message,
        output_schema=RankingJudgeOutput,
        judge_type=JudgeType.RANKING,
        batch_id=batch.batch_id,
        study_type=batch.study_type,
    )

    score_payload = batch.score_payload or []
    is_consistent, conflicts = check_direction_consistency(
        result.judge_ranking, score_payload
    )

    return result, is_consistent, conflicts
