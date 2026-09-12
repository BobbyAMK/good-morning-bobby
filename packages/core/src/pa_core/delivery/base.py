from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Delivered:
    sink: str
    external_id: str | None
    ok: bool
    error: str | None = None


@runtime_checkable
class Sink(Protocol):
    name: str

    def deliver(
        self, *, title: str, body_markdown: str, run_id: str, urgent: bool = False
    ) -> Delivered: ...
