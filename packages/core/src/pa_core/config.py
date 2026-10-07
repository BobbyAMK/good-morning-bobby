"""Typed configuration.

A launchd job gets a minimal environment: no shell profile, no PATH, none of your
exports. So launchd passes an ABSOLUTE path to the env file via PA_ENV_FILE, and
everything else has a working default.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PA_",
        env_file=os.environ.get("PA_ENV_FILE", ".env"),
        env_nested_delimiter="__",
        extra="ignore",
    )

    # --- paths: data lives OUTSIDE the repo, under ~/Library/Application Support.
    # This is not taste. A launchd job reading ~/Documents, ~/Desktop or ~/Downloads
    # is silently denied by macOS TCC until you grant Full Disk Access to the exact
    # binary, and the denial surfaces as a mysterious PermissionError.
    data_dir: Path = Path.home() / "Library/Application Support/pet-agents"
    log_dir: Path = Path.home() / "Library/Logs/pet-agents"
    out_dir: Path | None = None  # defaults to data_dir/out

    tz: str = "Asia/Ho_Chi_Minh"

    # --- LLM: the zero-cost path is the DEFAULT, not the fallback.
    ollama_url: str = "http://127.0.0.1:11434"
    model_small: str = "qwen2.5:7b-instruct-q4_K_M"
    model_large: str = "qwen2.5:7b-instruct-q4_K_M"

    # Bound context memory. Summaries are processed in small batches.
    num_ctx: int = 8192

    # Optional ledger budget; local inference has no API cost.
    monthly_budget_usd: float | None = 5.0

    llm_timeout_s: float = 600.0
    llm_retries: int = 2

    # --- delivery
    # Telegram is the ONLY channel you read. "file" is a local archive that always runs
    # first so output is never lost; it is not a delivery. "notify" (macOS banner) is
    # still available, but failure alerts use it regardless of this list.
    sinks: list[str] = Field(default_factory=lambda: ["file", "telegram"])
    # The sink that counts as "delivered". The watchdog alerts if today's run did not
    # reach it -- a successful file write must never mask a failed Telegram send.
    primary_sink: str = "telegram"
    telegram_chat_id: str | None = None  # normally read from the kv table, not here

    # --- watchdog: agent -> local clock time by which a run for today must exist
    expected_daily: dict[str, str] = Field(default_factory=lambda: {"morning-brief": "09:00"})

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def platform_db(self) -> Path:
        return self.data_dir / "platform.db"

    @property
    def lock_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def outputs(self) -> Path:
        return self.out_dir or (self.data_dir / "out")

    def now(self) -> datetime:
        """Always tz-aware. Naive datetimes are how DST bugs get in."""
        return datetime.now(self.zone)

    def today(self) -> date:
        """The LOCAL date. This is what `logical_date` is derived from."""
        return self.now().date()

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.log_dir, self.lock_dir, self.outputs):
            p.mkdir(parents=True, exist_ok=True)


_cached: Settings | None = None


def load(reload: bool = False) -> Settings:
    global _cached
    if _cached is None or reload:
        _cached = Settings()
    return _cached
