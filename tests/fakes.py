"""A scripted stand-in for anthropic.AsyncAnthropic: messages.create / .stream / .batches.

Responses come from a handler(params) -> FakeMessage. Prompt caching is simulated: a request whose tools + system
prefix was seen before reports cache_read_input_tokens, so tests catch volatile content in the cached prefix."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class FakeUsage:
    input_tokens: int = 1000
    output_tokens: int = 200
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str = "end_turn"
    model: str = "claude-sonnet-5"
    usage: FakeUsage = field(default_factory=FakeUsage)


def text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=f"toolu_{uuid.uuid4().hex[:12]}", name=name, input=args)


def text_message(text: str) -> FakeMessage:
    return FakeMessage([text_block(text)])


def json_message(obj: Any) -> FakeMessage:
    return FakeMessage([text_block(json.dumps(obj))])


def tool_message(*calls: tuple[str, dict[str, Any]]) -> FakeMessage:
    return FakeMessage([tool_block(n, a) for n, a in calls], stop_reason="tool_use")


Handler = Callable[[dict[str, Any]], FakeMessage]


class _Stream:
    def __init__(self, message: FakeMessage) -> None:
        self._message = message

    async def __aenter__(self) -> _Stream:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for block in self._message.content:
                if block.type == "text":
                    for i in range(0, len(block.text), 20):
                        yield SimpleNamespace(
                            type="content_block_delta",
                            delta=SimpleNamespace(type="text_delta", text=block.text[i : i + 20]),
                        )

        return gen()

    async def get_final_message(self) -> FakeMessage:
        return self._message


class _Batches:
    def __init__(self, owner: FakeAnthropic) -> None:
        self.owner = owner
        self.batches: dict[str, list[Any]] = {}
        self.ended: set[str] = set()

    async def create(self, requests: list[Any]) -> Any:
        batch_id = f"msgbatch_{uuid.uuid4().hex[:10]}"
        self.batches[batch_id] = list(requests)
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    async def retrieve(self, batch_id: str) -> Any:
        return SimpleNamespace(
            id=batch_id, processing_status="ended" if batch_id in self.ended else "in_progress"
        )

    async def results(self, batch_id: str) -> Any:
        requests = self.batches[batch_id]
        owner = self.owner

        async def gen() -> Any:
            for req in requests:
                message = owner.respond(dict(req["params"]))
                yield SimpleNamespace(
                    custom_id=req["custom_id"],
                    result=SimpleNamespace(type="succeeded", message=message),
                )

        return gen()


class _Messages:
    def __init__(self, owner: FakeAnthropic) -> None:
        self.owner = owner
        self.batches = _Batches(owner)

    async def create(self, **params: Any) -> FakeMessage:
        return self.owner.respond(params)

    def stream(self, **params: Any) -> _Stream:
        return _Stream(self.owner.respond(params))


class FakeAnthropic:
    def __init__(self, handler: Handler | None = None) -> None:
        self.handler = handler or (lambda params: text_message("ok"))
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)
        self._seen_prefixes: set[str] = set()

    def respond(self, params: dict[str, Any]) -> FakeMessage:
        self.calls.append(params)
        message = self.handler(params)
        prefix = hashlib.sha256(
            json.dumps([params.get("tools"), params.get("system")], sort_keys=True, default=str).encode()
        ).hexdigest()
        usage = FakeUsage(input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens)
        if params.get("system") and prefix in self._seen_prefixes:
            usage.cache_read_input_tokens = 1500
        elif params.get("system"):
            usage.cache_creation_input_tokens = 1500
        self._seen_prefixes.add(prefix)
        return FakeMessage(message.content, message.stop_reason, params.get("model", message.model), usage)

    def calls_for(self, model_prefix: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if str(c.get("model", "")).startswith(model_prefix)]
