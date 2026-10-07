"""Watchdog must judge delivery by the channel you read, not by "any sink"."""

from datetime import datetime

import pytest
from pa_core.config import Settings
from pa_core.harness import alerting
from pa_core.store import db, migrate


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(alerting, "preflight", lambda cfg: [])  # no Ollama/disk probing
    cfg = Settings(_env_file=None, data_dir=tmp_path, expected_daily={"morning-brief": "09:00"})
    con = db.connect(cfg.platform_db)
    migrate.migrate(con, db.MIGRATIONS)
    now = datetime(2026, 9, 14, 9, 30, tzinfo=cfg.zone)
    return cfg, con, now


def _ok_run(con, deliveries):
    con.execute(
        """insert into agent_run (run_id, agent, logical_date, dry_run, status, started_at)
           values ('r1', 'morning-brief', '2026-09-14', 0, 'ok', '2026-09-13T23:05:00Z')"""
    )
    for sink, ok in deliveries:
        con.execute(
            """insert into delivery (run_id, sink, delivered_at, ok)
               values ('r1', ?, '2026-09-13T23:06:00Z', ?)""",
            (sink, ok),
        )


def _undelivered(problems):
    return [p for p in problems if "NOT delivered" in p]


def test_file_ok_but_telegram_failed_alerts(env):
    cfg, con, now = env
    _ok_run(con, [("file", 1), ("telegram", 0)])
    assert _undelivered(alerting.watchdog_check(con, cfg, now))


def test_telegram_unconfigured_alerts(env):
    # an unbound chat means no telegram row at all -- the file row must not mask it
    cfg, con, now = env
    _ok_run(con, [("file", 1)])
    assert _undelivered(alerting.watchdog_check(con, cfg, now))


def test_telegram_delivered_is_quiet(env):
    cfg, con, now = env
    _ok_run(con, [("file", 1), ("telegram", 1)])
    assert alerting.watchdog_check(con, cfg, now) == []
