"""Voice / style card storage -- READ PATH ONLY for now.

Project 10 automates producing this from writing samples. Until then you hand-write
`personas/default.md` in twenty minutes, and projects 1/2/3/9 all consume it through
the same function -- so there is one voice, not five copy-pasted prompt fragments.

The card is PREPENDED to the system prompt by LLM.complete(persona=...), never
interpolated into the user message, so a cloud backend can place a prompt-cache
breakpoint after a stable prefix by construction.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from ..config import Settings

DEFAULT_SLUG = "default"


def card_path(cfg: Settings, slug: str = DEFAULT_SLUG) -> Path:
    d = cfg.personas_dir
    if not d.is_absolute():
        d = Path.cwd() / d
    local = d / f"{slug}.local.md"  # gitignored: may contain private samples
    return local if local.exists() else d / f"{slug}.md"


_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def load_card(cfg: Settings, slug: str = DEFAULT_SLUG) -> str | None:
    """HTML comments are stripped: the card doubles as a file you edit by hand, and
    its editing notes must not end up inside a prompt."""
    p = card_path(cfg, slug)
    if not p.exists():
        return None
    text = _COMMENT.sub("", p.read_text(encoding="utf-8")).strip()
    return text or None


def load_active_from_db(con: sqlite3.Connection, slug: str = DEFAULT_SLUG) -> str | None:
    row = con.execute(
        "select style_card_md from persona_profile where slug=? and is_active=1 "
        "order by version desc limit 1",
        (slug,),
    ).fetchone()
    return row["style_card_md"] if row else None


def save_version(
    con: sqlite3.Connection,
    *,
    slug: str,
    profile: dict,
    style_card_md: str,
    source_hash: str,
    activate: bool = True,
) -> int:
    row = con.execute(
        "select coalesce(max(version),0)+1 as v from persona_profile where slug=?", (slug,)
    ).fetchone()
    version = int(row["v"])
    con.execute(
        """insert into persona_profile
           (slug, version, created_at, source_hash, profile_json, style_card_md, is_active)
           values (?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?,?,?)""",
        (
            slug,
            version,
            source_hash,
            json.dumps(profile, ensure_ascii=False),
            style_card_md,
            int(activate),
        ),
    )
    if activate:
        con.execute(
            "update persona_profile set is_active=0 where slug=? and version<>?", (slug, version)
        )
    return version
