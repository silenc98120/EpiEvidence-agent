from __future__ import annotations

from typing import Any

import pytest

from app.cache.redis_work_cache import RedisWorkCache, RedisWorkCacheConfig


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, float]] = {}
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def ping(self) -> bool:
        return True

    async def hset(self, key: str, *, mapping: dict[str, str]) -> int:
        self.hashes.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def delete(self, key: str) -> int:
        existed = key in self.sorted_sets or key in self.hashes or key in self.values
        self.sorted_sets.pop(key, None)
        self.hashes.pop(key, None)
        self.values.pop(key, None)
        return int(existed)

    async def expire(self, key: str, seconds: int) -> bool:
        self.expirations[key] = seconds
        return True

    async def zadd(self, key: str, mapping: dict[str, float]) -> int:
        self.sorted_sets[key] = dict(mapping)
        return len(mapping)

    async def zrange(self, key: str, start: int, stop: int) -> list[str]:
        ordered = [
            member
            for member, _score in sorted(
                self.sorted_sets.get(key, {}).items(), key=lambda item: item[1]
            )
        ]
        return ordered[start : stop + 1 if stop >= 0 else None]

    async def set(self, key: str, value: str, *, ex: int) -> bool:
        self.values[key] = value
        self.expirations[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


class BrokenRedis:
    async def zrange(self, *_args: Any) -> list[str]:
        raise ConnectionError("redis unavailable")


@pytest.mark.anyio
async def test_work_cache_stores_ordered_article_ids_and_status() -> None:
    fake = FakeRedis()
    cache = RedisWorkCache(
        config=RedisWorkCacheConfig(namespace="test", ttl_seconds=600),
        client=fake,
    )

    assert await cache.ping() is True
    assert await cache.replace_search_run_article_ids("run-1", ["a3", "a1", "a2"]) is True
    assert await cache.get_search_run_article_ids("run-1") == ["a3", "a1", "a2"]
    assert await cache.set_task_status("task-1", "screening", group_count=2) is True
    assert await cache.get_task_status("task-1") == {
        "status": "screening",
        "group_count": "2",
    }


@pytest.mark.anyio
async def test_work_cache_stores_group_ids_but_not_article_content() -> None:
    fake = FakeRedis()
    cache = RedisWorkCache(
        config=RedisWorkCacheConfig(namespace="test", ttl_seconds=600),
        client=fake,
    )

    await cache.replace_group_article_ids(
        "task-1",
        "randomized controlled trial",
        ["article-1", "article-2"],
    )

    assert await cache.get_group_article_ids("task-1", "randomized controlled trial") == [
        "article-1",
        "article-2",
    ]
    assert all("abstract" not in key for key in fake.values)
    assert all("abstract" not in key for key in fake.hashes)


@pytest.mark.anyio
async def test_work_cache_can_store_small_json_decisions() -> None:
    fake = FakeRedis()
    cache = RedisWorkCache(
        config=RedisWorkCacheConfig(namespace="test", ttl_seconds=600),
        client=fake,
    )

    assert await cache.set_json(
        "intent:query-hash",
        {"intent_category": "new_research", "confidence": 0.91},
    ) is True
    assert await cache.get_json("intent:query-hash") == {
        "intent_category": "new_research",
        "confidence": 0.91,
    }


@pytest.mark.anyio
async def test_work_cache_fails_open_when_redis_is_unavailable() -> None:
    cache = RedisWorkCache(
        config=RedisWorkCacheConfig(namespace="test", ttl_seconds=600, fail_open=True),
        client=BrokenRedis(),
    )

    assert await cache.get_search_run_article_ids("run-1") == []


@pytest.mark.anyio
async def test_work_cache_can_fail_closed_when_required() -> None:
    cache = RedisWorkCache(
        config=RedisWorkCacheConfig(namespace="test", ttl_seconds=600, fail_open=False),
        client=BrokenRedis(),
    )

    with pytest.raises(ConnectionError):
        await cache.get_search_run_article_ids("run-1")
