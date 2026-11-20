# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Pure helpers for deciding which files the Refinery may process."""

import os
import re

from config import TEXT_EXTS


def intake_status(content: str) -> str:
    """Return a conservative intake lifecycle state; unmarked pointers stay quarantined."""
    match = re.search(r"^intake_status:\s*([a-z_]+)\s*$", content or "", re.MULTILINE | re.IGNORECASE)
    if not match:
        return "quarantined"
    status = match.group(1).lower()
    return status if status in {"quarantined", "approved", "rejected"} else "quarantined"


def is_supported_refinery_target(filepath: str) -> bool:
    filename = os.path.basename(str(filepath or ""))
    ext = os.path.splitext(filename)[1].lower()
    return filename != "reading-list.md" and (ext in TEXT_EXTS or ext == ".intake")
