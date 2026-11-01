"""一次结构化模型请求；重试和执行所有权由 BatchExecutor 管理。"""
from __future__ import annotations

import json
import hashlib
import time
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from .prompt_loader import QualityPromptLoader
from .evidence import resolve_article_evidence, resolve_portfolio_evidence
from .schema import (
    ArticleAssessmentBatch, HydratedArticleBatch, PortfolioEvaluation,
    QualityEvaluationJob, StudyType, article_assessment_model_for,
)


def _sanitize_batch_payload(response_data: dict, job_info: Mapping) -> dict:
    """只补调用方拥有的外层标识，不补造模型评价。"""
    return {**response_data, **{key: job_info[key] for key in
        ("task_id", "group_id", "study_type", "article_ids")}}


def _sanitize_portfolio_payload(response_data: dict, payload: Mapping) -> dict:
    return {**response_data, "task_id": payload["task_id"],
        "candidate_article_ids": [str(a["article_id"]) for a in payload["candidate_articles"]]}


class QualityEvaluatorRuntime:
    def __init__(self, *, model: Any, repository: Any,
                 prompt_loader: QualityPromptLoader | None = None,
                 callbacks: Sequence[Any] = ()) -> None:
        self.model = model
        self.repository = repository
        self.prompt_loader = prompt_loader or QualityPromptLoader()
        self.callbacks = list(callbacks)
        contract = {
            "model": {name: str(getattr(model, name, None)) for name in
                ("model_name", "model", "temperature", "max_tokens", "model_kwargs", "extra_body", "openai_api_base", "base_url")},
            "schemas": {kind.value: article_assessment_model_for(kind).model_json_schema() for kind in StudyType},
            "prompts": {kind.value: self.prompt_loader.article_evaluation_prompt(kind) for kind in StudyType},
            "portfolio_schema": PortfolioEvaluation.model_json_schema(),
            "portfolio_prompt": self.prompt_loader.portfolio_prompt(),
            "scoring_version": "1",
        }
        self.contract_version = hashlib.sha256(json.dumps(contract, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def evaluate(self, job: QualityEvaluationJob, *, user_query: str,
                       intent_analysis: Mapping | None = None,
                       search_plan: Mapping | None = None) -> ArticleAssessmentBatch:
        batch = await self.repository.hydrate(job, user_query=user_query)
        return await self.evaluate_hydrated(batch, intent_analysis=intent_analysis,
                                           search_plan=search_plan)

    async def evaluate_hydrated(self, batch: HydratedArticleBatch, *,
                               intent_analysis: Mapping | None = None,
                               search_plan: Mapping | None = None,
                               metrics: dict | None = None,
                               metadata: Mapping | None = None) -> ArticleAssessmentBatch:
        job = batch.job
        payload = {
            "user_query": batch.user_query,
            "intent_analysis": dict(intent_analysis or {}),
            "search_plan": dict(search_plan or {}),
            "job": job.model_dump(mode="json"),
            "articles": [a.model_dump(mode="json", exclude_none=True) for a in batch.articles],
        }
        return await self._invoke(
            system_prompt=self.prompt_loader.article_evaluation_prompt(job.study_type),
            payload=payload, output_model=article_assessment_model_for(job.study_type),
            metrics=metrics, metadata={"task_id": job.task_id, "study_type": job.study_type.value,
                                      **dict(metadata or {})},
        )

    async def evaluate_portfolio(self, *, task_id: str, user_query: str,
                                 intent_analysis: Mapping, search_plan: Mapping,
                                 candidate_articles: Sequence[Mapping],
                                 metrics: dict | None = None, metadata: Mapping | None = None) -> PortfolioEvaluation:
        ids = [UUID(str(a["article_id"])) for a in candidate_articles]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("集合评价必须包含不重复的候选文章")
        return await self._invoke(
            system_prompt=self.prompt_loader.portfolio_prompt(),
            payload={"task_id": task_id, "user_query": user_query,
                     "intent_analysis": dict(intent_analysis), "search_plan": dict(search_plan),
                     "candidate_articles": [dict(a) for a in candidate_articles]},
            output_model=PortfolioEvaluation, metrics=metrics,
            metadata={"task_id": task_id, "stage": "quality_portfolio", **dict(metadata or {})},
        )

    async def _invoke(self, *, system_prompt: str, payload: Mapping,
                      output_model: type[BaseModel], metrics: dict | None = None,
                      metadata: Mapping | None = None) -> Any:
        # structured output 负责传递 Schema；system prompt 只保留业务判定规则。
        payload_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        messages = [SystemMessage(content=system_prompt),
                    HumanMessage(content=payload_text)]
        runnable = self.model.with_structured_output(output_model, method="function_calling", include_raw=True)
        started = time.perf_counter()
        stats = metrics if metrics is not None else {}
        stats["prompt_chars"] = len(system_prompt) + len(payload_text)
        stats["payload_chars"] = len(payload_text)
        try:
            response = await runnable.ainvoke(messages, config={
                "callbacks": self.callbacks, "metadata": dict(metadata or {}),
                "run_name": "quality-model",
            })
        finally:
            stats["model_ms"] = round((time.perf_counter() - started) * 1000, 3)
        raw = response["raw"]
        usage = raw.usage_metadata or raw.response_metadata.get("token_usage", {})
        stats["usage"] = usage
        if usage:
            stats["input_tokens"] = usage.get("input_tokens", usage.get("prompt_tokens"))
            stats["output_tokens"] = usage.get("output_tokens", usage.get("completion_tokens"))
            stats["total_tokens"] = usage.get("total_tokens")
        stats["finish_reason"] = raw.response_metadata.get("finish_reason")
        started = time.perf_counter()
        # 使用 structured output 已解析的对象，再执行业务证据与输入覆盖校验。
        try:
            parsed = response.get("parsed")
            if parsed is not None:
                data = parsed.model_dump(mode="json") if isinstance(parsed, BaseModel) else dict(parsed)
            else:
                # 兼容少数 OpenAI-compatible Adapter 未填充 parsed 的响应。
                data = json.loads(raw.content)
            if "job" in payload:
                data = _sanitize_batch_payload(data, payload["job"])
                data = resolve_article_evidence(data, payload["articles"])
            else:
                data = _sanitize_portfolio_payload(data, payload)
                data = resolve_portfolio_evidence(data, payload["candidate_articles"])
            result = output_model.model_validate(data)
        finally:
            stats["validate_ms"] = round((time.perf_counter() - started) * 1000, 3)
        return result
