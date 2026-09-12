"""Schema migrations: PRAGMA user_version + ordered .sql files. Zero dependencies.

Not Alembic: that is an SQLAlchemy-shaped tool and there is no ORM here. SQLite's
user_version is purpose-built for exactly this.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ..errors import MigrationError

_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def pending(con: sqlite3.Connection, migrations_dir: Path) -> list[Path]:
    have = con.execute("pragma user_version").fetchone()[0]
    out = []
    for f in sorted(migrations_dir.iterdir()):
        m = _NAME.match(f.name)
        if m and int(m.group(1)) > have:
            out.append(f)
    return out


def migrate(con: sqlite3.Connection, migrations_dir: Path) -> list[str]:
    """Apply every migration newer than user_version, in filename order.

    Each file MUST end with `pragma user_version = <N>;` matching its numeric prefix.
    executescript() commits implicitly, so that trailing pragma is the only way to
    make the version bump part of the same unit -- and the post-condition check below
    is what turns a half-applied file into a loud failure instead of a silent one.
    """
    applied: list[str] = []
    for f in pending(con, migrations_dir):
        want = int(_NAME.match(f.name).group(1))  # type: ignore[union-attr]
        con.executescript(f.read_text(encoding="utf-8"))
        got = con.execute("pragma user_version").fetchone()[0]
        if got != want:
            raise MigrationError(
                f"{f.name} did not set `pragma user_version = {want}` as its last "
                f"statement (user_version is now {got}). The migration may be "
                f"half-applied -- inspect the database before retrying."
            )
        applied.append(f.name)
    return applied
