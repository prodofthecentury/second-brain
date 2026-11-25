# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Opt-In Read-Only External PC Folder Semantic / FTS5 Indexer.

Ships strictly **disabled by default** (`enabled=False`). When enabled by the owner:
- Indexes allowlisted local project directories in place without copying files into `brain/`.
- Hard-excludes sensitive system roots and credential files (`.ssh`, `.gnupg`, `.aws`,
  `.codex`, `.env*`, `*.pem`, `*.pfx`, `node_modules`, `.venv`, `.git`) plus `.gitignore` rules.
- Runs `inspect_and_redact_outbound` over snippets before persisting FTS5 chunks into
  `brain/.sidecar/external_files.sqlite3`.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("external_indexer", "scripts.external_indexer"):
    sys.modules.setdefault("external_indexer", sys.modules[__name__])
    sys.modules.setdefault("scripts.external_indexer", sys.modules[__name__])

from atom_mutator import atomic_write_bytes  # noqa: E402
from config import (  # noqa: E402
    BRAIN_DIR,
    _is_reparse_or_link,
    resolve_module_vault_path,
    security_state_dir,
    snapshot_module_defaults,
)
from host_adapters import inspect_and_redact_outbound, restrict_security_file_permissions  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

EXTERNAL_CONFIG_FILENAME = "external-folders-config.json"
MAX_EXTERNAL_FILE_BYTES = 512 * 1024
MAX_INDEX_FILES_PER_ROOT = 2000

EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    ".ssh", ".gnupg", ".aws", ".codex", ".azure", ".kube",
    ".gemini", ".claude", ".cursor", "secondbrain",
    "appdata", "programdata", "windows",
})
EXCLUDED_FILE_PATTERNS = (
    ".env*", "*.pem", "*.key", "*.pfx", "*.p12", "*.crt", "*.kdbx",
    "id_rsa*", "id_ed25519*", "*credentials*", "*secret*", "*.sqlite*", "*.db",
)
INDEXABLE_TEXT_EXTS = frozenset({
    ".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml",
    ".yaml", ".yml", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".sh", ".ps1", ".sql", ".html", ".css", ".lua",
})


class ExternalIndexerError(ValueError):
    """Raised on invalid or forbidden external folder paths or disabled access."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def external_config_path(state_dir: str | Path | None = None) -> Path:
    raw_base = Path(os.path.expanduser(str(state_dir))) if state_dir is not None else Path(security_state_dir())
    if os.path.lexists(str(raw_base)) and _is_reparse_or_link(str(raw_base)):
        raise PermissionError(f"Refusing symlinked security state dir: {raw_base}")
    base = raw_base.resolve()
    if os.path.lexists(str(base)) and _is_reparse_or_link(str(base)):
        raise PermissionError(f"Refusing symlinked security state dir: {base}")
    base.mkdir(parents=True, exist_ok=True)
    target = base / EXTERNAL_CONFIG_FILENAME
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing symlinked external config file: {target}")
    return target


def load_external_config(state_dir: str | Path | None = None) -> dict[str, Any]:
    cfg_p = external_config_path(state_dir)
    default_cfg: dict[str, Any] = {"enabled": False, "folders": [], "updated_at": None}
    if not cfg_p.is_file():
        return default_cfg
    try:
        raw = json.loads(cfg_p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_cfg
    except Exception:
        return default_cfg
    folders: list[str] = []
    for f in (raw.get("folders") or []):
        s = str(f or "").strip()
        if not s:
            continue
        fp = Path(os.path.expanduser(s))
        if os.path.lexists(str(fp)) and _is_reparse_or_link(str(fp)):
            continue
        folders.append(str(fp.resolve()))
    return {
        "enabled": bool(raw.get("enabled", False)),
        "folders": list(dict.fromkeys(folders)),
        "updated_at": raw.get("updated_at"),
    }


def _save_external_config(cfg: dict[str, Any], *, state_dir: str | Path | None = None) -> dict[str, Any]:
    cfg_p = external_config_path(state_dir)
    cfg["updated_at"] = _now_iso()
    atomic_write_bytes(str(cfg_p), (json.dumps(cfg, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    restrict_security_file_permissions(cfg_p)
    return cfg


_FORBIDDEN_EXTERNAL_PATH_PARTS = frozenset({
    ".ssh",
    ".gnupg",
    ".aws",
    ".codex",
    ".azure",
    ".kube",
    ".gemini",
    ".claude",
    ".cursor",
    "secondbrain",
    "appdata",
    "programdata",
    "windows",
    "system32",
    "etc",
})


def validate_external_folder_root(raw_path: str | Path) -> Path:
    p = Path(os.path.expanduser(str(raw_path or "").strip()))
    if not str(p) or not p.is_absolute():
        raise ExternalIndexerError("External folder path must be an absolute directory path.")
    if os.path.lexists(str(p)) and _is_reparse_or_link(str(p)):
        raise PermissionError(f"Refusing symlink/reparse external folder: {p}")
    resolved = p.resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"External folder does not exist: {resolved}")
    # Never allow filesystem root, home root directly, or system/credential directories
    if resolved == resolved.anchor or str(resolved) == resolved.anchor:
        raise ExternalIndexerError("Refusing to index filesystem root.")
    if resolved == Path.home().resolve():
        raise ExternalIndexerError("Refusing to index entire user home directory; pick a specific subfolder.")
    sec_dir = security_state_dir().resolve()
    if resolved == sec_dir or sec_dir in resolved.parents or resolved in sec_dir.parents:
        raise PermissionError(f"Refusing security state directory or ancestor: {resolved}")

    parts_to_check = resolved.parts
    if os.name == "nt":
        # Windows TEMP lives under %LOCALAPPDATA%\Temp; allow isolated subdirs strictly inside TEMP
        # while still rejecting TEMP itself and any forbidden segment below TEMP (or outside TEMP).
        temp_root = Path(tempfile.gettempdir()).resolve()
        if temp_root in resolved.parents:
            parts_to_check = resolved.relative_to(temp_root).parts
    low_parts = {part.lower() for part in parts_to_check}
    if low_parts & _FORBIDDEN_EXTERNAL_PATH_PARTS:
        raise PermissionError(f"Refusing sensitive system/credential directory: {resolved}")
    return resolved


def _prune_external_db(conn: sqlite3.Connection, allowed_roots: list[str]) -> int:
    """Delete indexed DB rows whose root_dir is no longer allowlisted or whose file disappeared/became a symlink."""
    allowed_set = {str(Path(r).resolve()) for r in allowed_roots if str(r).strip()}
    rows = conn.execute("SELECT file_id, root_dir, file_path FROM external_files").fetchall()
    to_delete: list[int] = []
    for row in rows:
        fid = int(row["file_id"])
        rdir = str(row["root_dir"] or "")
        fpath = str(row["file_path"] or "")
        if rdir not in allowed_set or not os.path.isfile(fpath) or _is_reparse_or_link(fpath):
            to_delete.append(fid)
    for fid in to_delete:
        conn.execute("DELETE FROM external_files WHERE file_id = ?", (fid,))
    return len(to_delete)


def configure_external_indexer(
    *,
    enabled: bool | None = None,
    add_folder: str | Path | None = None,
    remove_folder: str | Path | None = None,
    state_dir: str | Path | None = None,
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = load_external_config(state_dir)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if add_folder is not None:
        clean = str(validate_external_folder_root(add_folder))
        if clean not in cfg["folders"]:
            cfg["folders"].append(clean)
    if remove_folder is not None:
        rem_raw = Path(os.path.expanduser(str(remove_folder)))
        rem = str(rem_raw.resolve())
        rem_abs = os.path.abspath(str(rem_raw))
        cfg["folders"] = [f for f in cfg["folders"] if f not in {rem, rem_abs}]
        try:
            db_file = external_db_path(brain_dir)
            if os.path.isfile(db_file) and not _is_reparse_or_link(db_file):
                with closing(_connect_db(brain_dir)) as conn, conn:
                    _prune_external_db(conn, cfg["folders"])
        except Exception:
            pass
    return _save_external_config(cfg, state_dir=state_dir)


def external_db_path(brain_dir: str | Path | None = None) -> str:
    b = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    if os.path.lexists(b) and _is_reparse_or_link(b):
        raise PermissionError(f"Refusing symlinked brain dir: {b}")
    sidecar = os.path.join(b, ".sidecar")
    if os.path.lexists(sidecar) and _is_reparse_or_link(sidecar):
        raise PermissionError(f"Refusing symlinked .sidecar dir: {sidecar}")
    os.makedirs(sidecar, exist_ok=True)
    if _is_reparse_or_link(sidecar):
        raise PermissionError(f"Refusing symlinked .sidecar dir: {sidecar}")
    db_file = os.path.join(sidecar, "external_files.sqlite3")
    if os.path.lexists(db_file) and _is_reparse_or_link(db_file):
        raise PermissionError(f"Refusing symlinked external_files DB: {db_file}")
    return db_file


def _connect_db(brain_dir: str | Path | None = None) -> sqlite3.Connection:
    db_file = external_db_path(brain_dir)
    existed = os.path.exists(db_file)
    conn = sqlite3.connect(db_file, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS external_files (
            file_id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_dir TEXT NOT NULL,
            file_path TEXT NOT NULL UNIQUE,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS external_fts USING fts5(
            file_path,
            content,
            content='external_files',
            content_rowid='file_id',
            tokenize='unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS ext_ai AFTER INSERT ON external_files BEGIN
            INSERT INTO external_fts(rowid, file_path, content)
            VALUES (new.file_id, new.file_path, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS ext_ad AFTER DELETE ON external_files BEGIN
            INSERT INTO external_fts(external_fts, rowid, file_path, content)
            VALUES ('delete', old.file_id, old.file_path, old.content);
        END;
        """
    )
    if not existed:
        try:
            restrict_security_file_permissions(Path(db_file))
        except Exception:
            pass
    return conn


def _load_gitignore_globs(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if not gi.is_file() or _is_reparse_or_link(str(gi)):
        return []
    pats: list[str] = []
    for raw_ln in gi.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = raw_ln.strip()
        if not ln or ln.startswith("#") or ln.startswith("!"):
            continue
        pats.append(ln.rstrip("/"))
    return pats[:200]


def _is_excluded_rel(rel_posix: str, filename: str, gi_patterns: list[str]) -> bool:
    low_name = filename.lower()
    for pat in EXCLUDED_FILE_PATTERNS:
        if fnmatch.fnmatch(low_name, pat.lower()):
            return True
    for pat in gi_patterns:
        if fnmatch.fnmatch(filename, pat) or fnmatch.fnmatch(rel_posix, pat) or any(fnmatch.fnmatch(part, pat) for part in rel_posix.split("/")):
            return True
    return False


def scan_external_folders(
    *,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    require_enabled: bool = True,
) -> dict[str, Any]:
    cfg = load_external_config(state_dir)
    if require_enabled and not cfg.get("enabled"):
        raise PermissionError("External Folder Indexer is disabled by default. Enable it first.")
    folders = list(cfg.get("folders") or [])
    indexed_count = 0
    reused_count = 0
    skipped_secret = 0
    pruned_count = 0
    valid_roots: list[str] = []

    with closing(_connect_db(brain_dir)) as conn, conn:
        for raw_root in folders:
            try:
                root = validate_external_folder_root(raw_root)
            except Exception:
                continue
            valid_roots.append(str(root))
            gi_pats = _load_gitignore_globs(root)
            root_seen = 0
            for cur_dir, dirnames, filenames in os.walk(str(root), followlinks=False):
                dirnames[:] = [
                    d for d in sorted(dirnames)
                    if d.lower() not in EXCLUDED_DIR_NAMES
                    and not _is_reparse_or_link(os.path.join(cur_dir, d))
                    and d not in gi_pats
                ]
                for fn in sorted(filenames):
                    if root_seen >= MAX_INDEX_FILES_PER_ROOT:
                        break
                    fp = Path(cur_dir) / fn
                    if _is_reparse_or_link(str(fp)) or fp.suffix.lower() not in INDEXABLE_TEXT_EXTS:
                        continue
                    rel_posix = fp.relative_to(root).as_posix()
                    if _is_excluded_rel(rel_posix, fn, gi_pats):
                        skipped_secret += 1
                        continue
                    try:
                        st = fp.lstat()
                    except OSError:
                        continue
                    if st.st_size <= 0 or st.st_size > MAX_EXTERNAL_FILE_BYTES:
                        continue
                    existing = conn.execute(
                        "SELECT file_id, mtime_ns FROM external_files WHERE file_path = ?",
                        (str(fp),),
                    ).fetchone()
                    if existing and int(existing["mtime_ns"]) == int(st.st_mtime_ns):
                        reused_count += 1
                        root_seen += 1
                        continue
                    try:
                        raw_text = fp.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    # Scan full bounded file content (<= MAX_EXTERNAL_FILE_BYTES) before truncating
                    red = inspect_and_redact_outbound(raw_text, redact_pii=False, fail_on_secret_detection=False)
                    if red.secret_hits:
                        skipped_secret += 1
                        if existing:
                            conn.execute("DELETE FROM external_files WHERE file_id = ?", (int(existing["file_id"]),))
                        continue
                    sha = hashlib.sha256(raw_text.encode("utf-8", errors="ignore")).hexdigest()
                    if existing:
                        conn.execute("DELETE FROM external_files WHERE file_id = ?", (int(existing["file_id"]),))
                    conn.execute(
                        """
                        INSERT INTO external_files (root_dir, file_path, mtime_ns, sha256, indexed_at, content)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (str(root), str(fp), int(st.st_mtime_ns), sha, _now_iso(), red.sanitized_text[:24_000]),
                    )
                    indexed_count += 1
                    root_seen += 1

        pruned_count = _prune_external_db(conn, valid_roots)

    return {
        "status": "ok",
        "folders": folders,
        "indexed": indexed_count,
        "reused_unchanged": reused_count,
        "excluded_or_scrubbed": skipped_secret,
        "pruned": pruned_count,
    }


def search_external_files(
    query: str,
    *,
    limit: int = 12,
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    require_enabled: bool = True,
) -> list[dict[str, Any]]:
    cfg = load_external_config(state_dir)
    if require_enabled and not cfg.get("enabled"):
        raise PermissionError("External Folder Indexer is disabled by default.")
    allowed_roots = [str(Path(f).resolve()) for f in (cfg.get("folders") or []) if str(f).strip()]
    if not allowed_roots:
        return []
    q = str(query or "").strip()
    if not q:
        return []
    tokens = [re.sub(r"[^A-Za-z0-9_]+", "", t) for t in q.split() if re.sub(r"[^A-Za-z0-9_]+", "", t)]
    if not tokens:
        return []
    fts_query = " ".join(f'"{tok}"' for tok in tokens[:10])
    lim = max(1, min(50, int(limit)))
    placeholders = ",".join("?" for _ in allowed_roots)
    with closing(_connect_db(brain_dir)) as conn:
        rows = conn.execute(
            f"""
            SELECT f.file_path, f.root_dir, f.sha256, f.indexed_at,
                   substr(f.content, 1, 500) AS snippet, bm25(external_fts) AS rank_score
            FROM external_fts
            JOIN external_files f ON f.file_id = external_fts.rowid
            WHERE external_fts MATCH ?
              AND f.root_dir IN ({placeholders})
            ORDER BY rank_score ASC
            LIMIT ?
            """,
            (fts_query, *allowed_roots, lim),
        ).fetchall()
    return [dict(r) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    p_add = sub.add_parser("add")
    p_add.add_argument("folder")
    p_rem = sub.add_parser("remove")
    p_rem.add_argument("folder")
    sub.add_parser("scan")
    p_s = sub.add_parser("search")
    p_s.add_argument("query")
    p_s.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(load_external_config(), indent=2))
    elif args.cmd == "enable":
        print(json.dumps(configure_external_indexer(enabled=True), indent=2))
    elif args.cmd == "disable":
        print(json.dumps(configure_external_indexer(enabled=False), indent=2))
    elif args.cmd == "add":
        print(json.dumps(configure_external_indexer(add_folder=args.folder), indent=2))
    elif args.cmd == "remove":
        print(json.dumps(configure_external_indexer(remove_folder=args.folder), indent=2))
    elif args.cmd == "scan":
        print(json.dumps(scan_external_folders(require_enabled=True), indent=2))
    else:
        print(json.dumps({"results": search_external_files(args.query, limit=args.limit)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
