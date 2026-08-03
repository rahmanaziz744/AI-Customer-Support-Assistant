"""Versioned prompt loading.

Prompts live in files rather than string literals so they can be diffed and
reviewed like code. The active version per node is pinned in `PROMPT_VERSIONS`
and recorded on every run, so a result months later can be traced to the exact
prompt text that produced it.
"""

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent

# Bump deliberately; the old file stays in the repo for comparison.
PROMPT_VERSIONS: dict[str, str] = {
    "classify": "v1",
    "draft": "v1",
    "revise": "v1",
}


class PromptNotFound(FileNotFoundError):
    pass


@lru_cache(maxsize=32)
def load_prompt(node: str, version: str | None = None) -> str:
    resolved = version or PROMPT_VERSIONS.get(node)
    if resolved is None:
        raise PromptNotFound(f"No pinned prompt version for node {node!r}")

    path = PROMPT_DIR / f"{node}_{resolved}.md"
    if not path.exists():
        raise PromptNotFound(f"Prompt file not found: {path.name}")
    return path.read_text(encoding="utf-8").strip()


def prompt_version(node: str) -> str:
    return PROMPT_VERSIONS.get(node, "unknown")


def active_versions() -> dict[str, str]:
    return dict(PROMPT_VERSIONS)
