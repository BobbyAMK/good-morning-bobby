"""The LLM seam: one Protocol, several backends, routing by (Task, sensitive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class Task(StrEnum):
    """What the call is FOR. Maps to a model tier -- never to a model name at the
    call site, so re-tiering every agent is a config change, not a refactor."""

    CLASSIFY = "classify"  # labels, routing, yes/no              -> small model
    EXTRACT = "extract"  # structured fields out of text        -> small model
    SUMMARIZE = "summarize"  # the per-source MAP step              -> small model
    SYNTHESIZE = "synthesize"  # the REDUCE step: brief, verdict, plan -> largest local


SMALL_TASKS = frozenset({Task.CLASSIFY, Task.EXTRACT, Task.SUMMARIZE})


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    parsed: BaseModel | None
    usage: Usage
    model: str
    backend: str
    latency_ms: int
    cost_usd: float = 0.0
    truncated: bool = False  # hit the output cap -- the output is incomplete
    attempts: int = 1


@runtime_checkable
class LLMBackend(Protocol):
    name: str
    local: bool  # True => safe for sensitive payloads. This is the whole privacy seam.

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int,
        temperature: float,
        schema: type[BaseModel] | None,
        cacheable_prefix: str | None,
        timeout_s: float,
    ) -> LLMResult: ...

    def health(self) -> tuple[bool, str]: ...


@dataclass(slots=True)
class Route:
    """Ordered backend preference for one task, as "backend:model" strings."""

    task: Task
    chain: list[str] = field(default_factory=list)

    @staticmethod
    def split(spec: str) -> tuple[str, str]:
        backend, _, model = spec.partition(":")
        if not model:
            raise ValueError(f"route entry must be 'backend:model', got {spec!r}")
        return backend, model
