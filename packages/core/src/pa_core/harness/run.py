"""The morning brief run harness.

Guarantees, in order of how much grief they save:
  1. Exactly one run per (agent, logical_date) -- enforced by a DB constraint, so
     scheduling the same job at 06:05, 09:00 and 12:30 costs nothing.
  2. Exactly one process at a time -- flock, non-blocking, quiet on contention.
  3. A wedged generation cannot hold the lock forever -- wall-clock alarm.
  4. Nothing reaches the outside world on a dry run -- ctx.mutate(), not a flag each
     agent has to remember to check.
  5. A failure is never silent -- three alerting layers (see alerting.py).
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import structlog

from .. import logging as pa_logging
from ..config import Settings
from ..config import load as load_settings
from ..delivery.base import Delivered, Sink
from ..errors import AlreadyRan, AlreadyRunning, PaError
from ..llm.router import LLM, default_routes
from ..llm.usage import UsageLog
from ..store import db, migrate
from ..text import plain_dashes
from . import alerting
from .guards import single_instance, time_limit

log = structlog.get_logger("pa.run")


@dataclass(frozen=True, slots=True)
class AgentResult:
    title: str
    body_markdown: str
    urgent: bool = False
    metrics: dict[str, float] = field(default_factory=dict)
    attachment: str | None = None  # path to send as a document


@dataclass(slots=True)
class RunContext:
    run_id: str
    agent: str
    logical_date: date
    now: datetime
    dry_run: bool
    cfg: Settings
    con: sqlite3.Connection
    llm: LLM
    usage: UsageLog
    log: Any
    sinks: list[Sink] = field(default_factory=list)

    def mutate[T](self, what: str, fn: Callable[[], T], *, dry_value: T) -> T:
        """EVERY external side effect goes through here.

        On a dry run it logs the intent and returns dry_value instead of calling fn.
        That makes dry-run a property of the harness rather than something each agent
        must remember -- which is the only version of dry-run that stays true.
        """
        if self.dry_run:
            self.log.info("dry_run.skip", effect=what)
            return dry_value
        return fn()


def _git_sha(repo: str | None = None) -> str | None:
    # The deployed runtime has no .git; `just deploy` writes the version next to it.
    version = os.path.join(repo or os.getcwd(), "VERSION")
    if os.path.isfile(version):
        with open(version) as f:
            return f.read().strip() or None
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo or os.getcwd(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or None
    except OSError, subprocess.SubprocessError:
        return None


def build_sinks(cfg: Settings, con: sqlite3.Connection, *, agent: str, now: datetime) -> list[Sink]:
    """File sink FIRST, always. If every network sink fails the output still exists."""
    from .. import secrets as sec
    from ..delivery.macos_notify import MacNotifySink
    from ..delivery.markdown_file import MarkdownFileSink

    out: list[Sink] = []
    for name in cfg.sinks:
        if name == "file":
            out.append(MarkdownFileSink(cfg.outputs, agent=agent, now=now))
        elif name == "telegram":
            token = sec.get_optional("telegram_token")
            chat = db.kv_get(con, "telegram:chat_id") or cfg.telegram_chat_id
            if token and chat:
                from ..delivery.telegram import TelegramSink

                out.append(TelegramSink(token, chat))
            else:
                log.warning(
                    "sink.telegram_unconfigured",
                    hint="just telegram-token; then just telegram-bind",
                )
        elif name == "notify":
            out.append(MacNotifySink())
    # file sink first
    out.sort(key=lambda s: 0 if s.name == "file" else 1)
    return out


def build_llm(cfg: Settings, usage: UsageLog) -> LLM:
    from ..llm.ollama import OllamaBackend

    backends = {"ollama": OllamaBackend(cfg.ollama_url, num_ctx=cfg.num_ctx)}
    return LLM(
        backends=backends,
        routes=default_routes(cfg.model_small, cfg.model_large),
        usage=usage,
        default_timeout_s=cfg.llm_timeout_s,
        retries=cfg.llm_retries,
    )


def run_agent(
    agent: str,
    fn: Callable[[RunContext], AgentResult],
    *,
    logical_date: date | None = None,
    dry_run: bool = False,
    force: bool = False,
    timeout_s: int = 900,
    cfg: Settings | None = None,
    log_level: str = "INFO",
) -> int:
    """Returns a process exit code: 0 for ok or skipped, 1 for failed."""
    pa_logging.configure(level=log_level)
    cfg = cfg or load_settings()
    cfg.ensure_dirs()

    now = cfg.now()
    lday = logical_date or now.date()
    run_id = uuid.uuid4().hex[:16]
    blog = log.bind(agent=agent, run_id=run_id, logical_date=lday.isoformat(), dry_run=dry_run)

    try:
        with single_instance(cfg.lock_dir, agent):
            con = db.connect(cfg.platform_db)
            applied = migrate.migrate(con, db.MIGRATIONS)
            if applied:
                blog.info("db.migrated", applied=applied)

            alerting.stale_running_to_failed(con, older_than_s=timeout_s * 2)

            # A dry run touches nothing, so "already ran today" must never block it. It
            # only replaces the previous DRY row (dry_run is part of the unique key), so
            # it can never unblock or overwrite a real run.
            if force or dry_run:
                con.execute(
                    "delete from agent_run where agent=? and logical_date=? and dry_run=?",
                    (agent, lday.isoformat(), int(dry_run)),
                )
            try:
                con.execute(
                    """insert into agent_run
                       (run_id, agent, logical_date, dry_run, status, started_at, git_sha, host)
                       values (?,?,?,?,'running',?,?,?)""",
                    (
                        run_id,
                        agent,
                        lday.isoformat(),
                        int(dry_run),
                        now.isoformat(),
                        _git_sha(),
                        socket.gethostname(),
                    ),
                )
            except sqlite3.IntegrityError:
                blog.info(
                    "run.skipped",
                    reason="already ran for this logical_date",
                    hint="pass --force to regenerate",
                )
                return 0

            usage = UsageLog(con, cfg.monthly_budget_usd)
            ctx = RunContext(
                run_id=run_id,
                agent=agent,
                logical_date=lday,
                now=now,
                dry_run=dry_run,
                cfg=cfg,
                con=con,
                llm=build_llm(cfg, usage),
                usage=usage,
                log=blog,
                sinks=build_sinks(cfg, con, agent=agent, now=now),
            )

            try:
                with time_limit(timeout_s):
                    result = fn(ctx)
                _deliver(ctx, result)
            except BaseException as e:
                _finish_failed(con, run_id, now, cfg, e)
                blog.error(
                    "run.failed",
                    error_type=type(e).__name__,
                    error=str(e)[:500],
                    traceback=traceback.format_exc()[-2000:],
                )
                alerting.notify_failure(
                    cfg,
                    con,
                    agent=agent,
                    title=f"{agent} FAILED",
                    body=f"`{type(e).__name__}`: {e}\n\nrun_id `{run_id}`",
                    run_id=run_id,
                )
                return 1

            spent = usage.run_usd(run_id)
            dur = int((cfg.now() - now).total_seconds() * 1000)
            con.execute(
                """update agent_run set status='ok', finished_at=?, duration_ms=?, usd=?,
                          metrics_json=? where run_id=?""",
                (cfg.now().isoformat(), dur, spent, json.dumps(result.metrics), run_id),
            )
            db.kv_put(con, f"heartbeat:{agent}", cfg.now().isoformat())
            blog.info("run.ok", duration_ms=dur, usd=round(spent, 6), **result.metrics)
            return 0

    except AlreadyRunning as e:
        blog.info("run.skipped", reason=str(e))
        return 0
    except AlreadyRan as e:
        blog.info("run.skipped", reason=str(e))
        return 0
    except PaError as e:
        blog.error("run.setup_failed", error_type=type(e).__name__, error=str(e))
        return 1


def _finish_failed(con, run_id: str, started: datetime, cfg: Settings, e: BaseException) -> None:
    try:
        con.execute(
            """update agent_run set status='failed', finished_at=?, duration_ms=?,
                      error_type=?, error_msg=? where run_id=?""",
            (
                cfg.now().isoformat(),
                int((cfg.now() - started).total_seconds() * 1000),
                type(e).__name__,
                str(e)[:1000],
                run_id,
            ),
        )
    except sqlite3.Error:
        pass


def _deliver(ctx: RunContext, result: AgentResult) -> None:
    if ctx.dry_run:
        # A dry run delivers nowhere, so the artifact goes to stdout -- otherwise there
        # is nothing to look at. Logs are on stderr, so `> brief.md` captures just this.
        print(plain_dashes(f"# {result.title}\n\n{result.body_markdown}"), flush=True)
    for sink in ctx.sinks:
        d: Delivered = ctx.mutate(
            f"deliver via {sink.name}",
            lambda s=sink: s.deliver(
                title=result.title,
                body_markdown=result.body_markdown,
                run_id=ctx.run_id,
                urgent=result.urgent,
            ),
            dry_value=Delivered(sink.name, None, True, None),
        )
        if not ctx.dry_run:
            ctx.con.execute(
                """insert into delivery (run_id, sink, external_id, delivered_at, ok, error)
                   values (?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?)""",
                (ctx.run_id, d.sink, d.external_id, int(d.ok), d.error),
            )
        if d.ok:
            ctx.log.info("deliver.ok", sink=d.sink, id=d.external_id)
        else:
            ctx.log.warning("deliver.failed", sink=d.sink, error=d.error)


def run_watchdog(cfg: Settings | None = None, *, log_level: str = "INFO") -> int:
    pa_logging.configure(level=log_level)
    cfg = cfg or load_settings()
    cfg.ensure_dirs()
    con = db.connect(cfg.platform_db)
    migrate.migrate(con, db.MIGRATIONS)
    now = cfg.now()
    problems = alerting.watchdog_check(con, cfg, now)
    if not problems:
        log.info("watchdog.ok", checked=list(cfg.expected_daily))
        db.kv_put(con, "heartbeat:watchdog", now.isoformat())
        return 0
    log.error("watchdog.problems", count=len(problems), problems=problems)
    alerting.notify_failure(
        cfg,
        con,
        agent="watchdog",
        title="Good Morning Bobby: scheduled brief missing",
        body="\n".join(f"- {p}" for p in problems),
        run_id="watchdog",
    )
    return 1
