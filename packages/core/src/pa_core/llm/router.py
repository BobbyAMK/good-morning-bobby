"""Local inference routing, structured output validation, retries, and usage logging.

Sensitive inputs are restricted to local backends. The morning brief configures Ollama.
"""

from __future__ import annotations

import random
import time

import structlog
from pydantic import BaseModel

from ..errors import (
    LLMBadOutput,
    LLMError,
    LLMUnavailable,
    NoEligibleBackend,
)
from .base import SMALL_TASKS, LLMBackend, LLMResult, Route, Task
from .usage import UsageLog

log = structlog.get_logger("pa.llm")


class LLM:
    def __init__(
        self,
        *,
        backends: dict[str, LLMBackend],
        routes: dict[Task, list[str]],
        usage: UsageLog | None = None,
        routes_nonsensitive: dict[Task, list[str]] | None = None,
        allow_cloud: bool = False,
        default_timeout_s: float = 600.0,
        retries: int = 2,
    ) -> None:
        self._backends = backends
        self._routes = routes
        self._routes_ns = routes_nonsensitive or {}
        self._allow_cloud = allow_cloud
        self._usage = usage
        self._timeout = default_timeout_s
        self._retries = retries

    # ------------------------------------------------------------------ routing
    def _chain(self, task: Task, sensitive: bool) -> list[tuple[LLMBackend, str]]:
        specs = list(self._routes.get(task, []))
        if not sensitive and self._allow_cloud:
            # Non-sensitive routes are PREPENDED, and are reachable only when the
            # master switch is on. Sensitive calls never see this list at all.
            specs = list(self._routes_ns.get(task, [])) + specs

        out: list[tuple[LLMBackend, str]] = []
        for spec in specs:
            bname, model = Route.split(spec)
            be = self._backends.get(bname)
            if be is None:
                continue
            if sensitive and not be.local:
                continue  # default-deny: sensitive payloads never leave the machine
            out.append((be, model))

        if not out:
            raise NoEligibleBackend(
                f"no backend for task={task} sensitive={sensitive} "
                f"(allow_cloud={self._allow_cloud}); configured routes: {specs}"
            )
        return out

    # --------------------------------------------------------------- completion
    def complete(
        self,
        *,
        task: Task,
        system: str,
        user: str,
        agent: str,
        run_id: str | None = None,
        sensitive: bool = True,
        schema: type[BaseModel] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        retries: int | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        tries = self._retries if retries is None else retries
        chain = self._chain(task, sensitive)
        last: Exception | None = None

        for be, model in chain:
            for attempt in range(1, tries + 2):
                if not be.local:
                    if self._usage:
                        self._usage.assert_within_budget()
                t0 = time.monotonic()
                try:
                    res = be.complete(
                        system=system,
                        user=user,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        schema=schema,
                        cacheable_prefix=None,
                        timeout_s=timeout_s or self._timeout,
                    )
                except (LLMUnavailable, LLMBadOutput) as e:
                    last = e
                    self._log_failure(run_id, agent, task, be, model, sensitive, t0, e)
                    log.warning(
                        "llm.attempt_failed",
                        task=str(task),
                        backend=be.name,
                        model=model,
                        attempt=attempt,
                        error=str(e)[:200],
                    )
                    if attempt <= tries:
                        # jittered backoff; a schema failure retries immediately-ish
                        time.sleep(min(2 ** (attempt - 1), 8) * (0.5 + random.random()))
                        continue
                    break  # move to the next backend in the chain
                else:
                    cost = 0.0
                    if self._usage:
                        cost = self._usage.record(
                            run_id=run_id,
                            agent=agent,
                            task=task,
                            backend=be.name,
                            model=model,
                            sensitive=sensitive,
                            usage=res.usage,
                            latency_ms=res.latency_ms,
                            ok=True,
                        )
                    if res.truncated:
                        log.warning(
                            "llm.truncated",
                            task=str(task),
                            model=model,
                            output_tokens=res.usage.output_tokens,
                            hint="raise max_tokens or shorten the requested output",
                        )
                    log.info(
                        "llm.ok",
                        task=str(task),
                        backend=be.name,
                        model=model,
                        in_tok=res.usage.input_tokens,
                        out_tok=res.usage.output_tokens,
                        ms=res.latency_ms,
                        usd=round(cost, 6),
                        attempt=attempt,
                    )
                    object.__setattr__(res, "cost_usd", cost)
                    object.__setattr__(res, "attempts", attempt)
                    return res

        raise LLMError(f"all backends failed for task={task}: {last}") from last

    def _log_failure(self, run_id, agent, task, be, model, sensitive, t0, e) -> None:
        if not self._usage:
            return
        from .base import Usage

        self._usage.record(
            run_id=run_id,
            agent=agent,
            task=task,
            backend=be.name,
            model=model,
            sensitive=sensitive,
            usage=Usage(),
            latency_ms=int((time.monotonic() - t0) * 1000),
            ok=False,
            error=f"{type(e).__name__}: {e}"[:500],
        )

    def health(self) -> dict[str, tuple[bool, str]]:
        return {name: be.health() for name, be in self._backends.items()}


def default_routes(model_small: str, model_large: str) -> dict[Task, list[str]]:
    return {
        Task.CLASSIFY: [f"ollama:{model_small}"],
        Task.EXTRACT: [f"ollama:{model_small}"],
        Task.SUMMARIZE: [f"ollama:{model_small}"],
        Task.SYNTHESIZE: [f"ollama:{model_large}"],
    }


def tier_of(task: Task) -> str:
    return "small" if task in SMALL_TASKS else "large"
