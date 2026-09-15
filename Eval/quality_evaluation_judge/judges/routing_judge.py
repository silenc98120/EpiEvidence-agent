"""Routing Judge：研究类型门控。

判断 Batch 内每篇文章的实际研究设计是否与声明的 study_type 一致。
batch_verdict 由 Python 聚合：任一文章 disagree → batch disagree。
"""

from __future__ import annotations

import json
from uuid import UUID

from ..client import JudgeLLMClient
from ..enums import JudgeType, JudgeVerdict
from ..prompt_loader import LoadedPrompt
from ..schemas import ArticleRoutingVerdict, BatchSnapshot, RoutingJudgeOutput, ArticleSnapshot


def _build_user_message(batch: BatchSnapshot, articles: dict[UUID, ArticleSnapshot]) -> str:
    """组装 Routing Judge 的 user message。"""
    items = []
    for article_id in batch.article_ids:
        article = articles[article_id]
        items.append(
            {
                "article_id": str(article.article_id),
                "title": article.title,
                "abstract": article.abstract or "（无摘要）",
                "study_design": article.study_design,
                "publication_types": article.publication_types,
            }
        )
    payload = {
        "batch_id": batch.batch_id,
        "declared_study_type": batch.study_type,
        "articles": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def compute_batch_verdict(article_verdicts: list[ArticleRoutingVerdict]) -> JudgeVerdict:
    """Python 聚合：任一文章 disagree → batch disagree。"""
    verdicts = {v.verdict for v in article_verdicts}
    if JudgeVerdict.DISAGREE in verdicts:
        return JudgeVerdict.DISAGREE
    if JudgeVerdict.INSUFFICIENT_EVIDENCE in verdicts:
        return JudgeVerdict.INSUFFICIENT_EVIDENCE
    if JudgeVerdict.PARTIALLY_AGREE in verdicts:
        return JudgeVerdict.PARTIALLY_AGREE
    return JudgeVerdict.AGREE


async def run_routing_judge(
    *,
    client: JudgeLLMClient,
    prompt: LoadedPrompt,
    batch: BatchSnapshot,
    articles: dict[UUID, ArticleSnapshot],
) -> tuple[RoutingJudgeOutput, JudgeVerdict]:
    """执行 Routing Judge 并返回 LLM 输出 + Python 聚合的 batch_verdict。"""
    user_message = _build_user_message(batch, articles)
    result = await client.invoke(
        system_prompt=prompt.text,
        user_message=user_message,
        output_schema=RoutingJudgeOutput,
        judge_type=JudgeType.ROUTING,
        batch_id=batch.batch_id,
        study_type=batch.study_type,
    )
    batch_verdict = compute_batch_verdict(result.article_verdicts)
    return result, batch_verdict
