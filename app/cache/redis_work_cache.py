"""基于 Redis 的短期工作缓存。

Redis 在 EpiEvidence 中只保存任务编排所需的短期索引和小型决策结果：

* 任务状态；
* search run 对应的 ``article_id`` 有序集合；
* 分组后的 ``article_id`` 有序集合；
* 意图、充分性路由等小型 JSON 结果。

题录、摘要、原始响应和全文资源仍然以 PostgreSQL 为事实来源。缓存不可用时，
默认让主流程继续运行，避免把 Redis 变成检索任务的单点故障。
"""

from __future__ import annotations

import inspect
import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


class RedisWorkCacheConfig(BaseModel):
    """Redis 工作缓存配置。"""

    redis_url: str = Field(default="redis://localhost:6379/0", min_length=1)
    namespace: str = Field(default="epi:evidence", min_length=1, max_length=80)
    ttl_seconds: int = Field(default=24 * 60 * 60, ge=60, le=7 * 24 * 60 * 60)
    fail_open: bool = True

    @classmethod
    def from_env(cls) -> "RedisWorkCacheConfig":
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            namespace=os.getenv("REDIS_NAMESPACE", "epi:evidence"),
            ttl_seconds=int(os.getenv("REDIS_WORK_TTL_SECONDS", str(24 * 60 * 60))),
            fail_open=os.getenv("REDIS_FAIL_OPEN", "true").casefold()
            in {"1", "true", "yes", "on"},
        )


class RedisWorkCache:
    """Redis 工作缓存的最小应用边界。

    ``client`` 参数用于依赖注入和测试；不传时才会延迟导入并创建
    ``redis.asyncio.Redis`` 客户端，因此仅导入业务代码不会强制连接 Redis。
    """

    def __init__(
        self,
        *,
        config: RedisWorkCacheConfig | None = None,
        client: Any | None = None,
    ) -> None:
        self.config = config or RedisWorkCacheConfig.from_env()
        self._owns_client = client is None
        if client is not None:
            self.client = client
            return

        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError(
                "RedisWorkCache 需要 redis 包，请执行 `uv sync` 安装项目依赖。"
            ) from exc
        self.client = Redis.from_url(
            self.config.redis_url,
            decode_responses=True,
        )

    async def ping(self) -> bool:
        """检查 Redis 是否可用。"""

        result = await self._call("ping")
        return bool(result)

    async def close(self) -> None:
        """关闭由本类创建的客户端；外部注入的客户端由调用方管理。"""

        if not self._owns_client:
            return
        close_method = getattr(self.client, "aclose", None) or getattr(
            self.client, "close", None
        )
        if close_method is not None:
            result = close_method()
            if inspect.isawaitable(result):
                await result

    async def set_task_status(
        self,
        task_id: str,
        status: str,
        **metadata: str | int | float | bool,
    ) -> bool:
        """缓存任务状态和少量监测字段。"""

        key = self._key("task", task_id, "status")
        values = {"status": status}
        values.update({name: str(value) for name, value in metadata.items()})
        result = await self._call("hset", key, mapping=values)
        await self._expire(key)
        return result is not None

    async def get_task_status(self, task_id: str) -> dict[str, str]:
        """读取任务状态；缓存不可用或不存在时返回空字典。"""

        result = await self._call("hgetall", self._key("task", task_id, "status"))
        if not isinstance(result, Mapping):
            return {}
        return {self._decode(key): self._decode(value) for key, value in result.items()}

    async def replace_search_run_article_ids(
        self,
        search_run_id: str,
        article_ids: Sequence[str],
    ) -> bool:
        """缓存一次检索运行的文章 ID，并按返回顺序建立有序集合。"""

        return await self._replace_sorted_ids(
            self._key("search_run", search_run_id, "article_ids"),
            article_ids,
        )

    async def get_search_run_article_ids(self, search_run_id: str) -> list[str]:
        """按检索结果顺序读取文章 ID。"""

        return await self._read_sorted_ids(
            self._key("search_run", search_run_id, "article_ids")
        )

    async def replace_group_article_ids(
        self,
        task_id: str,
        group_name: str,
        article_ids: Sequence[str],
    ) -> bool:
        """缓存一个质量评价分组的文章 ID，不保存文章正文。"""

        return await self._replace_sorted_ids(
            self._key("task", task_id, "group", group_name, "article_ids"),
            article_ids,
        )

    async def get_group_article_ids(self, task_id: str, group_name: str) -> list[str]:
        """读取一个分组中的文章 ID。"""

        return await self._read_sorted_ids(
            self._key("task", task_id, "group", group_name, "article_ids")
        )

    async def set_json(self, name: str, payload: Mapping[str, Any]) -> bool:
        """缓存小型 JSON 决策结果，例如 intent 或 sufficiency 判断。"""

        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        result = await self._call(
            "set",
            self._key("json", name),
            encoded,
            ex=self.config.ttl_seconds,
        )
        return result is not None

    async def get_json(self, name: str) -> dict[str, Any] | None:
        """读取小型 JSON 决策结果。"""

        result = await self._call("get", self._key("json", name))
        if result is None:
            return None
        try:
            decoded = json.loads(self._decode(result))
        except (TypeError, json.JSONDecodeError):
            logger.bind(
                component="redis_work_cache",
                event="invalid_cached_json",
            ).warning("Redis 中的 JSON 缓存无法解析")
            return None
        return decoded if isinstance(decoded, dict) else None

    async def _replace_sorted_ids(
        self,
        key: str,
        article_ids: Sequence[str],
    ) -> bool:
        cleaned_ids = [str(article_id).strip() for article_id in article_ids if str(article_id).strip()]
        deleted = await self._call("delete", key)
        mapping = {article_id: float(index) for index, article_id in enumerate(cleaned_ids)}
        if mapping:
            added = await self._call("zadd", key, mapping)
            await self._expire(key)
            return deleted is not None and added is not None
        return deleted is not None

    async def _read_sorted_ids(self, key: str) -> list[str]:
        result = await self._call("zrange", key, 0, -1)
        if not isinstance(result, (list, tuple)):
            return []
        return [self._decode(value) for value in result]

    async def _expire(self, key: str) -> None:
        await self._call("expire", key, self.config.ttl_seconds)

    async def _call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        try:
            method = getattr(self.client, method_name)
            result = method(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except Exception as exc:
            logger.bind(
                component="redis_work_cache",
                event="redis_operation_failed",
                operation=method_name,
                error_type=type(exc).__name__,
            ).warning("Redis 工作缓存操作失败")
            if not self.config.fail_open:
                raise
            return None

    def _key(self, *parts: str) -> str:
        return ":".join(
            [self._slug(self.config.namespace), *(self._slug(part) for part in parts)]
        )

    @staticmethod
    def _slug(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
        return cleaned or "empty"

    @staticmethod
    def _decode(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return str(value)
