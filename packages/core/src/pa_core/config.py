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
    personas_dir: Path = Path("personas")

    tz: str = "Asia/Ho_Chi_Minh"

    # --- LLM: the zero-cost path is the DEFAULT, not the fallback.
    ollama_url: str = "http://127.0.0.1:11434"
    model_small: str = "qwen2.5:7b-instruct-q4_K_M"
    model_large: str = "qwen2.5:7b-instruct-q4_K_M"
    model_embed: str = "bge-m3"

    # The nominal 256K context is a lie at 16 GB -- the KV cache is the binding
    # constraint, not the weights. This ceiling is WHY the morning brief is map-reduce.
    num_ctx: int = 8192

    # Master switch for ANY non-local backend. Off until you turn it on.
    allow_cloud: bool = False
    monthly_budget_usd: float | None = 5.0

    llm_timeout_s: float = 600.0
    llm_retries: int = 2

    # --- delivery
    sinks: list[str] = Field(default_factory=lambda: ["file", "telegram", "notify"])
    telegram_chat_id: str | None = None  # normally read from the kv table, not here

    # --- watchdog: agent -> local clock time by which a run for today must exist
    expected_daily: dict[str, str] = Field(default_factory=lambda: {"morning-brief": "09:00"})

    # --- agent behaviour
    internal_domains: list[str] = Field(default_factory=list)
    my_email: str | None = None

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def platform_db(self) -> Path:
        return self.data_dir / "platform.db"

    @property
    def brain_db(self) -> Path:
        return self.data_dir / "brain.db"

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
