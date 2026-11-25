# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Opt-In Continuous Background Screen OCR ("Eyes") with DLP & Retention Expiry.

Ships strictly **disabled by default** (`enabled=False`). When enabled by the owner:
- Hard-skips sensitive/private windows (password managers, private tabs, `.env`, banking).
- Deduplicates unchanged frames via 64-bit perceptual `dHash`.
- Runs local-only OCR (custom callback, Tesseract CLI, or Windows WinRT OCR; never remote).
- Scrubs secrets via `host_adapters.inspect_and_redact_outbound` BEFORE storing FTS5 text
  inside `brain/.sidecar/screen_memory.sqlite3`.
- Enforces automatic rolling retention purge (`retention_days`, default 7 days).
- Never promotes screen captures into canonical `2-atoms/` without explicit owner action.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
from collections.abc import Callable
from contextlib import closing
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

if __name__ in ("screen_memory", "scripts.screen_memory"):
    sys.modules.setdefault("screen_memory", sys.modules[__name__])
    sys.modules.setdefault("scripts.screen_memory", sys.modules[__name__])

from scripts.atom_mutator import atomic_write_bytes  # noqa: E402
from scripts.config import (  # noqa: E402
    BRAIN_DIR,
    _is_reparse_or_link,
    resolve_module_vault_path,
    security_state_dir,
    snapshot_module_defaults,
)
from scripts.doc_extractors import _run_local_ocr_on_png  # noqa: E402
from scripts.host_adapters import inspect_and_redact_outbound, restrict_security_file_permissions  # noqa: E402
from scripts.notes_store import NotePaths, create_note  # noqa: E402
from scripts.video_extractor import DHASH_DEDUP_THRESHOLD, compute_dhash64, hamming_distance64  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

SCREEN_CONFIG_FILENAME = "screen-memory-config.json"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_INTERVAL_SECONDS = 15
MIN_INTERVAL_SECONDS = 10

DEFAULT_SENSITIVE_WINDOW_TERMS: tuple[str, ...] = (
    "keepass",
    "1password",
    "bitwarden",
    "lastpass",
    "dashlane",
    "proton pass",
    "incognito",
    "inprivate",
    "private browsing",
    ".env",
    "password",
    "passphrase",
    "secret",
    "credential",
    "private key",
    "id_rsa",
    "seed phrase",
    "recovery phrase",
    "online banking",
    "bank account",
)


class ScreenMemoryError(ValueError):
    """Raised when screen memory configuration, permissions, or inputs are invalid."""


def _now_utc() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def screen_config_path(state_dir: str | Path | None = None) -> Path:
    raw_base = Path(os.path.expanduser(str(state_dir))) if state_dir is not None else Path(security_state_dir())
    if os.path.lexists(str(raw_base)) and _is_reparse_or_link(str(raw_base)):
        raise PermissionError(f"Refusing symlinked security state dir: {raw_base}")
    base = raw_base.resolve()
    if os.path.lexists(str(base)) and _is_reparse_or_link(str(base)):
        raise PermissionError(f"Refusing symlinked security state dir: {base}")
    base.mkdir(parents=True, exist_ok=True)
    target = base / SCREEN_CONFIG_FILENAME
    if os.path.lexists(str(target)) and _is_reparse_or_link(str(target)):
        raise PermissionError(f"Refusing symlinked screen memory config: {target}")
    return target


def load_screen_config(state_dir: str | Path | None = None) -> dict[str, Any]:
    cfg_path = screen_config_path(state_dir)
    defaults: dict[str, Any] = {
        "enabled": False,
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "blocked_window_terms": list(DEFAULT_SENSITIVE_WINDOW_TERMS),
        "updated_at": None,
    }
    if not cfg_path.is_file():
        return defaults
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            return defaults
    except Exception:
        return defaults
    loaded_terms = [
        str(t).strip().lower()
        for t in (loaded.get("blocked_window_terms") or ())
        if str(t).strip()
    ]
    blocked = list(dict.fromkeys([*DEFAULT_SENSITIVE_WINDOW_TERMS, *loaded_terms]))
    return {
        "enabled": bool(loaded.get("enabled", False)),
        "interval_seconds": max(MIN_INTERVAL_SECONDS, min(3600, int(loaded.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)))),
        "retention_days": max(1, min(365, int(loaded.get("retention_days", DEFAULT_RETENTION_DAYS)))),
        "blocked_window_terms": blocked,
        "updated_at": loaded.get("updated_at"),
    }


def configure_screen_memory(
    *,
    enabled: bool | None = None,
    interval_seconds: int | None = None,
    retention_days: int | None = None,
    blocked_window_terms: list[str] | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = load_screen_config(state_dir)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    if interval_seconds is not None:
        val = int(interval_seconds)
        if val < MIN_INTERVAL_SECONDS or val > 3600:
            raise ScreenMemoryError(f"interval_seconds must be in {MIN_INTERVAL_SECONDS}..3600.")
        cfg["interval_seconds"] = val
    if retention_days is not None:
        ret = int(retention_days)
        if ret < 1 or ret > 365:
            raise ScreenMemoryError("retention_days must be in 1..365.")
        cfg["retention_days"] = ret
    if blocked_window_terms is not None:
        terms = [str(t).strip().lower() for t in blocked_window_terms if str(t).strip()]
        merged = list(dict.fromkeys([*DEFAULT_SENSITIVE_WINDOW_TERMS, *terms]))
        cfg["blocked_window_terms"] = merged
    cfg["updated_at"] = _now_iso()

    cfg_path = screen_config_path(state_dir)
    atomic_write_bytes(str(cfg_path), (json.dumps(cfg, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    restrict_security_file_permissions(cfg_path)
    return cfg


def screen_db_path(brain_dir: str | Path | None = None) -> str:
    b = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    if os.path.lexists(b) and _is_reparse_or_link(b):
        raise PermissionError(f"Refusing symlinked brain dir: {b}")
    sidecar = os.path.join(b, ".sidecar")
    if os.path.lexists(sidecar) and _is_reparse_or_link(sidecar):
        raise PermissionError(f"Refusing symlinked .sidecar dir: {sidecar}")
    os.makedirs(sidecar, exist_ok=True)
    db_file = os.path.join(sidecar, "screen_memory.sqlite3")
    if os.path.lexists(db_file) and _is_reparse_or_link(db_file):
        raise PermissionError(f"Refusing symlinked screen_memory DB: {db_file}")
    return db_file


def _connect_db(brain_dir: str | Path | None = None) -> sqlite3.Connection:
    db_file = screen_db_path(brain_dir)
    existed = os.path.exists(db_file)
    conn = sqlite3.connect(db_file, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS screen_captures (
            capture_id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            app_name TEXT NOT NULL DEFAULT '',
            window_title TEXT NOT NULL DEFAULT '',
            dhash64 TEXT NOT NULL DEFAULT '',
            image_sha256 TEXT NOT NULL DEFAULT '',
            ocr_engine TEXT NOT NULL DEFAULT '',
            secret_scrubbed INTEGER NOT NULL DEFAULT 0,
            ocr_text TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS screen_fts USING fts5(
            window_title,
            app_name,
            ocr_text,
            content='screen_captures',
            content_rowid='capture_id',
            tokenize='unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS screen_ai AFTER INSERT ON screen_captures BEGIN
            INSERT INTO screen_fts(rowid, window_title, app_name, ocr_text)
            VALUES (new.capture_id, new.window_title, new.app_name, new.ocr_text);
        END;
        CREATE TRIGGER IF NOT EXISTS screen_ad AFTER DELETE ON screen_captures BEGIN
            INSERT INTO screen_fts(screen_fts, rowid, window_title, app_name, ocr_text)
            VALUES ('delete', old.capture_id, old.window_title, old.app_name, old.ocr_text);
        END;
        """
    )
    if not existed:
        try:
            restrict_security_file_permissions(Path(db_file))
        except Exception:
            pass
    return conn


def is_window_blocked(window_title: str, app_name: str = "", *, blocked_terms: list[str] | tuple[str, ...] = DEFAULT_SENSITIVE_WINDOW_TERMS) -> str | None:
    hay = f"{app_name or ''} {window_title or ''}".casefold()
    for term in blocked_terms:
        clean = str(term or "").strip().casefold()
        if clean and clean in hay:
            return clean
    return None


def _run_windows_winrt_ocr(png_bytes: bytes) -> tuple[str, str | None]:
    if sys.platform != "win32":
        return "", None
    ps = shutil.which("powershell") or shutil.which("pwsh")
    if not ps:
        return "", None
    fd, tmp_png = tempfile.mkstemp(prefix=".sb-winocr-", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(png_bytes)
        ps_script = (
            "$ErrorActionPreference='Stop';"
            "Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
            "[Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]|Out-Null;"
            "[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]|Out-Null;"
            "[Windows.Media.Ocr.OcrEngine,Windows.Media.Ocr,ContentType=WindowsRuntime]|Out-Null;"
            "$asTask=([System.WindowsRuntimeSystemExtensions].GetMethods()|Where-Object{$_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethod})[0];"
            "function Await($op,$t){$m=$asTask.MakeGenericMethod($t);$task=$m.Invoke($null,@($op));$task.Wait();return $task.Result};"
            "$f=Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:SB_OCR_PNG)) ([Windows.Storage.StorageFile]);"
            "$s=Await ($f.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream]);"
            "$dec=Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($s)) ([Windows.Graphics.Imaging.BitmapDecoder]);"
            "$bmp=Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap]);"
            "$eng=[Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages();"
            "if($eng){$res=Await ($eng.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult]);$res.Text}"
        )
        env = {**os.environ, "SB_OCR_PNG": tmp_png}
        proc = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", ps_script], env=env, capture_output=True, text=True, timeout=15, check=False)
        txt = (proc.stdout or "").strip() if proc.returncode == 0 else ""
        return (txt, "windows-winrt-ocr") if txt else ("", None)
    except Exception:
        return "", None
    finally:
        if os.path.exists(tmp_png):
            try:
                os.unlink(tmp_png)
            except OSError:
                pass


def ingest_screen_frame(
    png_bytes: bytes,
    *,
    window_title: str = "",
    app_name: str = "",
    brain_dir: str | Path | None = None,
    state_dir: str | Path | None = None,
    ocr_fn: Callable[[bytes], str] | None = None,
    require_enabled: bool = True,
) -> dict[str, Any]:
    """Process one PNG screen capture: enforce opt-in + blocklist + dHash dedup + local OCR + DLP scrub."""
    cfg = load_screen_config(state_dir)
    if require_enabled and not cfg.get("enabled"):
        raise PermissionError("Screen OCR ('Eyes') is disabled by default. Opt in via 'second-brain screen enable' first.")

    matched_term = is_window_blocked(window_title, app_name, blocked_terms=cfg["blocked_window_terms"])
    if matched_term:
        return {"status": "skipped_blocked_window", "matched_term": matched_term}

    if not isinstance(png_bytes, (bytes, bytearray)) or not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ScreenMemoryError("Screen capture frame must be a valid PNG image payload.")

    raw_bytes = bytes(png_bytes)
    img_sha = hashlib.sha256(raw_bytes).hexdigest()

    fd, tmp_png = tempfile.mkstemp(prefix=".sb-screen-", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw_bytes)
        dhash_int = compute_dhash64(tmp_png)
    finally:
        if os.path.exists(tmp_png):
            try:
                os.unlink(tmp_png)
            except OSError:
                pass
    dhash_hex = f"{dhash_int:016x}" if dhash_int is not None else ""

    prune_expired_captures(brain_dir=brain_dir)

    with closing(_connect_db(brain_dir)) as conn:
        last = conn.execute(
            "SELECT dhash64 FROM screen_captures ORDER BY capture_id DESC LIMIT 1"
        ).fetchone()
        if last and dhash_int is not None and str(last["dhash64"]):
            prev_int = int(str(last["dhash64"]), 16)
            if hamming_distance64(prev_int, dhash_int) <= DHASH_DEDUP_THRESHOLD:
                return {"status": "skipped_duplicate_frame", "dhash64": dhash_hex}

    ocr_text = ""
    engine = None
    if ocr_fn is not None:
        ocr_text = str(ocr_fn(raw_bytes) or "").strip()
        engine = "custom-ocr" if ocr_text else None
    else:
        ocr_text, engine = _run_local_ocr_on_png(raw_bytes, 1, allow_vision_ocr=False)
        if not ocr_text:
            ocr_text, engine = _run_windows_winrt_ocr(raw_bytes)

    if not ocr_text:
        return {"status": "skipped_empty_ocr", "dhash64": dhash_hex}

    app_report = inspect_and_redact_outbound(str(app_name or "")[:120], redact_pii=False, fail_on_secret_detection=False)
    title_report = inspect_and_redact_outbound(str(window_title or "")[:300], redact_pii=False, fail_on_secret_detection=False)
    text_report = inspect_and_redact_outbound(ocr_text[:24_000], redact_pii=False, fail_on_secret_detection=False)
    scrubbed = bool(app_report.secret_hits or title_report.secret_hits or text_report.secret_hits)

    now_dt = _now_utc()
    exp_dt = now_dt + datetime.timedelta(days=int(cfg["retention_days"]))
    with closing(_connect_db(brain_dir)) as conn, conn:
        cur = conn.execute(
            """
            INSERT INTO screen_captures (
                captured_at, expires_at, app_name, window_title, dhash64,
                image_sha256, ocr_engine, secret_scrubbed, ocr_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_dt.isoformat(timespec="seconds"),
                exp_dt.isoformat(timespec="seconds"),
                app_report.sanitized_text[:120],
                title_report.sanitized_text,
                dhash_hex,
                img_sha,
                str(engine or "local-ocr"),
                1 if scrubbed else 0,
                text_report.sanitized_text,
            ),
        )
        cid = int(cur.lastrowid or 0)

    return {
        "status": "indexed",
        "capture_id": cid,
        "dhash64": dhash_hex,
        "ocr_engine": engine,
        "secret_scrubbed": scrubbed,
        "secret_hits": list(dict.fromkeys([*app_report.secret_hits, *title_report.secret_hits, *text_report.secret_hits])),
        "expires_at": exp_dt.isoformat(timespec="seconds"),
    }


def prune_expired_captures(*, brain_dir: str | Path | None = None, now_iso: str | None = None) -> int:
    cutoff = str(now_iso or _now_iso())
    with closing(_connect_db(brain_dir)) as conn, conn:
        cur = conn.execute("DELETE FROM screen_captures WHERE expires_at <= ?", (cutoff,))
        return int(cur.rowcount or 0)


def search_screen_memory(query: str, *, limit: int = 12, brain_dir: str | Path | None = None) -> list[dict[str, Any]]:
    prune_expired_captures(brain_dir=brain_dir)
    q = str(query or "").strip()
    lim = max(1, min(50, int(limit)))
    with closing(_connect_db(brain_dir)) as conn:
        if not q:
            rows = conn.execute(
                """
                SELECT capture_id, captured_at, expires_at, app_name, window_title,
                       ocr_engine, secret_scrubbed, ocr_text
                FROM screen_captures
                ORDER BY capture_id DESC LIMIT ?
                """,
                (lim,),
            ).fetchall()
            return [dict(r) for r in rows]

        tokens = [re.sub(r"[^A-Za-z0-9_]+", "", t) for t in q.split() if re.sub(r"[^A-Za-z0-9_]+", "", t)]
        if not tokens:
            return []
        fts_query = " ".join(f'"{tok}"' for tok in tokens[:10])
        rows = conn.execute(
            """
            SELECT c.capture_id, c.captured_at, c.expires_at, c.app_name, c.window_title,
                   c.ocr_engine, c.secret_scrubbed, c.ocr_text,
                   bm25(screen_fts) AS rank_score
            FROM screen_fts
            JOIN screen_captures c ON c.capture_id = screen_fts.rowid
            WHERE screen_fts MATCH ?
            ORDER BY rank_score ASC, c.capture_id DESC
            LIMIT ?
            """,
            (fts_query, lim),
        ).fetchall()
    return [dict(r) for r in rows]


def promote_capture_to_note(capture_id: int, *, brain_dir: str | Path | None = None, tags: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    """Explicit owner action to save one Screen OCR capture as a Working Note (never canonical 2-atoms/)."""
    with closing(_connect_db(brain_dir)) as conn:
        row = conn.execute("SELECT * FROM screen_captures WHERE capture_id = ?", (int(capture_id),)).fetchone()
        if row is None:
            raise FileNotFoundError(f"Screen capture {capture_id} not found.")
    rec = dict(row)
    title = f"Screen Note: {rec.get('window_title') or rec.get('app_name') or f'Capture #{capture_id}'}"[:140]
    body = (
        f"> Promoted from Screen OCR capture #{capture_id} at {rec.get('captured_at')}\n"
        f"> App: `{rec.get('app_name') or 'unknown'}` · Engine: `{rec.get('ocr_engine')}`\n\n"
        f"{rec.get('ocr_text')}\n"
    )
    resolved_brain = resolve_module_vault_path(globals(), "BRAIN_DIR", str(brain_dir) if brain_dir is not None else None)
    created = create_note(
        title,
        body,
        tags=["screen-ocr", *[str(t) for t in tags if str(t).strip()]],
        paths=NotePaths.for_brain(resolved_brain),
    )
    return {"status": "promoted_to_note", "capture_id": int(capture_id), "note": created}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_st = sub.add_parser("status")
    p_st.add_argument("--json", action="store_true")

    p_en = sub.add_parser("enable")
    p_en.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    p_en.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)

    sub.add_parser("disable")

    p_s = sub.add_parser("search")
    p_s.add_argument("query", nargs="?", default="")
    p_s.add_argument("--limit", type=int, default=12)

    p_pr = sub.add_parser("prune")
    p_pr.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(load_screen_config(), indent=2, ensure_ascii=False))
    elif args.cmd == "enable":
        print(json.dumps(configure_screen_memory(enabled=True, interval_seconds=args.interval, retention_days=args.retention_days), indent=2))
    elif args.cmd == "disable":
        print(json.dumps(configure_screen_memory(enabled=False), indent=2))
    elif args.cmd == "prune":
        print(json.dumps({"pruned": prune_expired_captures()}, indent=2))
    else:
        print(json.dumps({"results": search_screen_memory(args.query, limit=args.limit)}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
