"""共享请求名额与单篇执行；所有权、重试和指标统一管理。"""
from __future__ import annotations
import asyncio
import json
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from typing import Any, Callable
from uuid import UUID

from loguru import logger
from openai import APIConnectionError, InternalServerError, RateLimitError
from pydantic import ValidationError

from backend.app.db.repositories.quality_evaluation_results import OwnershipLost
from .schema import HydratedArticleBatch
from .subgraph import score_batch


class ModelRequestLimiter:
    """按类型轮转接纳请求；整个应用图共享一个实例。"""
    def __init__(self, limit: int = 5) -> None:
        if limit < 1:
            raise ValueError("模型并发上限必须大于 0")
        self.limit = limit
        self.active = 0
        self.peak_active = 0
        self._waiting: OrderedDict[str, deque] = OrderedDict()

    def _drain(self) -> None:
        while self.active < self.limit and self._waiting:
            key, queue = self._waiting.popitem(last=False)
            future = queue.popleft()
            if queue:
                self._waiting[key] = queue
            if not future.cancelled():
                self.active += 1
                self.peak_active = max(self.peak_active, self.active)
                future.set_result(None)

    @asynccontextmanager
    async def slot(self, study_type: str):
        started = time.perf_counter()
        future = asyncio.get_running_loop().create_future()
        self._waiting.setdefault(study_type, deque()).append(future)
        self._drain()
        try:
            await future
        except asyncio.CancelledError:
            if not future.cancelled():
                self.active -= 1
            self._drain()
            raise
        try:
            yield round((time.perf_counter() - started) * 1000, 3)
        finally:
            self.active -= 1
            self._drain()


class BatchExecutor:
    """执行冻结输入；只有持有当前租约的执行者可以提交结果。"""
    def __init__(self, runtime: Any, repository: Any, limiter: ModelRequestLimiter, *,
                 threshold: float = 3.0, max_attempts: int = 2,
                 deadline_seconds: float = 240, lease_seconds: float = 120,
                 backoff_seconds: float = 1) -> None:
        self.runtime = runtime
        self.repository = repository
        self.limiter = limiter
        self.threshold = threshold
        self.max_attempts = max_attempts
        self.deadline = deadline_seconds
        self.lease = lease_seconds
        self.backoff = backoff_seconds

    async def execute(self, task_id: str, batch_id: str,
                      progress: Callable[[dict], None] | None = None) -> dict:
        row, = await self.repository.read_batches(task_id, [batch_id])
        if row["status"] == "completed" or (
            row["status"] == "failed" and row["attempt_count"] >= self.max_attempts
        ):
            await self._progress(row, progress)
            return row
        snapshot = row["input_payload"]
        if snapshot["kind"] == "article" and snapshot["evaluator_version"] != self.runtime.contract_version:
            raise RuntimeError("评估规则或模型配置已变更，请创建新任务")
        token = await self.repository.claim_for_processing(
            task_id=task_id, batch_id=batch_id, group_id=row["group_id"],
            study_type=row["study_type"], article_ids=[UUID(a) for a in row["article_ids"]],
            lease_seconds=self.lease, max_attempts=self.max_attempts)
        if token is None:
            current, = await self.repository.read_batches(task_id, [batch_id])
            return current
        await self._progress({**row, "status": "processing"}, progress)
        started = time.perf_counter()
        stats = row["metrics_payload"] or {"attempts": []}
        stats["queue_wait_ms"] = round((time.time() - row["created_at"].timestamp()) * 1000, 3)
        try:
            async with asyncio.timeout(self.deadline):
                worker = asyncio.create_task(self._attempts(row, token, stats))
                keeper = asyncio.create_task(self._renew(task_id, batch_id, token))
                try:
                    done, _ = await asyncio.wait((worker, keeper), return_when=asyncio.FIRST_COMPLETED)
                    if keeper in done:
                        keeper.result()  # 续租失败立即停止模型执行，保留原始异常。
                    evaluation = worker.result()
                finally:
                    worker.cancel()
                    keeper.cancel()
                    await asyncio.gather(worker, keeper, return_exceptions=True)
                stats["execution_ms"] = round((time.perf_counter() - started) * 1000, 3)
                stats["job_total_ms"] = round((time.time() - row["created_at"].timestamp()) * 1000, 3)
                persisted = time.perf_counter()
                await self.repository.save_completed(task_id=task_id, batch_id=batch_id,
                    attempt_id=token, evaluation=evaluation, metrics=stats)
                logger.bind(task_id=task_id, batch_id=batch_id).info(
                    "质量评估完成：execution_ms={} completion_persist_ms={}", stats["execution_ms"],
                    round((time.perf_counter() - persisted) * 1000, 3))
        except asyncio.CancelledError:
            stats["execution_ms"] = round((time.perf_counter() - started) * 1000, 3)
            if stats["attempts"]:
                stats["attempts"][-1]["error_type"] = "CancelledError"
            try:
                await self.repository.save_failed(task_id=task_id, batch_id=batch_id,
                    attempt_id=token, error_type="CancelledError", error_message="任务已取消",
                    metrics=stats, status="cancelled")
            except OwnershipLost:
                pass  # API 取消事务可能已撤销该租约。
            raise
        except OwnershipLost:
            raise
        except Exception as exc:
            stats["execution_ms"] = round((time.perf_counter() - started) * 1000, 3)
            await self.repository.save_failed(task_id=task_id, batch_id=batch_id,
                attempt_id=token, error_type=type(exc).__name__, error_message=str(exc), metrics=stats)
        row, = await self.repository.read_batches(task_id, [batch_id])
        await self._progress(row, progress)
        return row

    async def _renew(self, task_id: str, batch_id: str, token: UUID) -> None:
        while True:
            await asyncio.sleep(self.lease / 4)
            await self.repository.renew_lease(task_id, batch_id, token, self.lease)

    async def _attempts(self, row: dict, token: UUID, stats: dict) -> dict:
        snapshot = row["input_payload"]
        retryable = (APIConnectionError, InternalServerError, RateLimitError,
                     ValidationError, json.JSONDecodeError, TimeoutError)
        for number in range(row["attempt_count"] + 1, self.max_attempts + 1):
            attempt: dict = {"number": number, "attempt_id": str(token)}
            error = None
            async with self.limiter.slot(row["study_type"]) as wait:
                attempt["limiter_wait_ms"] = wait
                attempt["in_flight"] = self.limiter.active
                stats["attempts"].append(attempt)
                await self.repository.begin_attempt(str(row["task_id"]), row["batch_id"], token, metrics=stats)
                metadata = {
                    "execution_id": str(row["execution_id"]),
                    "batch_id": row["batch_id"], "article_ids": row["article_ids"],
                    "study_type": row["study_type"], "attempt_id": str(token), "attempt": number,
                }
                try:
                    if snapshot["kind"] == "portfolio":
                        result = await self.runtime.evaluate_portfolio(
                            **snapshot["input"], metrics=attempt, metadata=metadata)
                        evaluation = {"assessment": result.model_dump(mode="json"), "scores": [],
                                      "retained_article_ids": [], "excluded_article_ids": []}
                    else:
                        batch = HydratedArticleBatch.model_validate(snapshot["batch"])
                        assessment = await self.runtime.evaluate_hydrated(batch,
                            intent_analysis=snapshot["intent_analysis"], search_plan=snapshot["search_plan"],
                            metrics=attempt, metadata={**metadata, "article_id": row["article_ids"][0]})
                        scoring = time.perf_counter()
                        evaluation = score_batch(batch, assessment, threshold=self.threshold).to_dict()
                        attempt["score_ms"] = round((time.perf_counter() - scoring) * 1000, 3)
                except Exception as exc:
                    error = exc
                    attempt["error_type"] = type(exc).__name__
                    attempt["error_message"] = str(exc)
                    if isinstance(exc, ValidationError):
                        attempt["validation_errors"] = exc.errors(include_input=False, include_context=False, include_url=False)
            persisted = time.perf_counter()
            await self.repository.record_metrics(str(row["task_id"]), row["batch_id"], token, stats)
            attempt["persist_ms"] = round((time.perf_counter() - persisted) * 1000, 3)
            logger.bind(task_id=str(row["task_id"]), execution_id=str(row["execution_id"]),
                        batch_id=row["batch_id"], study_type=row["study_type"]).info(
                "质量评估尝试：{}", attempt)
            if error is None:
                return evaluation
            if not isinstance(error, retryable) or number == self.max_attempts:
                raise error
            delay = self.backoff * 2 ** (number - 1)
            if isinstance(error, RateLimitError):
                delay = max(delay, float(error.response.headers.get("retry-after", delay)))
            stats["retry_backoff_ms"] = stats.get("retry_backoff_ms", 0) + delay * 1000
            await asyncio.sleep(delay)
        raise RuntimeError("该批次已耗尽尝试预算")

    async def _progress(self, row: dict, publish: Callable | None) -> None:
        if publish is not None:
            counts = await self.repository.progress(str(row["task_id"]), str(row["execution_id"]))
            publish({"type": "quality_progress", "batch_id": row["batch_id"],
                     "execution_id": str(row["execution_id"]), "batch_status": row["status"], **counts})
