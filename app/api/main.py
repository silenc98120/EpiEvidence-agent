"""FastAPI application factory for the local research workbench."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app.api.routes import create_router
from app.api.services.event_broker import EventBroker
from app.api.services.task_runner import TaskRunner
from app.api.services.task_store import TaskStore


def create_app(graph: Any | None = None) -> FastAPI:
    """Create an app with injectable Agent graph and in-process services."""

    store = TaskStore()
    broker = EventBroker()
    runner = TaskRunner(graph, store, broker)
    app = FastAPI(title="EpiEvidence Research API")
    app.include_router(create_router(store, runner, broker))
    app.state.task_store = store
    app.state.event_broker = broker
    app.state.task_runner = runner
    return app


app = create_app()
