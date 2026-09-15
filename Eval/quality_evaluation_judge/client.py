"""基于 JUDGE_* 环境变量的独立 LLM 调用客户端。"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TypeVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from backend.app.core.llm_provider import OPENAI_COMPATIBLE_PROVIDERS, normalize_openai_base_url

from .config import EvaluationConfig
from .enums import JudgeType

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class JudgeCallRecord:
    """单次 Judge 调用的可审计记录。"""

    judge_type: str
    batch_id: str
    study_type: str
    attempt: int
    success: bool
    latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_type: str | None = None
    error_message: str | None = None


class JudgeLLMClient:
    """3 个 Judge 共享的模型调用客户端。

    使用 ``JUDGE_PROVIDER`` / ``JUDGE_MODEL`` / ``JUDGE_BASE_URL`` /
    ``JUDGE_API_KEY`` 初始化独立的 ChatOpenAI 实例，与生产 LLM 完全隔离。
    """

    def __init__(self, config: EvaluationConfig) -> None:
        self._config = config
        self._llm = self._create_llm(config)
        self._call_records: list[JudgeCallRecord] = []

    @property
    def call_records(self) -> list[JudgeCallRecord]:
        return list(self._call_records)

    async def invoke(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_schema: type[T],
        judge_type: JudgeType,
        batch_id: str,
        study_type: str,
    ) -> T:
        """调用 Judge LLM 并返回结构化输出，带重试与审计记录。"""
        structured_llm = self._llm.with_structured_output(
            output_schema,
            method="json_schema",
            strict=True,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 2):  # 1 次原始 + N 次重试
            start = time.monotonic()
            try:
                result = await structured_llm.ainvoke(messages)
                elapsed_ms = (time.monotonic() - start) * 1000

                # 尝试提取 token usage
                prompt_tokens = None
                completion_tokens = None
                if hasattr(result, "response_metadata"):
                    usage = getattr(result, "response_metadata", {}).get("token_usage", {})
                    prompt_tokens = usage.get("prompt_tokens")
                    completion_tokens = usage.get("completion_tokens")

                self._call_records.append(
                    JudgeCallRecord(
                        judge_type=judge_type.value,
                        batch_id=batch_id,
                        study_type=study_type,
                        attempt=attempt,
                        success=True,
                        latency_ms=elapsed_ms,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                )
                return result  # type: ignore[return-value]

            except Exception as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                last_error = exc
                self._call_records.append(
                    JudgeCallRecord(
                        judge_type=judge_type.value,
                        batch_id=batch_id,
                        study_type=study_type,
                        attempt=attempt,
                        success=False,
                        latency_ms=elapsed_ms,
                        error_type=type(exc).__name__,
                        error_message=str(exc)[:500],
                    )
                )

        # 所有重试耗尽
        raise RuntimeError(
            f"Judge {judge_type.value} 在 batch {batch_id} 上 "
            f"{self._config.max_retries + 1} 次调用全部失败"
        ) from last_error

    @staticmethod
    def _create_llm(config: EvaluationConfig) -> ChatOpenAI:
        provider = (config.judge_provider or os.getenv("JUDGE_PROVIDER", "")).lower()
        model = config.judge_model or os.getenv("JUDGE_MODEL", "")
        base_url = normalize_openai_base_url(
            config.judge_base_url or os.getenv("JUDGE_BASE_URL")
        )
        api_key = os.getenv("JUDGE_API_KEY", "")

        if not provider:
            raise RuntimeError("缺少 JUDGE_PROVIDER 配置")
        if not model:
            raise RuntimeError("缺少 JUDGE_MODEL 配置")
        if provider not in OPENAI_COMPATIBLE_PROVIDERS:
            raise ValueError(f"Judge 暂不支持 provider: {provider}")
        if not api_key:
            raise RuntimeError("缺少 JUDGE_API_KEY 环境变量")

        kwargs: dict = {
            "model": model,
            "api_key": api_key,
            "temperature": config.temperature,
            "timeout": 180,
            "max_retries": 0,  # 重试由本类控制
        }
        if base_url:
            kwargs["base_url"] = base_url

        return ChatOpenAI(**kwargs)
