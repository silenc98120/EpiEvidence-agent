"""质量评估 Judge 命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import selectors
from pathlib import Path
from uuid import UUID

from backend.app.db.session import ManagedSessionFactory

from .config import DEFAULT_CONFIG_PATH, load_config
from .extraction.programmatic_checks import build_dry_run_report
from .extraction.repository import EvaluationReadRepository
from .extraction.snapshot_builder import build_task_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按 task_id 审查质量评估持久化结果")
    parser.add_argument("--task-id", required=True, type=UUID)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只提取和预检，不调用 LLM、不落盘")
    mode.add_argument("--execute", action="store_true", help="执行 Judge（后续切片实现）")
    parser.add_argument("--max-llm-calls", type=int, default=None)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser


async def run(args: argparse.Namespace) -> int:
    if args.max_llm_calls is not None and args.max_llm_calls < 0:
        raise ValueError("--max-llm-calls 不能小于 0")
    if args.execute:
        raise NotImplementedError("--execute 将在 Judge 调用切片实现；当前请先使用 --dry-run")
    config = load_config(args.config)
    sessions = ManagedSessionFactory()
    try:
        snapshot = await build_task_snapshot(EvaluationReadRepository(sessions), args.task_id)
        report = build_dry_run_report(snapshot, config, args.max_llm_calls)
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return 0 if report.within_call_budget else 2
    finally:
        await sessions.dispose()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # psycopg async 不支持 Windows 默认的 ProactorEventLoop。这里显式创建
    # Selector loop，避免依赖 Python 3.14 已弃用的全局 event-loop policy。
    with asyncio.Runner(
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    ) as runner:
        return runner.run(run(args))
