"""Second Brain — local-first, evidence-grounded memory core for AI hosts."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("second-brain")
except PackageNotFoundError:
    __version__ = "0.2.0"

__all__ = ["__version__"]
