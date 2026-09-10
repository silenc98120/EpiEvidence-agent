"""将同一 Langfuse client 安全注入 LangGraph 节点。"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from functools import wraps
from typing import Any, Literal

from langchain_core.callbacks import BaseCallbackHandler
from langfuse import Langfuse


ObservationType = Literal[
    "span",
    "agent",
    "tool",
    "chain",
    "retriever",
]
MetricBuilder = Callable[[Mapping[str, Any] | Any], dict[str, Any]]


class LangfuseRuntime:
    """节点共享的 Langfuse client 与 LangChain callback。

    ``callback_handler`` 仅传给 LangChain LLM runnable。其他节点使用 ``client``
    创建有语义的 observation，并且只上传调用方构造的安全指标对象。
    """

    def __init__(
        self,
        *,
        client: Langfuse | None,
        callback_handler: BaseCallbackHandler | None,
    ) -> None:
        self.client = client
        self.callback_handler = callback_handler

    def wrap_node(
        self,
        *,
        name: str,
        as_type: ObservationType,
        node: Callable[..., Any],
        input_metrics: MetricBuilder,
        output_metrics: MetricBuilder,
    ) -> Callable[..., Any]:
        """用安全的节点指标包装同步或异步 LangGraph 节点。"""

        if self.client is None:
            return node

        if inspect.iscoroutinefunction(node):

            @wraps(node)
            async def observed_async(*args: Any, **kwargs: Any) -> Any:
                state = args[0] if args else kwargs
                with self.client.start_as_current_observation(
                    name=name,
                    as_type=as_type,
                    input=input_metrics(state),
                ) as observation:
                    try:
                        result = await node(*args, **kwargs)
                    except Exception as exc:
                        observation.update(
                            level="ERROR",
                            status_message=f"{name} failed",
                            output={
                                "status": "failed",
                                "error_type": type(exc).__name__,
                            },
                        )
                        raise
                    observation.update(output=output_metrics(result))
                    return result

            return observed_async

        @wraps(node)
        def observed_sync(*args: Any, **kwargs: Any) -> Any:
            state = args[0] if args else kwargs
            with self.client.start_as_current_observation(
                name=name,
                as_type=as_type,
                input=input_metrics(state),
            ) as observation:
                try:
                    result = node(*args, **kwargs)
                except Exception as exc:
                    observation.update(
                        level="ERROR",
                        status_message=f"{name} failed",
                        output={
                            "status": "failed",
                            "error_type": type(exc).__name__,
                        },
                    )
                    raise
                observation.update(output=output_metrics(result))
                return result

        return observed_sync
