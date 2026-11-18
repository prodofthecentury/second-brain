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
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import _is_reparse_or_link, safe_file_in_dir
from index_manifest import atom_revisions, record_known_atom_hash, revisions_fingerprint


LEXICAL_SCHEMA_VERSION = 1
FTS_TABLE = "atoms_fts"
META_TABLE = "lexical_meta"
MAX_QUERY_TERMS = 24
MAX_QUERY_RESULTS = 500
TOKEN_PATTERN = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)

FTS_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by",
        "can", "could", "did", "do", "does", "for", "from", "had", "has", "have",
        "having", "how", "i", "in", "into", "is", "it", "its", "me", "my", "of",
        "on", "or", "our", "owner", "owners", "owner's", "rather", "should", "so",
        "some", "such", "than", "that", "the", "their", "them", "then", "there",
        "these", "they", "this", "those", "through", "to", "too", "under", "very",
        "was", "we", "were", "what", "when", "where", "which", "while", "who",
        "why", "will", "with", "would", "you", "your", "please", "output",
        "according", "approved", "factual", "claim", "claims", "confirms",
        "confirm", "purely",
    }
)


def tokenize(text: str) -> list[str]:
    """Extract normalized lowercase lexical tokens from text."""
    return [tok.casefold() for tok in TOKEN_PATTERN.findall(str(text or ""))]


def _porter_is_consonant(w: str, i: int) -> bool:
    ch = w[i]
    if ch in "aeiou":
        return False
    if ch == "y":
        k = i
        while k > 0 and w[k - 1] == "y":
            k -= 1
        base_is_cons = True if k == 0 else (w[k - 1] not in "aeiou")
        return base_is_cons if ((i - k) % 2 == 0) else (not base_is_cons)
    return True


def _porter_measure(w: str) -> int:
    m = 0
    in_vowel = False
    for idx in range(len(w)):
        if not _porter_is_consonant(w, idx):
            in_vowel = True
        elif in_vowel:
            m += 1
            in_vowel = False
    return m


def _porter_contains_vowel(w: str) -> bool:
    return any(not _porter_is_consonant(w, i) for i in range(len(w)))


def _porter_double_consonant(w: str) -> bool:
    return len(w) >= 2 and w[-1] == w[-2] and _porter_is_consonant(w, len(w) - 1)


def _porter_cvc(w: str) -> bool:
    return (
        len(w) >= 3
        and _porter_is_consonant(w, len(w) - 3)
        and not _porter_is_consonant(w, len(w) - 2)
        and _porter_is_consonant(w, len(w) - 1)
        and w[-1] not in "wxy"
    )


_PORTER_STEP2: tuple[tuple[str, str], ...] = (
    ("ational", "ate"),
    ("tional", "tion"),
    ("enci", "ence"),
    ("anci", "ance"),
    ("izer", "ize"),
    ("abli", "able"),
    ("bli", "ble"),
    ("alli", "al"),
    ("entli", "ent"),
    ("eli", "e"),
    ("ousli", "ous"),
    ("ization", "ize"),
    ("ation", "ate"),
    ("ator", "ate"),
    ("alism", "al"),
    ("iveness", "ive"),
    ("fulness", "ful"),
    ("ousness", "ous"),
    ("aliti", "al"),
    ("iviti", "ive"),
    ("biliti", "ble"),
    ("logi", "log"),
)

_PORTER_STEP3: tuple[tuple[str, str], ...] = (
    ("icate", "ic"),
    ("ative", ""),
    ("alize", "al"),
    ("iciti", "ic"),
    ("ical", "ic"),
    ("ful", ""),
    ("ness", ""),
)

_PORTER_STEP4: tuple[str, ...] = (
    "ement", "ance", "ence", "able", "ible", "ment",
    "ant", "ent", "ism", "ate", "iti", "ous", "ive", "ize", "ion",
    "al", "er", "ic", "ou",
)


@functools.lru_cache(maxsize=16384)
def porter_stem(word: str) -> str:
    """Pure-Python Martin Porter (1980) English stemmer covering Steps 1a-5b."""
    w = str(word or "").strip().casefold()
    if len(w) <= 2:
        return w
    if "-" in w:
        parts = w.split("-")
        if all(p for p in parts):
            return "-".join(porter_stem(p) for p in parts)
        return w
    if not (w.isascii() and w.isalpha()):
        return w

    # Step 1a
    if w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("ies"):
        w = w[:-2]
    elif not w.endswith("ss") and w.endswith("s"):
        w = w[:-1]

    # Step 1b
    if w.endswith("eed"):
        stem = w[:-3]
        if _porter_measure(stem) > 0:
            w = stem + "ee"
    else:
        trimmed = False
        if w.endswith("ed") and _porter_contains_vowel(w[:-2]):
            w = w[:-2]
            trimmed = True
        elif w.endswith("ing") and _porter_contains_vowel(w[:-3]):
            w = w[:-3]
            trimmed = True
        if trimmed:
            if w.endswith(("at", "bl", "iz")):
                w = w + "e"
            elif _porter_double_consonant(w) and w[-1] not in "lsz":
                w = w[:-1]
            elif _porter_measure(w) == 1 and _porter_cvc(w):
                w = w + "e"

    # Step 1c
    if w.endswith("y") and _porter_contains_vowel(w[:-1]):
        w = w[:-1] + "i"

    # Step 2 (longest matching suffix)
    for suffix, repl in sorted(_PORTER_STEP2, key=lambda item: len(item[0]), reverse=True):
        if w.endswith(suffix):
            stem = w[: -len(suffix)]
            if _porter_measure(stem) > 0:
                w = stem + repl
            break

    # Step 3 (longest matching suffix)
    for suffix, repl in sorted(_PORTER_STEP3, key=lambda item: len(item[0]), reverse=True):
        if w.endswith(suffix):
            stem = w[: -len(suffix)]
            if _porter_measure(stem) > 0:
                w = stem + repl
            break

    # Step 4 (longest matching suffix)
    for suffix in _PORTER_STEP4:
        if w.endswith(suffix):
            stem = w[: -len(suffix)]
            if suffix == "ion":
                if stem.endswith(("s", "t")) and _porter_measure(stem) > 1:
                    w = stem
            elif _porter_measure(stem) > 1:
                w = stem
            break

    # Step 5a
    if w.endswith("e"):
        stem = w[:-1]
        m = _porter_measure(stem)
        if m > 1 or (m == 1 and not _porter_cvc(stem)):
            w = stem

    # Step 5b
    if _porter_measure(w) > 1 and _porter_double_consonant(w) and w.endswith("l"):
        w = w[:-1]

    return w


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
    """Turn arbitrary user text into a small, literal-only FTS5 OR query, stripping stopwords before slicing."""
    seen: set[str] = set()
    terms: list[str] = []
    fallback_terms: list[str] = []
    for token in TOKEN_PATTERN.findall(str(query or "")):
        normalized = token.casefold()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in FTS_STOP_WORDS:
            if len(fallback_terms) < MAX_QUERY_TERMS:
                fallback_terms.append(normalized)
            continue
        terms.append(normalized)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    chosen = terms if terms else fallback_terms
    return " OR ".join(f'"{term}"' for term in chosen)


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
                atom_path = safe_file_in_dir(atoms_dir, filename, allowed_exts={".md"})
                if not atom_path or _is_reparse_or_link(atom_path):
                    raise RuntimeError(f"Refusing to index unsafe, symlink, or reparse point atom: {filename}")
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


def upsert_lexical_atoms(
    atoms_dir: str,
    index_path: str,
    changed_filenames: list[str] | tuple[str, ...] | set[str],
    deleted_filenames: list[str] | tuple[str, ...] | set[str] = (),
) -> dict[str, object]:
    """Incrementally update specific rows in the FTS5 index in O(|changed|) time.

    Falls back automatically to full atomic rebuild if the index does not yet
    exist or its schema is unrecognized. Must be called while holding writer.lock.
    """
    destination = Path(index_path).resolve()
    if not destination.is_file():
        return rebuild_lexical_index(atoms_dir, index_path)

    changed_clean = sorted({Path(f).name for f in changed_filenames if f and str(f).endswith(".md") and Path(f).name != "README.md"})
    deleted_clean = sorted({Path(f).name for f in deleted_filenames if f and str(f).endswith(".md")})
    target_names = sorted(set(changed_clean) | set(deleted_clean))

    try:
        conn = sqlite3.connect(str(destination), timeout=10.0)
        try:
            meta = _read_metadata(conn)
            if not isinstance(meta, dict) or meta.get("schema_version") != LEXICAL_SCHEMA_VERSION:
                conn.close()
                return rebuild_lexical_index(atoms_dir, index_path)

            with conn:
                if target_names:
                    placeholders = ",".join("?" for _ in target_names)
                    conn.execute(f"DELETE FROM {FTS_TABLE} WHERE filename IN ({placeholders})", target_names)
                for fname in changed_clean:
                    candidate_path = os.path.join(atoms_dir, fname)
                    if not os.path.lexists(candidate_path) or _is_reparse_or_link(candidate_path):
                        continue
                    atom_path = safe_file_in_dir(atoms_dir, fname, allowed_exts={".md"})
                    if not atom_path or not os.path.isfile(atom_path) or _is_reparse_or_link(atom_path):
                        continue
                    with open(atom_path, "rb") as fh:
                        raw = fh.read()
                    text = raw.decode("utf-8")
                    digest = hashlib.sha256(raw).hexdigest()
                    record_known_atom_hash(atom_path, digest)
                    conn.execute(
                        f"INSERT INTO {FTS_TABLE}(filename, content, content_sha256) VALUES (?, ?, ?)",
                        (fname, text, digest),
                    )
                row_count_row = conn.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()
                indexed_count = int(row_count_row[0]) if row_count_row else 0
                revisions = atom_revisions(atoms_dir)
                if indexed_count != len(revisions):
                    # Self-heal if out-of-band file changes occurred since last sync
                    raise RuntimeError("Row count drifted from disk; triggering clean atomic rebuild.")
                fp = revisions_fingerprint(revisions)
                updated_meta = {
                    "schema_version": LEXICAL_SCHEMA_VERSION,
                    "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "atom_count": len(revisions),
                    "indexed_count": indexed_count,
                    "atom_revisions_sha256": fp,
                }
                conn.executemany(
                    f"INSERT OR REPLACE INTO {META_TABLE}(key, value) VALUES (?, ?)",
                    [(k, json.dumps(v, ensure_ascii=False)) for k, v in updated_meta.items()],
                )
        finally:
            conn.close()
    except Exception:
        return rebuild_lexical_index(atoms_dir, index_path)

    return {
        "indexed_count": indexed_count,
        "atom_count": len(revisions),
        "index_path": str(destination),
        "atom_revisions_sha256": fp,
        "incremental": True,
    }


def sync_lexical_index(atoms_dir: str, index_path: str) -> dict[str, object]:
    """Reconcile the index with disk by diffing (filename, sha256) and upserting only the delta.

    Handles out-of-band edits (external editors, sync tools) in O(changed) index writes.
    Must be called while holding writer.lock.
    """
    destination = Path(index_path).resolve()
    if lexical_index_is_current(atoms_dir, index_path):
        return {"changed": 0, "deleted": 0, "rebuilt": False}
    if not destination.is_file():
        stats = rebuild_lexical_index(atoms_dir, index_path)
        return {"changed": stats["indexed_count"], "deleted": 0, "rebuilt": True}
    try:
        conn = _readonly_connection(index_path)
        try:
            indexed = dict(conn.execute(f"SELECT filename, content_sha256 FROM {FTS_TABLE}").fetchall())
        finally:
            conn.close()
    except (LexicalIndexUnavailable, sqlite3.Error):
        stats = rebuild_lexical_index(atoms_dir, index_path)
        return {"changed": stats["indexed_count"], "deleted": 0, "rebuilt": True}
    on_disk = {rev["filename"]: rev["content_sha256"] for rev in atom_revisions(atoms_dir)}
    changed = [name for name, digest in on_disk.items() if indexed.get(name) != digest]
    deleted = [name for name in indexed if name not in on_disk]
    upsert_lexical_atoms(atoms_dir, index_path, changed, deleted)
    return {"changed": len(changed), "deleted": len(deleted), "rebuilt": False}


def list_indexed_atoms_fast(
    index_path: str,
    *,
    query: str = "",
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[LexicalCandidate], int]:
    """Fast paginated browse/search directly from FTS5 sidecar for thousands of notes."""
    connection = _readonly_connection(index_path)
    try:
        safe_limit = max(1, min(int(limit), 100_000))
        safe_offset = max(0, min(int(offset), 1_000_000))
        expr = _fts_query(query) if query and query.strip() else ""
        if expr:
            total_row = connection.execute(
                f"SELECT COUNT(*) FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
                (expr,),
            ).fetchone()
            rows = connection.execute(
                f"SELECT filename, content, content_sha256, rank "
                f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
                (expr, safe_limit, safe_offset),
            ).fetchall()
        else:
            total_row = connection.execute(f"SELECT COUNT(*) FROM {FTS_TABLE}").fetchone()
            rows = connection.execute(
                f"SELECT filename, content, content_sha256, 0.0 AS rank "
                f"FROM {FTS_TABLE} ORDER BY filename ASC LIMIT ? OFFSET ?",
                (safe_limit, safe_offset),
            ).fetchall()
        total = int(total_row[0]) if total_row else 0
    except sqlite3.Error as exc:
        raise LexicalIndexUnavailable(f"Lexical list query failed: {exc}") from exc
    finally:
        connection.close()

    items = [
        LexicalCandidate(
            filename=str(filename),
            content=str(content),
            content_sha256=str(content_sha256),
            rank=float(rank),
        )
        for filename, content, content_sha256, rank in rows
    ]
    return items, total


def _search_lexical_index_verified(
    query: str,
    index_path: str,
    n_results: int = 5,
    atoms_dir: str | None = None,
) -> tuple[list[LexicalCandidate], dict[str, str] | None]:
    """Return (candidates, verified_filename_sha_map) using one SQLite open and one atoms_dir scan."""
    expression = _fts_query(query)
    try:
        limit = max(1, min(int(n_results), MAX_QUERY_RESULTS))
    except (TypeError, ValueError):
        limit = 5

    verified_map: dict[str, str] | None = None
    connection = _readonly_connection(index_path)
    try:
        if atoms_dir is not None:
            metadata = _read_metadata(connection)
            if not isinstance(metadata, dict) or metadata.get("schema_version") != LEXICAL_SCHEMA_VERSION:
                raise LexicalIndexUnavailable("Lexical recall index is stale; rebuild it before serving memory.")
            revisions = atom_revisions(atoms_dir)
            if not (
                metadata.get("atom_count") == len(revisions)
                and metadata.get("indexed_count") == len(revisions)
                and metadata.get("atom_revisions_sha256") == revisions_fingerprint(revisions)
            ):
                raise LexicalIndexUnavailable("Lexical recall index is stale; rebuild it before serving memory.")
            verified_map = {item["filename"]: item["content_sha256"] for item in revisions}
        if not expression:
            return [], verified_map
        rows = connection.execute(
            f"SELECT filename, content, content_sha256, rank "
            f"FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (expression, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        raise LexicalIndexUnavailable(f"Lexical recall query failed: {exc}") from exc
    finally:
        connection.close()

    candidates = [
        LexicalCandidate(
            filename=str(filename),
            content=str(content),
            content_sha256=str(content_sha256),
            rank=float(rank),
        )
        for filename, content, content_sha256, rank in rows
    ]
    return candidates, verified_map


def search_lexical_index(
    query: str,
    index_path: str,
    n_results: int = 5,
    atoms_dir: str | None = None,
) -> list[LexicalCandidate]:
    """Return FTS5 candidates only when the sidecar is current and query syntax is safe."""
    candidates, _ = _search_lexical_index_verified(
        query, index_path, n_results=n_results, atoms_dir=atoms_dir
    )
    return candidates
