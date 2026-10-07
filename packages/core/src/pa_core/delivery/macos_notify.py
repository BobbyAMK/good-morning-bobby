"""macOS notification via osascript. No dependency, no terminal-notifier install."""

from __future__ import annotations

import shutil
import subprocess

from ..text import plain_dashes
from .base import Delivered


def _esc(s: str) -> str:
    return plain_dashes(s).replace("\\", "\\\\").replace('"', '\\"')


class MacNotifySink:
    name = "notify"

    def __init__(self, *, app_title: str = "Good Morning Bobby") -> None:
        self._title = app_title

    def deliver(
        self, *, title: str, body_markdown: str, run_id: str, urgent: bool = False
    ) -> Delivered:
        osa = shutil.which("osascript")
        if not osa:
            return Delivered(self.name, None, False, "osascript not found")
        first = next((ln for ln in body_markdown.splitlines() if ln.strip()), title)
        sound = "Basso" if urgent else "Ping"
        script = (
            f'display notification "{_esc(first[:180])}" '
            f'with title "{_esc(self._title)}" subtitle "{_esc(title[:80])}" '
            f'sound name "{sound}"'
        )
        try:
            subprocess.run([osa, "-e", script], check=True, capture_output=True, timeout=10)
            return Delivered(self.name, None, True)
        except (subprocess.SubprocessError, OSError) as e:
            return Delivered(self.name, None, False, f"{type(e).__name__}: {e}")
