"""Small, dependency-free helper for avoiding ingestion of partial file writes."""

from __future__ import annotations

import os
import time


def wait_for_stable_file(
    filepath: str,
    interval_seconds: float = 0.5,
    stable_checks: int = 3,
    timeout_seconds: float = 60.0,
) -> bool:
    """Wait until a nonempty file's size and mtime remain stable repeatedly."""
    if stable_checks < 1 or interval_seconds < 0 or timeout_seconds < 0:
        raise ValueError("stability checks must be positive and time values nonnegative")
    deadline = time.monotonic() + timeout_seconds
    previous = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            stat = os.stat(filepath)
            current = (stat.st_size, stat.st_mtime_ns)
            if stat.st_size > 0 and current == previous:
                stable += 1
                if stable >= stable_checks:
                    return True
            else:
                stable = 0
            previous = current
        except OSError:
            if not os.path.exists(filepath):
                return False
            previous = None
            stable = 0
        time.sleep(interval_seconds)
    return False
