"""Embed the policy corpus into Postgres.

    python scripts/ingest_policies.py [--force]
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.core.db import session_scope  # noqa: E402
from app.core.logging import configure_logging, get_logger  # noqa: E402
from app.rag.ingest import ingest_policies  # noqa: E402

logger = get_logger("ingest")


async def main(force: bool) -> int:
    configure_logging()
    settings = get_settings()

    async with session_scope() as db:
        report = await ingest_policies(db, settings.policies_dir, force=force)

    logger.info("ingest_complete", summary=report.summary())
    print(f"Policy ingestion complete: {report.summary()}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force", action="store_true", help="re-embed every document, even if unchanged"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.force)))
