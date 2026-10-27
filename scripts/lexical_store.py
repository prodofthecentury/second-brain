# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Rebuildable, fail-closed SQLite FTS5 sidecar for canonical memory atoms.

This database is derived data only. It holds no review decision or evidence that
cannot be reconstructed from the canonical Markdown atoms. Query input is
reduced to literal word tokens and passed through SQLite parameter binding.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from index_manifest import atom_revisions, revisions_fingerprint


LEXICAL_SCHEMA_VERSION = 1
FTS_TABLE = "atoms_fts"
META_TABLE = "lexical_meta"
MAX_QUERY_TERMS = 24
MAX_QUERY_RESULTS = 100
TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


class LexicalIndexUnavailable(RuntimeError):
    """Raised when lexical recall would serve an absent, malformed, or stale sidecar."""


@dataclass(frozen=True)
class LexicalCandidate:
    filename: str
    content: str
    content_sha256: str
    rank: float


def _index_metadata(atoms_dir: str, indexed_count: int | None = None) -> dict[str, str | int]:
    revisions = atom_revisions(atoms_dir)
    return {
        "schema_version": LEXICAL_SCHEMA_VERSION,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "atom_count": len(revisions),
        "indexed_count": len(revisions) if indexed_count is None else indexed_count,
        "atom_revisions_sha256": revisions_fingerprint(revisions),
    }


def _read_metadata(connection: sqlite3.Connection) -> dict[str, object] | None:
    try:
        rows = connection.execute(f"SELECT key, value FROM {META_TABLE}").fetchall()
    except sqlite3.Error:
        return None
    metadata: dict[str, object] = {}
    for key, value in rows:
        try:
            metadata[str(key)] = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
    return metadata


def _readonly_connection(index_path: str) -> sqlite3.Connection:
    path = Path(index_path).resolve()
    if not path.is_file():
        raise LexicalIndexUnavailable("Lexical recall index is unavailable; rebuild it first.")
    try:
        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LexicalIndexUnavailable(f"Lexical recall index cannot be opened: {exc}") from exc


def lexical_index_is_current(atoms_dir: str, index_path: str) -> bool:
    """Return true only when the sidecar is a complete projection of current atom bytes."""
    try:
        connection = _readonly_connection(index_path)
    except LexicalIndexUnavailable:
        return False
    try:
        metadata = _read_metadata(connection)
    finally:
        connection.close()
    if not isinstance(metadata, dict) or metadata.get("schema_version") != LEXICAL_SCHEMA_VERSION:
        return False
    current = _index_metadata(atoms_dir)
    return (
        metadata.get("atom_count") == current["atom_count"]
        and metadata.get("indexed_count") == current["atom_count"]
        and metadata.get("atom_revisions_sha256") == current["atom_revisions_sha256"]
    )


def _fts_query(query: str) -> str:
    """Turn arbitrary user text into a small, literal-only FTS5 OR query."""
    seen = set()
    terms = []
    for token in TOKEN_PATTERN.findall(str(query or "")):
        normalized = token.casefold()
        if normalized and normalized not in seen:
            seen.add(normalized)
            terms.append(normalized)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    return " OR ".join(f'"{term}"' for term in terms)


def rebuild_lexical_index(atoms_dir: str, index_path: str) -> dict[str, object]:
    """Build a complete staging FTS5 database, then atomically activate it."""
    revisions = atom_revisions(atoms_dir)
    destination = Path(index_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, staging_path = tempfile.mkstemp(
        prefix=f".{destination.stem}.rebuild-",
        suffix=".sqlite3",
        dir=destination.parent,
    )
    os.close(descriptor)

    try:
        connection = sqlite3.connect(staging_path)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(f"CREATE TABLE {META_TABLE}(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute(
                f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
                "filename UNINDEXED, content, content_sha256 UNINDEXED, tokenize='unicode61')"
            )
            for revision in revisions:
                filename = revision["filename"]
                atom_path = os.path.join(atoms_dir, filename)
                with open(atom_path, "rb") as file:
                    content_bytes = file.read()
                content = content_bytes.decode("utf-8")
                content_hash = hashlib.sha256(content_bytes).hexdigest()
                if content_hash != revision["content_sha256"]:
                    raise RuntimeError(f"Atom changed during lexical rebuild: {filename}")
                connection.execute(
                    f"INSERT INTO {FTS_TABLE}(filename, content, content_sha256) VALUES (?, ?, ?)",
                    (filename, content, content_hash),
                )

            metadata = _index_metadata(atoms_dir, indexed_count=len(revisions))
            if metadata["atom_revisions_sha256"] != revisions_fingerprint(revisions):
                raise RuntimeError("Canonical atoms changed during lexical rebuild; active sidecar was preserved.")
            connection.executemany(
                f"INSERT INTO {META_TABLE}(key, value) VALUES (?, ?)",
                [(key, json.dumps(value, ensure_ascii=False)) for key, value in metadata.items()],
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(staging_path, destination)
    except Exception:
        if os.path.exists(staging_path):
            os.remove(staging_path)
        raise

    return {
        "indexed_count": len(revisions),
        "atom_count": len(revisions),
        "index_path": str(destination),
        "atom_revisions_sha256": revisions_fingerprint(revisions),
    }


def search_lexical_index(
    query: str,
    index_path: str,
    n_results: int = 5,
    atoms_dir: str | None = None,
) -> list[LexicalCandidate]:
    """Return FTS5 candidates only when the sidecar is current and query syntax is safe."""
    if atoms_dir is not None and not lexical_index_is_current(atoms_dir, index_path):
        raise LexicalIndexUnavailable("Lexical recall index is stale; rebuild it before serving memory.")
    expression = _fts_query(query)
    if not expression:
        return []
    try:
        limit = max(1, min(int(n_results), MAX_QUERY_RESULTS))
    except (TypeError, ValueError):
        limit = 5

    connection = _readonly_connection(index_path)
    try:
        rows = connection.execute(
            f"SELECT filename, content, content_sha256, rank "
            f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (expression, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise LexicalIndexUnavailable(f"Lexical recall query failed: {exc}") from exc
    finally:
        connection.close()

    return [
        LexicalCandidate(
            filename=str(filename),
            content=str(content),
            content_sha256=str(content_sha256),
            rank=float(rank),
        )
        for filename, content, content_sha256, rank in rows
    ]
