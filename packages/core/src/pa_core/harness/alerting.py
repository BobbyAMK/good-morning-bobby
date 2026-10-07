"""Failure alerting and the dead-man's-switch watchdog.

Three independent layers, because each can fail:

  1. in-process except -> Telegram urgent. Catches exceptions inside an agent. Fails
     when the network is down, the token is bad, or the process never started.
  2. ops/run.sh: non-zero exit -> log + osascript notification. Catches hard crashes,
     OOM kills, import errors. Fails when the job never ran at all.
  3. THIS watchdog, 09:30 and 13:00. Catches "nothing happened" -- a launchd job
     unloaded by an OS update, the Mac being off, a plist syntax error, a full disk,
     Ollama not running. It is the only layer that can see silent non-execution.

Layer 3 is the one that actually solves the stated problem. Its silence is meaningful
precisely because you have seen it speak.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import structlog

from ..config import Settings
from ..store import db

log = structlog.get_logger("pa.alert")


def stale_running_to_failed(con: sqlite3.Connection, *, older_than_s: int = 1800) -> int:
    """Reap rows a crashed process left in 'running'. Without this, the idempotency
    guard would refuse to ever run that agent again for that logical_date."""
    cur = con.execute(
        """update agent_run
              set status = 'failed',
                  error_type = 'Reaped',
                  error_msg = 'process died mid-run (no finished_at)',
                  finished_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')
            where status = 'running'
              and (julianday('now') - julianday(started_at)) * 86400 > ?""",
        (older_than_s,),
    )
    return cur.rowcount or 0


def preflight(cfg: Settings) -> list[str]:
    """Cheap checks that catch the common 'why did nothing happen' causes."""
    problems: list[str] = []

    # Ollama reachable, and the exact pinned tags still present.
    try:
        from ..llm.ollama import OllamaBackend

        be = OllamaBackend(cfg.ollama_url, num_ctx=cfg.num_ctx)
        ok, msg = be.health()
        if not ok:
            problems.append(f"ollama: {msg}")
        else:
            installed = set(be.installed_models())
            for want in {cfg.model_small, cfg.model_large}:
                # ollama reports bare names as "name:latest"
                if want not in installed and f"{want}:latest" not in installed:
                    problems.append(f"ollama: pinned model {want!r} is not installed")
    except Exception as e:
        problems.append(f"ollama preflight failed: {type(e).__name__}: {e}")

    # Disk space for the database, logs, and archived briefs.
    try:
        import shutil

        free_gb = shutil.disk_usage(cfg.data_dir).free / 1e9
        if free_gb < 2:
            problems.append(f"disk: only {free_gb:.1f} GB free")
    except OSError as e:
        problems.append(f"disk check failed: {e}")

    problems += services_not_running()
    return problems


# launchd KeepAlive services. Checked by asking launchd, NOT by a heartbeat: a heartbeat
# goes stale every time the Mac sleeps, and the watchdog replays right after wake.
SERVICES = ("com.pa.bot",)


def services_not_running(labels: tuple[str, ...] = SERVICES) -> list[str]:
    import os
    import subprocess
    from pathlib import Path

    out = []
    for label in labels:
        if not (Path.home() / "Library/LaunchAgents" / f"{label}.plist").exists():
            continue  # not installed on this machine: nothing to expect
        try:
            res = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as e:
            out.append(f"{label}: launchctl failed: {e}")
            continue
        if res.returncode != 0:
            out.append(f"{label}: installed but not loaded -- run: just install-brief")
        elif "state = running" not in res.stdout:
            exit_code = next(
                (
                    ln.split("=", 1)[1].strip()
                    for ln in res.stdout.splitlines()
                    if "last exit code" in ln
                ),
                "?",
            )
            out.append(f"{label}: not running (last exit code {exit_code}) -- see: just bot-logs")
    return out


def watchdog_check(con: sqlite3.Connection, cfg: Settings, now: datetime) -> list[str]:
    """Ask the only question that matters: did the thing that should exist, exist?"""
    problems: list[str] = []
    local = now.astimezone(cfg.zone)
    today = local.date().isoformat()
    hhmm = local.strftime("%H:%M")

    reaped = stale_running_to_failed(con)
    if reaped:
        problems.append(f"reaped {reaped} run(s) stuck in 'running'")

    for agent, expect_by in cfg.expected_daily.items():
        if hhmm < expect_by:
            continue
        row = con.execute(
            """select status, error_type, error_msg from agent_run
                where agent = ? and logical_date = ? and dry_run = 0
                order by started_at desc limit 1""",
            (agent, today),
        ).fetchone()
        if row is None:
            problems.append(f"{agent}: NO RUN for {today} -- the job never fired")
        elif row["status"] == "running":
            problems.append(f"{agent}: still 'running' -- process died mid-run")
        elif row["status"] == "failed":
            problems.append(f"{agent}: failed -- {row['error_type']}: {row['error_msg']}")

    # Produced output but it never reached the channel you actually read. Checking "any
    # sink" here was a real bug: the file sink always succeeds, so a revoked Telegram
    # token or an unbound chat looked like a clean delivery and nothing ever alerted.
    for r in con.execute(
        """select a.agent from agent_run a
            where a.logical_date = ? and a.status = 'ok' and a.dry_run = 0
              and not exists (select 1 from delivery d
                               where d.run_id = a.run_id and d.sink = ? and d.ok = 1)""",
        (today, cfg.primary_sink),
    ):
        problems.append(f"{r['agent']}: produced output but NOT delivered to {cfg.primary_sink}")

    problems += preflight(cfg)
    return problems


def notify_failure(
    cfg: Settings, con: sqlite3.Connection, *, agent: str, title: str, body: str, run_id: str
) -> None:
    """Best-effort urgent alert. Must never raise: it runs inside an except block."""
    from ..delivery.macos_notify import MacNotifySink

    try:
        MacNotifySink().deliver(title=title, body_markdown=body, run_id=run_id, urgent=True)
    except Exception:
        pass
    try:
        from .. import secrets as sec
        from ..delivery.telegram import TelegramSink

        token = sec.get_optional("telegram_token")
        chat = db.kv_get(con, "telegram:chat_id") or cfg.telegram_chat_id
        if token and chat:
            TelegramSink(token, chat).deliver(
                title=title, body_markdown=body, run_id=run_id, urgent=True
            )
    except Exception as e:
        log.warning("alert.telegram_failed", error=str(e)[:200])
