import asyncio
import selectors
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from uvicorn import Config, Server


def selector_loop_factory():
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


async def serve():
    config = Config(
        "backend.app.api.main:app",
        host="127.0.0.1",
        port=8000,
        loop="asyncio",
        log_level="info",
    )
    server = Server(config)
    await server.serve()


if __name__ == "__main__":
    with asyncio.Runner(loop_factory=selector_loop_factory) as runner:
        runner.run(serve())
