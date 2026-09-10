"""三个质量评价 LLM 调用的结构化运行时适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from .prompt_loader import QualityPromptLoader
from .schema import (
    ArticleAssessmentBatch,
    HydratedArticleBatch,
    PortfolioEvaluation,
    QualityEvaluationJob,
    VerificationBatch,
    article_assessment_model_for,
)


class QualityEvaluatorRuntime:
    def __init__(
        self,
        *,
        model: Any,
        repository: Any,
        prompt_loader: QualityPromptLoader | None = None,
        max_attempts: int = 2,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        self.model = model
        self.repository = repository
        self.prompt_loader = prompt_loader or QualityPromptLoader()
        self.max_attempts = max_attempts

    async def evaluate(
        self,
        job: QualityEvaluationJob,
        *,
        user_query: str,
        intent_analysis: Mapping[str, Any] | None = None,
        search_plan: Mapping[str, Any] | None = None,
    ) -> ArticleAssessmentBatch:
        hydrated = await self.repository.hydrate(job, user_query=user_query)
        payload = {
            "user_query": user_query,
            "intent_analysis": dict(intent_analysis or {}),
            "search_plan": dict(search_plan or {}),
            "job": job.model_dump(mode="json"),
            "articles": [article.model_dump(mode="json") for article in hydrated.articles],
        }
        result = await self._invoke(
            system_prompt=self.prompt_loader.article_evaluation_prompt(job.study_type),
            payload=payload,
            output_model=article_assessment_model_for(job.study_type),
        )
        if (
            result.task_id != job.task_id
            or result.group_id != job.group_id
            or result.study_type != job.study_type
            or set(result.article_ids) != set(job.article_ids)
        ):
            raise ValueError("文章级评价输出与输入 job 不一致")
        return result

    async def verify(
        self,
        *,
        hydrated: HydratedArticleBatch,
        original: ArticleAssessmentBatch,
        original_total_scores: Mapping[UUID, float],
    ) -> VerificationBatch:
        expected_ids = set(hydrated.job.article_ids)
        if set(original.article_ids) != expected_ids:
            raise ValueError("原始评价与数据库补全文章 UUID 不一致")
        if set(original_total_scores) != expected_ids:
            raise ValueError("original_total_scores 必须完整覆盖文章 UUID")
        payload = {
            "user_query": hydrated.user_query,
            "articles": [article.model_dump(mode="json") for article in hydrated.articles],
            "original_assessments": original.model_dump(mode="json"),
            "original_total_scores": {
                str(article_id): score for article_id, score in original_total_scores.items()
            },
        }
        result = await self._invoke(
            system_prompt=self.prompt_loader.verification_prompt(),
            payload=payload,
            output_model=VerificationBatch,
        )
        if (
            result.task_id != original.task_id
            or result.group_id != original.group_id
            or result.study_type != original.study_type
            or set(result.article_ids) != expected_ids
        ):
            raise ValueError("核查输出与原始评价 batch 不一致")
        for article in result.article_verifications:
            if article.original_total_score != original_total_scores[article.article_id]:
                raise ValueError("核查输出不得改写 original_total_score")
        return result

    async def evaluate_portfolio(
        self,
        *,
        task_id: str,
        user_query: str,
        intent_analysis: Mapping[str, Any],
        search_plan: Mapping[str, Any],
        candidate_articles: Sequence[Mapping[str, Any]],
    ) -> PortfolioEvaluation:
        if not candidate_articles:
            raise ValueError("candidate_articles 不能为空")
        candidate_ids = [UUID(str(article["article_id"])) for article in candidate_articles]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate_articles 的 article_id 不能重复")
        payload = {
            "task_id": task_id,
            "user_query": user_query,
            "intent_analysis": dict(intent_analysis),
            "search_plan": dict(search_plan),
            "candidate_articles": [dict(article) for article in candidate_articles],
        }
        result = await self._invoke(
            system_prompt=self.prompt_loader.portfolio_prompt(),
            payload=payload,
            output_model=PortfolioEvaluation,
        )
        if result.task_id != task_id or set(result.candidate_article_ids) != set(
            candidate_ids
        ):
            raise ValueError("集合评价输出与候选文章 UUID 不一致")
        return result

    async def _invoke(
        self,
        *,
        system_prompt: str,
        payload: Mapping[str, Any],
        output_model: type[BaseModel],
    ) -> Any:
        runnable = self.model.with_structured_output(output_model)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
        ]
        last_error: Exception | None = None
        for _attempt in range(self.max_attempts):
            try:
                response = await runnable.ainvoke(messages)
                return (
                    response
                    if isinstance(response, output_model)
                    else output_model.model_validate(response)
                )
            except Exception as exc:  # Validation and provider failures share retry policy.
                last_error = exc
        raise RuntimeError(
            f"结构化质量评价在 {self.max_attempts} 次尝试后仍失败"
        ) from last_error
