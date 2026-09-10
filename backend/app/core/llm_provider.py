import os
from dotenv import load_dotenv

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama


load_dotenv()

OPENAI_COMPATIBLE_PROVIDERS = {
    "openai",
    "deepseek",
    "qwen",
    "kimi",
    "modelscope",
    "openrouter",
}


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
    base_url = os.getenv("LLM_BASE_URL")
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


