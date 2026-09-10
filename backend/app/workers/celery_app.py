from __future__ import annotations

from celery import Celery

celery_app = Celery("epi-evidence-agent")

celery_app.config_from_object(
    "backend.app.workers.celery_config"
)