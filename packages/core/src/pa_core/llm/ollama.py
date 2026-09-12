"""Ollama backend -- the DEFAULT, and on the zero-cost path the only one.

Hand-rolled on httpx2 rather than using the `ollama` package: that package depends on
`httpx` while `anthropic` 1.x depends on `httpx2`, and installing both would put two
near-identical HTTP stacks in one venv. We need exactly two endpoints.

NOTE: the `httpx2` distribution installs as the module `httpx2`, not `httpx`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence

import httpx2 as httpx
from pydantic import BaseModel, ValidationError

from ..errors import LLMBadOutput, LLMUnavailable
from .base import LLMResult, Usage


class OllamaBackend:
    name = "ollama"
    local = True  # => eligible for sensitive payloads

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        *,
        num_ctx: int = 8192,
        client: httpx.Client | None = None,
    ) -> None:
        self._num_ctx = num_ctx
        self._c = client or httpx.Client(
            base_url=base_url,
            # Generous read timeout (a 9B reduce step can take minutes), short connect
            # timeout so "Ollama isn't running" fails in 2s instead of hanging at 6am.
            timeout=httpx.Timeout(600.0, connect=2.0),
        )

    # ---------------------------------------------------------------- completion
    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        max_tokens: int = 2048,
        temperature: float = 0.2,
        schema: type[BaseModel] | None = None,
        cacheable_prefix: str | None = None,  # cloud-only concern; ignored here
        timeout_s: float = 600.0,
    ) -> LLMResult:
        body: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                # Deliberately NOT the model's nominal context. At 16 GB the KV cache
                # is the binding constraint, not the weights -- which is why callers
                # must map-reduce instead of sending one huge prompt.
                "num_ctx": self._num_ctx,
            },
        }
        if schema is not None:
            # Constrained decoding. Guarantees SHAPE, never correctness.
            body["format"] = schema.model_json_schema()

        t0 = time.monotonic()
        try:
            r = self._c.post("/api/chat", json=body, timeout=timeout_s)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise LLMUnavailable(f"ollama {e.response.status_code}: {e.response.text[:200]}") from e
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"ollama unreachable: {e}") from e

        d = r.json()
        text = d.get("message", {}).get("content", "")
        latency_ms = int((time.monotonic() - t0) * 1000)

        parsed: BaseModel | None = None
        if schema is not None:
            try:
                parsed = schema.model_validate_json(text)
            except ValidationError as e:
                raise LLMBadOutput(
                    f"{model} output failed {schema.__name__} validation: {e.errors()[:3]}"
                ) from e

        return LLMResult(
            text=text,
            parsed=parsed,
            usage=Usage(
                input_tokens=int(d.get("prompt_eval_count") or 0),
                output_tokens=int(d.get("eval_count") or 0),
            ),
            model=model,
            backend=self.name,
            latency_ms=latency_ms,
            cost_usd=0.0,
            truncated=d.get("done_reason") == "length",
        )

    # ---------------------------------------------------------------- embeddings
    def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        try:
            r = self._c.post("/api/embed", json={"model": model, "input": list(texts)})
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"ollama embed failed: {e}") from e
        return r.json()["embeddings"]

    # ---------------------------------------------------------------- health
    def health(self) -> tuple[bool, str]:
        try:
            r = self._c.get("/api/tags", timeout=3.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return False, f"ollama unreachable: {e}"
        tags = [m["name"] for m in r.json().get("models", [])]
        return True, f"ollama ok, {len(tags)} models"

    def installed_models(self) -> list[str]:
        r = self._c.get("/api/tags", timeout=3.0)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []))

    def bench(self, model: str, *, prompt_tokens: int = 2000, gen_tokens: int = 200) -> dict:
        """Measure real prefill and generation throughput on THIS machine.

        Every latency figure in the plan is an estimate from memory-bandwidth class.
        This replaces the estimate with a number.
        """
        filler = "the quick brown fox jumps over the lazy dog. " * ((prompt_tokens // 9) + 1)
        t0 = time.monotonic()
        res = self.complete(
            system="Reply with a single short sentence.",
            user=filler[: prompt_tokens * 5] + "\n\nSummarize the above in one sentence.",
            model=model,
            max_tokens=gen_tokens,
            temperature=0.0,
        )
        wall = time.monotonic() - t0
        gen_tps = res.usage.output_tokens / wall if wall else 0.0
        return {
            "model": model,
            "wall_s": round(wall, 2),
            "prompt_tokens": res.usage.input_tokens,
            "output_tokens": res.usage.output_tokens,
            "overall_output_tok_per_s": round(gen_tps, 1),
        }
