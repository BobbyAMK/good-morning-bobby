"""The canonical artifact sink.

This runs FIRST, always, before Telegram. If every network sink fails, the output
still exists on disk -- so a delivery failure is never a lost brief.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..text import plain_dashes
from .base import Delivered

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(s: str) -> str:
    return _SLUG.sub("-", s.lower()).strip("-")[:60] or "untitled"


class MarkdownFileSink:
    name = "file"

    def __init__(self, out_dir: Path, *, agent: str, now: datetime) -> None:
        self._dir = out_dir / agent / f"{now:%Y}" / f"{now:%m}"
        self._now = now

    def deliver(
        self, *, title: str, body_markdown: str, run_id: str, urgent: bool = False
    ) -> Delivered:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"{self._now:%Y-%m-%d}-{slugify(title)}.md"
            front = (
                "---\n"
                f"title: {title}\n"
                f"generated_at: {self._now.isoformat()}\n"
                f"run_id: {run_id}\n"
                "---\n\n"
            )
            path.write_text(plain_dashes(front + body_markdown), encoding="utf-8")
            return Delivered(self.name, str(path), True)
        except OSError as e:
            return Delivered(self.name, None, False, f"{type(e).__name__}: {e}")
