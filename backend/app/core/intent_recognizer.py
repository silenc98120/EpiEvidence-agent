""" 用户 Query 任务意图识别模块
用户意图识别分为2步：
1.基于 LLM 语义理解与中英双语扩写。
2.基于 MeSH 术语库进行关键词模式匹配，返回近似词。
"""

from enum import StrEnum

from time import perf_counter

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import SystemMessage, HumanMessage
from loguru import logger

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field, model_validator

from backend.app.prompts.intent_recognizer_prompt_template import INTENT_RECOGNIZER_SYSTEM_PROMPT_TEMPLATE

class IntentCategory(StrEnum):
    SIMPLE_CHAT = "simple_chat"
    CLARIFICATION_ANSWER = "clarification_answer"
    NEW_RESEARCH = "new_research"
    SUPPLEMENTARY_SEARCH = "supplementary_search"


class ResearchDirection(StrEnum):
    SAFETY = "safety"
    EFFECTIVENESS = "effectiveness"
    INCIDENCE = "incidence"
    MECHANISM = "mechanism"
    PROGNOSIS = "prognosis"
    RISK_FACTOR = "risk_factor"
    OTHER = "other"


class ResearchKeywordKind(StrEnum):
    DISEASE = "disease"
    DRUG = "drug"
    POPULATION = "population"
    INTERVENTION = "intervention"
    COMPARATOR = "comparator"
    OUTCOME = "outcome"
    TIME = "time"
    REGION = "region"
    OTHER = "other"


class KeywordCandidate(BaseModel):
    """LLM 从用户 query 提取的候选关键词，尚未经过 MeSH 确认。"""

    raw_text: str = Field(min_length=1, max_length=200)
    english_candidates: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)


class IntentRecognitionResult(BaseModel):
    """LLM 对当前用户消息的结构化分析结果。"""

    intent_category: IntentCategory
    intent_confidence: float = Field(ge=0.0, le=1.0)
    directions: list[ResearchDirection] = Field(default_factory=list, max_length=3)
    keywords: dict[ResearchKeywordKind, list[KeywordCandidate]] = Field(
        default_factory=dict,
    )
    short_reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_simple_chat(self) -> "IntentRecognitionResult":
        if self.intent_category == IntentCategory.SIMPLE_CHAT:
            if self.directions or self.keywords:
                raise ValueError(
                    "simple_chat 不能包含研究方向或研究关键词"
                )
        return self


class IntentRecognizer:
    """调用 LLM 完成用户消息分类与关键词初步提取。"""

    def __init__(
            self,
            model: BaseChatModel,
            system_prompt_template: str = INTENT_RECOGNIZER_SYSTEM_PROMPT_TEMPLATE,
            callback_handler: BaseCallbackHandler | None = None,
    ) -> None:
        self._model = model
        self._system_prompt_template = system_prompt_template
        self._callback_handler = callback_handler
        self._structured_model = model.with_structured_output(
            IntentRecognitionResult,
            include_raw=True,
        )
        self._system_prompt = self.build_intent_system_prompt()

    async def recognize(self, user_query: str) -> IntentRecognitionResult:
        """识别单条用户 query，返回已通过 Pydantic 校验的结果。"""

        query = user_query.strip()
        if not query:
            raise ValueError("query 不能为空。")

        start_time = perf_counter()

        try:
            callbacks = (
                [self._callback_handler]
                if self._callback_handler is not None
                else []
            )

            response = await self._structured_model.ainvoke(
                [
                    SystemMessage(content=self._system_prompt),
                    HumanMessage(content=f"<user_query>\n{query}\n</user_query>"),
                ],
                config={
                    "callbacks": callbacks,
                    "run_name": "intent-recognition",
                    "tags": ["workflow:research-task", "step:intent-recognition"],
                },
            )
        except Exception:
            latency_ms = (perf_counter() - start_time) * 1000

            logger.bind(
                component="intent_recognizer",
                event="llm_call_failed",
                latency_ms=round(latency_ms, 1),
            ).exception("意图识别 LLM 调用失败")

            raise

        latency_ms = (perf_counter() - start_time) * 1000
        result = response["parsed"]
        raw_message = response["raw"]
        parsing_error = response["parsing_error"]

        if parsing_error is not None or result is None:
            logger.bind(
                component="intent_recognizer",
                event="structured_output_invalid",
                latency_ms=round(latency_ms, 1),
                error_type=type(parsing_error).__name__ if parsing_error else None,
            ).warning("意图识别结构化输出校验失败")

            raise ValueError("LLM 返回内容未能通过 IntentRecognitionResult 校验。")

        usage = (
                getattr(raw_message, "usage_metadata", None)
                or raw_message.response_metadata.get("token_usage", {})
        )

        logger.bind(
            component="intent_recognizer",
            event="intent_recognition_completed",
            intent=result.intent_category.value,
            confidence=result.intent_confidence,
            keyword_group_count=len(result.keywords),
            latency_ms=round(latency_ms, 1),
            token_usage=usage,
        ).info("意图识别完成")

        return result


    # -------------------------------------------------------------
    #  IntentRecognizer 类内部工具
    # -------------------------------------------------------------
    def _enum_values(self,enum_type: type[StrEnum]) -> str:
        return "、".join(member.value for member in enum_type)

    def build_intent_system_prompt(self) -> str:
        return (
            self._system_prompt_template.replace(
                "__KEYWORD_KINDS__",
                self._enum_values(ResearchKeywordKind),
            )
            .replace(
                "__RESEARCH_DIRECTIONS__",
                self._enum_values(ResearchDirection),
            )
        )