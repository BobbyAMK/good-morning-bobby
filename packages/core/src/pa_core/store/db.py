"""SQLite connections with the pragmas that matter.

Two databases, not one and not ten:
  platform.db -- small, precious, irreplaceable. Back it up.
  brain.db    -- large, fully rebuildable from source files. Excluded from backups,
                 so a re-embed or VACUUM never holds a write lock the 6am brief wants.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

MIGRATIONS = Path(__file__).parent / "migrations"


def connect(path: Path, *, vectors: bool = False, timeout: float = 10.0) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None -> autocommit; we open transactions explicitly where we
    # want them, which keeps executescript()'s implicit COMMIT from surprising us.
    con = sqlite3.connect(path, isolation_level=None, timeout=timeout)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode = WAL")  # concurrent readers during a long run
    con.execute("pragma synchronous = NORMAL")  # WAL makes this safe enough
    con.execute("pragma foreign_keys = ON")  # OFF by default in sqlite3 -- always set it
    con.execute("pragma busy_timeout = 10000")
    if vectors:
        import sqlite_vec

        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)  # re-close the door
    return con


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit immediate transaction. Use for multi-statement writes."""
    con.execute("begin immediate")
    try:
        yield con
    except BaseException:
        con.execute("rollback")
        raise
    else:
        con.execute("commit")


def kv_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("select v from kv where k = ?", (key,)).fetchone()
    return row["v"] if row else None


def kv_put(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "insert into kv (k, v, updated_at) values (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        "on conflict(k) do update set v = excluded.v, updated_at = excluded.updated_at",
        (key, value),
    )


def backup(con: sqlite3.Connection, dest: Path) -> None:
    """VACUUM INTO -- a consistent single-file snapshot, safe while WAL is active."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    con.execute("vacuum into ?", (str(dest),))
