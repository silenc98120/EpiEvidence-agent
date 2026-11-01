"""通过真实 FastAPI 任务入口运行本地 agent test_set。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from loguru import logger


logger.remove()
logger.add(sys.stderr, level="CRITICAL")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


TEST_SET_PATH = Path(__file__).resolve().parents[1] / "test_set" / "agent_queries.json"
PROJECT_ROOT = TEST_SET_PATH.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_test_set(path: Path = TEST_SET_PATH) -> list[dict[str, Any]]:
    """加载并校验可重复运行的 agent query 集合。"""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("test_set 必须是非空 JSON 数组")
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("test_set 每一项必须是对象")
        case = dict(item)
        case_id = str(case.get("id", "")).strip()
        query = str(case.get("query", "")).strip()
        expected_intent = str(case.get("expected_intent", "")).strip()
        if not case_id or case_id in seen_ids:
            raise ValueError("test_set id 必须非空且唯一")
        if not query or not expected_intent:
            raise ValueError("test_set 每一项必须包含 query 和 expected_intent")
        seen_ids.add(case_id)
        cases.append(
            {
                "id": case_id,
                "query": query,
                "expected_intent": expected_intent,
            }
        )
    return cases


def _terminal(status: str) -> bool:
    return status in {"completed", "failed", "cancelled"}


async def run_case(
    client: httpx.AsyncClient,
    case: dict[str, Any],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    response = await client.post("/api/tasks", json={"query": case["query"]})
    response.raise_for_status()
    accepted = response.json()
    task_id = UUID(str(accepted["task_id"]))
    stages: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    snapshot: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snapshot = (await client.get(f"/api/tasks/{task_id}")).json()
        stage = snapshot.get("stage")
        if stage and (not stages or stages[-1] != stage):
            stages.append(stage)
        if _terminal(str(snapshot.get("status"))):
            break
        await asyncio.sleep(poll_interval_seconds)
    else:
        snapshot = {**snapshot, "status": "timeout"}

    intent: dict[str, Any] | None = None
    search_plan: dict[str, Any] | None = None
    graph = getattr(client, "_agent_graph", None)
    if graph is not None and hasattr(graph, "aget_state"):
        state_snapshot = await graph.aget_state(
            {"configurable": {"thread_id": str(task_id)}}
        )
        values = getattr(state_snapshot, "values", {})
        if isinstance(values, dict):
            raw_intent = values.get("intent_analysis")
            if isinstance(raw_intent, dict):
                intent = raw_intent
            raw_search_plan = values.get("search_plan")
            if isinstance(raw_search_plan, dict):
                search_plan = raw_search_plan
    return {
        "id": case["id"],
        "query": case["query"],
        "expected_intent": case["expected_intent"],
        "task_id": str(task_id),
        "status": snapshot.get("status"),
        "stage": snapshot.get("stage"),
        "stages": stages,
        "intent": intent,
        "search_plan": search_plan,
        "error": snapshot.get("error"),
        "result": snapshot.get("result"),
    }


async def run_test_set(
    *,
    test_set_path: Path = TEST_SET_PATH,
    case_ids: set[str] | None = None,
    timeout_seconds: float = 180.0,
    poll_interval_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    from backend.app.api.main import app

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 给 run_case 一个只读的已编译图引用，用来查看最终 checkpoint State。
            client._agent_graph = app.state.compiled_graph  # type: ignore[attr-defined]
            cases = load_test_set(test_set_path)
            if case_ids:
                cases = [case for case in cases if case["id"] in case_ids]
                found_ids = {case["id"] for case in cases}
                missing_ids = case_ids - found_ids
                if missing_ids:
                    raise ValueError(f"test_set 中不存在 case: {sorted(missing_ids)}")
            return [
                await run_case(
                    client,
                    case,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                )
                for case in cases
            ]


def main() -> None:
    from backend.app.runtime.checkpoint import configure_checkpoint_event_loop_policy

    configure_checkpoint_event_loop_policy()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--test-set", type=Path, default=TEST_SET_PATH)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="显示 LLM 调用异常摘要，用于定位鉴权、参数或网络问题",
    )
    args = parser.parse_args()
    if args.verbose:
        logger.remove()
        logger.add(
            sys.stderr,
            level="INFO",
            backtrace=True,
            diagnose=True,
        )
    results = asyncio.run(
        run_test_set(
            test_set_path=args.test_set,
            case_ids=set(args.case_id),
            timeout_seconds=args.timeout,
            poll_interval_seconds=args.poll_interval,
        )
    )
    print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
