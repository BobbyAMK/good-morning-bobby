"""Mutual exclusion and run-timeout.

launchd replays a missed StartCalendarInterval job on wake, and we deliberately
schedule the brief at three times a day (§ scheduling). So two processes racing for
the same agent is a NORMAL event, not an exception -- it must be cheap and quiet.
"""

from __future__ import annotations

import fcntl
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType

from ..errors import AlreadyRunning, RunTimeout


@contextmanager
def single_instance(lock_dir: Path, agent: str) -> Iterator[None]:
    """Non-blocking exclusive flock. Already held -> AlreadyRunning (caller exits 0).

    The file handle is held open for the lifetime of the context: flock is tied to
    the open descriptor, so closing it early silently releases the lock.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{agent}.lock"
    f = path.open("w")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise AlreadyRunning(f"{agent} is already running") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


@contextmanager
def time_limit(seconds: int) -> Iterator[None]:
    """Wall-clock alarm, so a wedged Ollama generation cannot hold the lock until noon.

    Uses SIGALRM, which only fires on the main thread -- correct for a CLI entrypoint
    and the reason this is not used inside worker threads.
    """
    if seconds <= 0:
        yield
        return

    def _raise(signum: int, frame: FrameType | None) -> None:
        raise RunTimeout(f"run exceeded {seconds}s")

    prev = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)
