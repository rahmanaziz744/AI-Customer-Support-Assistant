"""Model factory.

Every node obtains its model through `get_chat_model`, and tests swap in a
scripted fake by calling `set_model_factory`. That keeps the whole graph
runnable — and assertable — without network access or an API key.
"""

from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

ModelFactory = Callable[[str, dict[str, Any]], BaseChatModel]

_factory: ModelFactory | None = None

# Models that accept `output_config.effort`. Haiku 4.5 and the 4.5-generation
# Sonnet reject the parameter with a 400, so an allowlist is the safe shape:
# an unrecognised model simply runs without an effort hint instead of failing
# every request. Add new model families here as they are adopted.
_EFFORT_MODELS = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
)


def supports_effort(model: str) -> bool:
    """Whether `model` accepts `output_config.effort`."""
    return model.startswith(_EFFORT_MODELS)


def _default_factory(node: str, options: dict[str, Any]) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    settings = get_settings()
    model = settings.model_for(node)

    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": options.get("max_tokens", 4096),
        "api_key": settings.anthropic_api_key.get_secret_value(),
        "timeout": options.get("timeout", 120),
        "max_retries": 2,
    }

    # `effort` controls reasoning depth and token spend. It is nested under
    # output_config, and the Sonnet 5 / Opus 5 generation rejects
    # temperature/top_p entirely, so this is the only sampling-style dial we
    # set. Cheaper models such as Haiku 4.5 reject `effort` in turn — see
    # `supports_effort` — so it is omitted rather than sent blindly.
    effort = options.get("effort", settings.agent_effort)
    if effort and supports_effort(model):
        kwargs["model_kwargs"] = {"output_config": {"effort": effort}}

    return ChatAnthropic(**kwargs)


def set_model_factory(factory: ModelFactory | None) -> None:
    """Override (or with None, restore) how models are constructed."""
    global _factory
    _factory = factory


def get_chat_model(node: str, **options: Any) -> BaseChatModel:
    factory = _factory or _default_factory
    return factory(node, options)


def response_text(message: Any) -> str:
    """Flatten a chat message's content to plain text.

    Content is a list of blocks when thinking or tool use is involved; only the
    text blocks belong in a customer-facing draft.
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p).strip()
    return str(content).strip()


def usage_of(message: Any) -> dict[str, Any] | None:
    """Extract LangChain's normalised `usage_metadata`, when present."""
    return getattr(message, "usage_metadata", None)


def model_name_of(message: Any, fallback: str) -> str:
    meta = getattr(message, "response_metadata", None) or {}
    return meta.get("model_name") or meta.get("model") or fallback
