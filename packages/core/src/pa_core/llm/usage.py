"""Token and cost logging.

Local calls are logged too, at cost_usd = 0.0, deliberately: after a month you have
real token and latency histograms, which is the only honest input to deciding whether
the paid path is worth anything to you.
"""

from __future__ import annotations

import sqlite3

from ..errors import BudgetExceeded
from .base import Task, Usage

# (input, output, cache_write_5m, cache_read) USD per MTok.
# Verified 2026-09-12. Model ids live here, never inlined at a call site, so a
# deprecation is a one-line edit.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5": (5.00, 25.00, 6.25, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
}

WEB_SEARCH_USD_PER_CALL = 10 / 1000  # server-side web_search: $10 per 1,000 searches


def price_usd(model: str, u: Usage) -> float:
    p = PRICES.get(model)
    if p is None:
        return 0.0  # local models are free
    pin, pout, pcw, pcr = p
    return (
        u.input_tokens * pin
        + u.output_tokens * pout
        + u.cache_write_tokens * pcw
        + u.cache_read_tokens * pcr
    ) / 1_000_000


class UsageLog:
    def __init__(self, con: sqlite3.Connection, monthly_budget_usd: float | None = None) -> None:
        self._con = con
        self._budget = monthly_budget_usd

    def record(
        self,
        *,
        run_id: str | None,
        agent: str,
        task: Task,
        backend: str,
        model: str,
        sensitive: bool,
        usage: Usage,
        latency_ms: int,
        ok: bool,
        error: str | None = None,
    ) -> float:
        cost = price_usd(model, usage)
        self._con.execute(
            """insert into llm_call
               (run_id, agent, task, backend, model, sensitive,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                cost_usd, latency_ms, ok, error, created_at)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
            (
                run_id,
                agent,
                str(task),
                backend,
                model,
                int(sensitive),
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_tokens,
                usage.cache_write_tokens,
                cost,
                latency_ms,
                int(ok),
                error,
            ),
        )
        return cost

    def month_to_date_usd(self) -> float:
        return self._con.execute(
            "select coalesce(sum(cost_usd),0) from llm_call "
            "where created_at >= strftime('%Y-%m-01','now')"
        ).fetchone()[0]

    def run_usd(self, run_id: str) -> float:
        return self._con.execute(
            "select coalesce(sum(cost_usd),0) from llm_call where run_id = ?", (run_id,)
        ).fetchone()[0]

    def assert_within_budget(self) -> None:
        """Called before any NON-local call. Local calls are never budget-gated."""
        if self._budget is None:
            return
        spent = self.month_to_date_usd()
        if spent >= self._budget:
            raise BudgetExceeded(spent, self._budget)
