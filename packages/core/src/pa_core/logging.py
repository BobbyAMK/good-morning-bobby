"""structlog -> JSONL on stdout, which launchd redirects to a file.

JSONL because the only tools you will actually have at 6am are `grep` and `jq`.
Every line carries run_id and agent, bound once in the harness.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def configure(*, level: str = "INFO", pretty: bool | None = None) -> None:
    """pretty=None -> auto: human-readable on a TTY, JSONL under launchd."""
    if pretty is None:
        pretty = sys.stderr.isatty()

    renderer: Any = (
        structlog.dev.ConsoleRenderer(colors=True)
        if pretty
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get(name: str = "pa") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
