"""Event-loop selection.

psycopg's async mode cannot run on Windows' default ProactorEventLoop, and we
use psycopg for both the SQLAlchemy engine and the LangGraph Postgres
checkpointer. Selecting the selector loop keeps a single driver across the
project instead of splitting access between two.

Two entry points, because they are needed at different moments:

- `configure_event_loop_policy()` covers scripts and tests, which create their
  loop through `asyncio.run()` and therefore honour the policy.
- `loop_factory()` covers uvicorn, which since 0.36 passes an explicit
  `loop_factory` to `asyncio.run()` and so bypasses the policy entirely.

Both are no-ops off Windows, which is where the app runs in Docker.
"""

import asyncio
import selectors
import sys
from collections.abc import Callable

IS_WINDOWS = sys.platform == "win32"


def configure_event_loop_policy() -> None:
    if IS_WINDOWS:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    """Loop factory for `asyncio.run(..., loop_factory=...)`, or None to default."""
    if IS_WINDOWS:
        return lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    return None


configure_event_loop_policy()
