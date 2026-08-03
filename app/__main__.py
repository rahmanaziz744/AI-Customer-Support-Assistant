"""Run the API server: `python -m app`.

Starts uvicorn on a loop we choose ourselves. Since uvicorn 0.36 the server
passes its own `loop_factory` to `asyncio.run()`, which bypasses the event loop
policy — so on Windows the ProactorEventLoop would come back and psycopg's
async mode would fail. Driving `server.serve()` directly is the supported way
to control that.

On Linux this is equivalent to `uvicorn app.main:app`, which also works.
"""

import asyncio

import app.core.runtime as runtime


def main() -> None:
    import uvicorn

    from app.core.config import get_settings

    settings = get_settings()
    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)

    factory = runtime.loop_factory()
    if factory is not None:
        asyncio.run(server.serve(), loop_factory=factory)
    else:
        asyncio.run(server.serve())


if __name__ == "__main__":
    main()
