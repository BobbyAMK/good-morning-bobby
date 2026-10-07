"""Admin CLI: setup, health, and the watchdog."""

from __future__ import annotations

import typer

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Good Morning Bobby setup, health, and run history.",
)


def _cfg():
    from .config import load

    c = load()
    c.ensure_dirs()
    return c


def _con():
    from .store import db, migrate

    con = db.connect(_cfg().platform_db)
    migrate.migrate(con, db.MIGRATIONS)
    return con


# ----------------------------------------------------------------- telegram setup
TOKEN_RE = r"\d{6,12}:[A-Za-z0-9_-]{30,40}"


@app.command("telegram-token")
def telegram_token() -> None:
    """Store the @BotFather token in the Keychain -- only after Telegram confirms it.

    A bare `keyring set` accepts anything, and a wrong paste then surfaces much later as
    a bot you "cannot find". Validate first, store second.
    """
    import getpass
    import re

    from . import secrets as sec
    from .delivery.telegram import TelegramSink

    token = getpass.getpass("Bot token from @BotFather (input hidden): ").strip()
    if not re.fullmatch(TOKEN_RE, token):
        typer.echo(
            f"That is not a bot token (got {len(token)} chars). A token looks like "
            "123456789:AAH... (~46 chars, one colon). In @BotFather send /mybots, pick "
            "your bot, then 'API Token'. Nothing was stored.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        me = TelegramSink(token, "0").me()
    except Exception as e:
        typer.echo(f"Telegram rejected the token: {e}. Nothing was stored.", err=True)
        raise typer.Exit(1) from e
    username = me.get("username")
    if not username:
        typer.echo("Telegram did not confirm the token. Nothing was stored.", err=True)
        raise typer.Exit(1)
    sec.put("telegram_token", token)
    typer.echo(f"stored token for @{username}")
    typer.echo(f"Open https://t.me/{username} , press Start, then: just telegram-bind")


@app.command("telegram-bind")
def telegram_bind() -> None:
    """Read your chat id from a message you sent the bot, and PIN it to an allow-list.

    Without this pin, anyone who finds your bot can talk to your agents.
    """
    from . import secrets as sec
    from .delivery.telegram import TelegramSink, discover_chat_id
    from .store.db import kv_put

    token = sec.get("telegram_token")
    me = TelegramSink(token, "0").me()
    typer.echo(f"bot: @{me.get('username')}")
    typer.echo(
        "note: if pa-bot is running it consumes getUpdates first. Stop it while binding:\n"
        "  launchctl bootout gui/$(id -u)/com.pa.bot   (then: just install-brief)"
    )
    chat = discover_chat_id(token)
    if not chat:
        typer.echo("No messages found. Send your bot any message, then re-run.", err=True)
        raise typer.Exit(1)
    kv_put(_con(), "telegram:chat_id", chat)
    typer.echo(f"bound chat_id {chat} (pinned as the only accepted inbound chat)")


@app.command("telegram-test")
def telegram_test() -> None:
    """Send a message deliberately full of MarkdownV2 landmines."""
    from . import secrets as sec
    from .delivery.telegram import TelegramSink
    from .store.db import kv_get

    chat = kv_get(_con(), "telegram:chat_id")
    if not chat:
        typer.echo("not bound. Run: pa-core-admin telegram-bind", err=True)
        raise typer.Exit(1)
    body = (
        "## Section 1 · shape\n"
        "**Load:** 4h15m booked / 3 external (2 internal-only) - protect tonight.\n"
        "· Mai Tran <mai@client.com> asked twice & is blocked. Cost: $0.09.\n"
        "· see [the sheet](https://example.com/q4?a=1&b=2)\n"
        "`source.release(version='0.9.2')` - code, punctuation, ellipsis…\n"
        "---\n"
        "Every one of those chars would need escaping in MarkdownV2. In HTML mode: none.\n"
        + ("filler to force a second chunk. " * 180)
    )
    d = TelegramSink(sec.get("telegram_token"), chat).deliver(
        title="Good Morning Bobby self-test", body_markdown=body, run_id="selftest"
    )
    typer.echo(f"ok={d.ok} id={d.external_id} err={d.error}")
    raise typer.Exit(0 if d.ok else 1)


# ----------------------------------------------------------------- llm
@app.command("smoke-llm")
def smoke_llm() -> None:
    """One structured-output call through the real router, validated by pydantic."""
    from pydantic import BaseModel

    from .harness.run import build_llm
    from .llm.base import Task
    from .llm.usage import UsageLog

    class Out(BaseModel):
        model_config = {"extra": "forbid"}
        keywords: list[str]

    cfg = _cfg()
    llm = build_llm(cfg, UsageLog(_con(), cfg.monthly_budget_usd))
    res = llm.complete(
        task=Task.EXTRACT,
        system="Extract software search keywords. JSON only, with a keywords list.",
        user="Find a Python HTTP client library.",
        agent="smoke",
        schema=Out,
        max_tokens=200,
    )
    typer.echo(f"backend={res.backend} model={res.model} {res.latency_ms}ms -> {res.parsed}")


@app.command()
def bench() -> None:
    """Measure real prefill and generation throughput on THIS machine."""
    from .llm.ollama import OllamaBackend

    cfg = _cfg()
    be = OllamaBackend(cfg.ollama_url, num_ctx=cfg.num_ctx)
    for model in dict.fromkeys([cfg.model_small, cfg.model_large]):
        be.complete(
            system="ok",
            user="hi",
            model=model,
            max_tokens=5,
            temperature=0.0,
            schema=None,
            cacheable_prefix=None,
            timeout_s=120,
        )
        for ptok in (500, 3000):
            import time as _t

            filler = "The quick brown fox jumps over the lazy dog. " * (ptok // 9 + 1)
            t0 = _t.monotonic()
            r = be.complete(
                system="Summarize in one sentence.",
                user=filler[: ptok * 5],
                model=model,
                max_tokens=120,
                temperature=0.0,
                schema=None,
                cacheable_prefix=None,
                timeout_s=300,
            )
            wall = _t.monotonic() - t0
            typer.echo(
                f"{model:32s} in={r.usage.input_tokens:5d} out={r.usage.output_tokens:4d} "
                f"wall={wall:6.1f}s  prefill≈{r.usage.input_tokens / max(wall, 0.01):6.0f} tok/s"
            )


# ----------------------------------------------------------------- health
@app.command()
def doctor() -> None:
    """Everything that silently breaks, checked in one place."""
    from . import secrets as sec
    from .harness.alerting import preflight
    from .llm.ollama import OllamaBackend
    from .store.db import kv_get

    cfg, con = _cfg(), _con()
    bad = 0

    def line(ok: bool, label: str, detail: str = "") -> None:
        nonlocal bad
        typer.echo(("  ok   " if ok else "  FAIL ") + label + (f" - {detail}" if detail else ""))
        if not ok:
            bad += 1

    typer.echo("config")
    line(True, "data_dir", str(cfg.data_dir))
    line(True, "timezone", cfg.tz)
    line(True, "num_ctx", str(cfg.num_ctx))
    line(True, "keychain backend", sec.backend_name())

    typer.echo("ollama")
    ok, msg = OllamaBackend(cfg.ollama_url, num_ctx=cfg.num_ctx).health()
    line(ok, "reachable", msg)
    for p in preflight(cfg):
        line(False, "preflight", p)

    typer.echo("telegram")
    tok = sec.get_optional("telegram_token")
    line(bool(tok), "token in Keychain", "" if tok else "run: just telegram-token")
    chat = kv_get(con, "telegram:chat_id")
    line(bool(chat), "chat bound", chat or "run: pa-core-admin telegram-bind")

    typer.echo("runs")
    for r in con.execute(
        "select agent, logical_date, status, usd, duration_ms from agent_run "
        "order by started_at desc limit 5"
    ):
        typer.echo(
            f"  {r['agent']:16s} {r['logical_date']} {r['status']:8s} "
            f"${r['usd']:.4f} {r['duration_ms'] or 0}ms"
        )
    mtd = con.execute(
        "select coalesce(sum(cost_usd),0) from llm_call where created_at >= strftime('%Y-%m-01','now')"
    ).fetchone()[0]
    typer.echo(f"  month-to-date LLM spend: ${mtd:.4f}")

    raise typer.Exit(1 if bad else 0)


@app.command()
def watchdog() -> None:
    """The dead-man's switch. Run by launchd at 09:30 and 13:00."""
    from .harness.run import run_watchdog

    raise typer.Exit(run_watchdog())


@app.command("runs")
def runs(limit: int = 20) -> None:
    """Recent runs with cost and duration."""
    for r in _con().execute(
        "select agent, logical_date, status, usd, duration_ms, error_type, error_msg "
        "from agent_run order by started_at desc limit ?",
        (limit,),
    ):
        err = f"  {r['error_type']}: {(r['error_msg'] or '')[:60]}" if r["error_type"] else ""
        typer.echo(
            f"{r['agent']:16s} {r['logical_date']} {r['status']:8s} "
            f"${r['usd']:.4f} {r['duration_ms'] or 0:6d}ms{err}"
        )


@app.command("cost")
def cost() -> None:
    """Which agent is actually expensive -- one SQL query, as designed."""
    rows = (
        _con()
        .execute(
            """select agent, task, backend, model, count(*) n,
                  sum(input_tokens) tin, sum(output_tokens) tout,
                  sum(cost_usd) usd, avg(latency_ms) ms, sum(ok=0) errs
             from llm_call where created_at >= strftime('%Y-%m-01','now')
            group by agent, task, model order by usd desc, n desc"""
        )
        .fetchall()
    )
    if not rows:
        typer.echo("no LLM calls this month")
        return
    typer.echo(
        f"{'agent':16s} {'task':11s} {'model':30s} {'n':>4s} {'in':>8s} {'out':>7s} "
        f"{'usd':>8s} {'ms':>7s} {'err':>4s}"
    )
    for r in rows:
        typer.echo(
            f"{r['agent'][:16]:16s} {r['task'][:11]:11s} {r['model'][:30]:30s} "
            f"{r['n']:4d} {r['tin']:8d} {r['tout']:7d} {r['usd']:8.4f} "
            f"{r['ms']:7.0f} {r['errs']:4d}"
        )
