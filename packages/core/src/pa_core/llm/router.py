"""The LLM facade.

THE design decision here: `sensitive: bool = True` is the DEFAULT. A call must
explicitly opt out of privacy to be allowed off this machine. Mail, calendar, notes
and finances therefore cannot leak by omission -- only by someone writing
`sensitive=False`, which a lint test greps for.

Why that matters concretely: Google's own Gemini API terms say free-tier content is
used to "provide, improve, and develop Google products" and that "human reviewers may
read, annotate, and process your API input and output". The paid tier does not. So a
free cloud tier is attractive on price and disqualifying on privacy, and the type
system should make the distinction impossible to forget.
"""

from __future__ import annotations

import random
import time
from collections.abc import Sequence

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
        persona: str | None = None,
        retries: int | None = None,
        timeout_s: float | None = None,
    ) -> LLMResult:
        # The persona card is PREPENDED to system, never interpolated into user, so a
        # cloud backend can put a cache breakpoint after a stable prefix by construction.
        if persona:
            system = f"{persona}\n\n{system}"

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
                        cacheable_prefix=system if persona else None,
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

    # --------------------------------------------------------------- map-reduce
    def map_reduce(
        self,
        *,
        chunks: Sequence[str],
        map_system: str,
        map_user_tmpl: str,
        reduce_system: str,
        reduce_user_tmpl: str,
        agent: str,
        run_id: str | None = None,
        sensitive: bool = True,
        reduce_schema: type[BaseModel] | None = None,
        map_task: Task = Task.SUMMARIZE,
        reduce_task: Task = Task.SYNTHESIZE,
        map_max_tokens: int = 512,
        reduce_max_tokens: int = 2048,
        persona: str | None = None,
    ) -> tuple[LLMResult, list[str]]:
        """MAP each chunk with the small model, then REDUCE the summaries with the large one.

        This is not an optimization, it is the only shape that fits. At num_ctx=8192 a
        single 21K-token prompt does not run, and even if it did the prefill alone would
        dominate the wall clock. `{chunk}` / `{parts}` are the template placeholders.
        """
        parts: list[str] = []
        for i, chunk in enumerate(chunks):
            res = self.complete(
                task=map_task,
                system=map_system,
                user=map_user_tmpl.format(chunk=chunk, index=i + 1, total=len(chunks)),
                agent=agent,
                run_id=run_id,
                sensitive=sensitive,
                max_tokens=map_max_tokens,
            )
            parts.append(res.text.strip())

        reduced = self.complete(
            task=reduce_task,
            system=reduce_system,
            user=reduce_user_tmpl.format(parts="\n\n".join(parts), n=len(parts)),
            agent=agent,
            run_id=run_id,
            sensitive=sensitive,
            schema=reduce_schema,
            max_tokens=reduce_max_tokens,
            persona=persona,
        )
        return reduced, parts

    # --------------------------------------------------------------- embeddings
    def embed(
        self,
        texts: Sequence[str],
        *,
        model: str,
        agent: str = "core",
        run_id: str | None = None,
    ) -> list[list[float]]:
        for be in self._backends.values():
            if be.local:
                return be.embed(texts, model=model)
        raise NoEligibleBackend("no local backend for embeddings")

    def health(self) -> dict[str, tuple[bool, str]]:
        return {name: be.health() for name, be in self._backends.items()}


def default_routes(model_small: str, model_large: str) -> dict[Task, list[str]]:
    return {
        Task.CLASSIFY: [f"ollama:{model_small}"],
        Task.EXTRACT: [f"ollama:{model_small}"],
        Task.SUMMARIZE: [f"ollama:{model_small}"],
        Task.SYNTHESIZE: [f"ollama:{model_large}"],
        Task.REWRITE: [f"ollama:{model_large}"],
    }


def tier_of(task: Task) -> str:
    return "small" if task in SMALL_TASKS else "large"
