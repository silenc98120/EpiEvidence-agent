import os
from dotenv import load_dotenv

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama

from backend.app.core.local_bge_m3 import LocalBGEM3EmbeddingClient
from backend.app.core.quality_candidate_selector import OpenAICompatibleEmbeddingClient


load_dotenv()

OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "deepseek",
    "zai",
    "qwen",
    "aliyun",
    "kimi",
    "modelscope",
    "openrouter",
}


def normalize_openai_base_url(base_url: str | None) -> str | None:
    """将 OpenAI-compatible 地址规范化为 API 根路径。"""

    if not base_url:
        return None
    normalized = base_url.strip().rstrip("/")
    suffix = "/chat/completions"
    if normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)]
    return normalized.rstrip("/")


def _required_env(name: str) -> str:
    """读取必填配置，缺失时在启动阶段给出明确错误。"""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"缺少环境变量: {name}")
    return value


def create_chat_model() -> BaseChatModel:
    """根据当前环境配置创建一个聊天模型客户端。"""
    provider = _required_env("LLM_PROVIDER").lower()
    model = _required_env("LLM_MODEL")
    base_url = normalize_openai_base_url(os.getenv("LLM_BASE_URL"))
    temperature = float(os.getenv("LLM_TEMPERATURE", "0"))

    if provider == "ollama":
        return ChatOllama(
            model=model,
            base_url=base_url or "http://localhost:11434",
            temperature=temperature,
        )

    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        api_key = _required_env("LLM_API_KEY")
        kwargs = {
            "model": model,
            "api_key": api_key,
            "temperature": temperature,
            "timeout": 120,
            "max_retries": 3,
        }
        if base_url:
            kwargs["base_url"] = base_url

        return ChatOpenAI(**kwargs)

    raise ValueError(f"不支持的 LLM_PROVIDER: {provider}")


def create_embedding_client() -> OpenAICompatibleEmbeddingClient | LocalBGEM3EmbeddingClient:
    """创建质量候选排序使用的本地或 OpenAI-compatible Adapter。"""

    local_path = os.getenv("BGE_M3_MODEL_PATH", "").strip()
    if local_path:
        return LocalBGEM3EmbeddingClient(
            model_path=local_path,
            device=os.getenv("EMBEDDING_DEVICE", "cpu"),
            batch_size=int(os.getenv("EMBEDDING_BATCH_SIZE", "8")),
            max_length=int(os.getenv("EMBEDDING_MAX_LENGTH", "1024")),
            max_concurrency=int(os.getenv("EMBEDDING_MAX_CONCURRENCY", "1")),
        )

    model = _required_env("EMBEDDING_MODEL")
    api_key = os.getenv("EMBEDDING_API_KEY") or _required_env("LLM_API_KEY")
    base_url = normalize_openai_base_url(
        os.getenv("EMBEDDING_BASE_URL") or os.getenv("LLM_BASE_URL")
    )
    return OpenAICompatibleEmbeddingClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


