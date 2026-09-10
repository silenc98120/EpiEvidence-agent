from __future__ import annotations

import os

from kombu import Queue

broker_url = os.getenv(
    "CELERY_BROKER_URL",
    os.getenv("REDIS_URL", "redis://localhost:6379/0"),
)

task_serializer = "json"
accept_content = ["json"]
result_serializer = "json"

timezone = "Asia/Shanghai"
enable_utc = False

task_track_started = True

# 任务执行完成后才确认。
task_acks_late = True
task_reject_on_worker_lost = True

# Celery 启动时连接 Redis 失败，允许继续重试。
broker_connection_retry_on_startup = True

# 业务结果由 PostgreSQL 保存，不使用 Celery Result Backend。
task_ignore_result = True

# 不允许拼写错误的队列名称被自动创建。
task_create_missing_queues = False

task_default_queue = "default"

task_queues = (
    Queue("default", routing_key="default"),
    Queue("quality-rct", routing_key="quality-rct"),
    Queue("quality-cohort", routing_key="quality-cohort"),
    Queue("quality-case-control", routing_key="quality-case-control"),
    Queue(
        "quality-cross-sectional-analytical",
        routing_key="quality-cross-sectional-analytical",
    ),
    Queue(
        "quality-cross-sectional-prevalence",
        routing_key="quality-cross-sectional-prevalence",
    ),
    Queue(
        "quality-systematic-review",
        routing_key="quality-systematic-review",
    ),
    Queue(
        "quality-meta-analysis",
        routing_key="quality-meta-analysis",
    ),
    Queue(
        "quality-qualitative-systematic-review",
        routing_key="quality-qualitative-systematic-review",
    ),
    Queue(
        "quality-umbrella-review",
        routing_key="quality-umbrella-review",
    ),
    Queue(
        "quality-narrative-review",
        routing_key="quality-narrative-review",
    ),
)


imports = ("backend.app.workers.celery_tasks",)
