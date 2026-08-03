"""Scripted stand-in for the chat model.

Lets the whole graph — routing, guardrails, tool handling, the approval
interrupt — run deterministically with no API key and no network. Only the
three methods the nodes actually use are implemented; duck typing does the
rest, since `get_chat_model` is the single seam every node goes through.
"""

from typing import Any

from langchain_core.messages import AIMessage

from app.agents.llm import set_model_factory


class ScriptedResponse:
    """One canned model reply."""

    def __init__(
        self,
        text: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
        structured: Any = None,
        usage: dict[str, Any] | None = None,
        model: str = "claude-opus-5",
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls or []
        self.structured = structured
        self.usage = usage or {"input_tokens": 900, "output_tokens": 180}
        self.model = model

    def to_message(self) -> AIMessage:
        return AIMessage(
            content=self.text,
            tool_calls=[
                {
                    "name": call["name"],
                    "args": call.get("args", {}),
                    "id": call.get("id", f"call_{i}"),
                    "type": "tool_call",
                }
                for i, call in enumerate(self.tool_calls)
            ],
            usage_metadata={
                "input_tokens": self.usage.get("input_tokens", 0),
                "output_tokens": self.usage.get("output_tokens", 0),
                "total_tokens": (
                    self.usage.get("input_tokens", 0) + self.usage.get("output_tokens", 0)
                ),
            },
            response_metadata={"model_name": self.model},
        )


class _StructuredWrapper:
    def __init__(self, model: "ScriptedModel", include_raw: bool) -> None:
        self._model = model
        self._include_raw = include_raw

    async def ainvoke(self, messages: Any, **_: Any) -> Any:
        response = self._model._next()
        if self._include_raw:
            return {"parsed": response.structured, "raw": response.to_message()}
        return response.structured


class ScriptedModel:
    """Replays a queue of `ScriptedResponse`s, one per invocation."""

    def __init__(self, responses: list[ScriptedResponse] | ScriptedResponse) -> None:
        self._responses = [responses] if isinstance(responses, ScriptedResponse) else list(
            responses
        )
        self.calls: list[Any] = []
        self.bound_tools: list[Any] = []

    def _next(self) -> ScriptedResponse:
        if not self._responses:
            raise AssertionError("ScriptedModel ran out of scripted responses")
        # Repeat the final response rather than failing, so a node that retries
        # does not need every attempt scripted.
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    def with_structured_output(self, _schema: Any, include_raw: bool = False) -> Any:
        return _StructuredWrapper(self, include_raw)

    def bind_tools(self, tools: list[Any]) -> "ScriptedModel":
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages: Any, **_: Any) -> AIMessage:
        self.calls.append(messages)
        return self._next().to_message()


def use_scripted_models(script: dict[str, list[ScriptedResponse] | ScriptedResponse]) -> dict:
    """Install a per-node script. Returns the node->model map for assertions."""
    models: dict[str, ScriptedModel] = {
        node: ScriptedModel(responses) for node, responses in script.items()
    }

    def factory(node: str, _options: dict[str, Any]) -> Any:
        if node not in models:
            raise AssertionError(f"No scripted model for node {node!r}")
        return models[node]

    set_model_factory(factory)
    return models


def restore_real_models() -> None:
    set_model_factory(None)
