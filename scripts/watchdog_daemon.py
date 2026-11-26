# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "watchdog",
#     "ollama",
#     "chromadb",
#     "faster-whisper",
#     "Pillow",
# ]
# ///
"""
Feature 2 — Real-Time Watchdog Daemon
Monitors 0-raw/, notes/, and 2-atoms/ with hardware-tiered gating:

  Documents (.pdf/.docx/.epub/.html/.csv/.tsv) → Deterministic local extraction (all tiers)
  Audio files                                  → faster-whisper transcribe (when installed & RAM >= 6 GiB)
  Image files                                  → Pillow + Ollama vision (Tier 2+ with Ollama & Pillow)
  .md / .txt / approved .intake                → Refinery LLM drafting (Tier 2+ with reachable Ollama)
  notes/ & 2-atoms/                            → Debounced model-free FTS5 sync + read-only stale-anchor check

Constrained tiers (Tier 0 / Tier 1 or offline Ollama) archive/retain captures safely in 0-raw/ and
record a deferred-queue entry without counterfeiting atoms or spinning in a retry loop.

Run this once and leave it running:
    uv run scripts/watchdog_daemon.py
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import logging
import os
import queue
import sys
import threading
import time

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # Clean minimal wheel without optional [watchdog] extra
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment,misc]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atom_mutator import atomic_write_bytes, review_lock
from config import (
    ATOMS_DIR,
    AUDIO_EXTS,
    BRAIN_DIR,
    DOC_EXTS,
    IMAGE_EXTS,
    LEXICAL_INDEX_PATH,
    NOTES_DIR,
    RAW_DIR,
    SIDECAR_DIR,
    TEXT_EXTS,
    _is_reparse_or_link,
    ensure_dirs,
)
from hardware_profile import HardwareCapabilityProfile, detect_hardware_profile
from refinery_targets import intake_status, is_supported_refinery_target
from stable_file import wait_for_stable_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchdog")

# ─── Skip These Files ─────────────────────────────────────────────────────────
SKIP_NAMES = {"reading-list.md", "README.md"}
TEMP_SUFFIXES = (".tmp", ".part", ".crdownload", ".swp", ".bak", ".lock")
CONSTRAINED_LLM_TIERS = frozenset({"tier_0_lexical_only", "tier_1_cpu_compact"})


def should_ignore_watchdog_path(
    filepath: str,
    *,
    raw_dir: str | None = None,
    brain_dir: str | None = None,
) -> bool:
    """Return True for hidden, temporary, swap, sidecar/inbox/subdirectory, or symlinked files."""
    filename = os.path.basename(filepath)
    if (
        not filename
        or filename in SKIP_NAMES
        or filename.startswith((".", "~"))
        or filename.lower().endswith(TEMP_SUFFIXES)
    ):
        return True
    if os.path.lexists(filepath) and _is_reparse_or_link(filepath):
        return True
    target_raw = raw_dir or (os.path.join(brain_dir, "0-raw") if brain_dir else None)
    if target_raw is not None:
        abs_parent = os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(filepath))))
        abs_raw = os.path.normcase(os.path.abspath(target_raw))
        if abs_parent != abs_raw:
            return True
    else:
        parts = {p.lower() for p in os.path.normpath(os.path.abspath(filepath)).split(os.sep)}
        if {".sidecar", "inbox", "archive"} & parts:
            return True
    return False


def _should_ignore_markdown_watch_path(filepath: str, watched_dir: str) -> bool:
    """Filter events inside notes/ or 2-atoms/ to direct top-level canonical .md files only."""
    filename = os.path.basename(filepath)
    if (
        not filename
        or filename == "README.md"
        or filename.startswith((".", "~"))
        or filename.lower().endswith(TEMP_SUFFIXES)
        or os.path.splitext(filename)[1].lower() != ".md"
    ):
        return True
    if os.path.lexists(filepath) and _is_reparse_or_link(filepath):
        return True
    abs_parent = os.path.normcase(os.path.abspath(os.path.dirname(os.path.abspath(filepath))))
    abs_watched = os.path.normcase(os.path.abspath(watched_dir))
    return abs_parent != abs_watched


def get_daemon_hardware_profile() -> HardwareCapabilityProfile:
    """Probe cached hardware/Ollama readiness before launching automatic model extraction."""
    return detect_hardware_profile(ollama_timeout=2.0, use_cache=True)


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _sidecar_for_raw(raw_dir: str) -> str:
    brain_root = os.path.dirname(os.path.abspath(raw_dir))
    return os.path.join(brain_root, ".sidecar")


def _record_deferred_capture(filepath: str, *, stage: str, reason: str, raw_dir: str | None = None) -> None:
    """Persist a non-destructive deferred-queue entry in .sidecar/deferred-captures.json."""
    active_raw = raw_dir or RAW_DIR
    sidecar = _sidecar_for_raw(active_raw)
    os.makedirs(sidecar, exist_ok=True)
    if _is_reparse_or_link(sidecar):
        return
    queue_file = os.path.join(sidecar, "deferred-captures.json")
    lock_file = os.path.join(sidecar, "writer.lock")
    with review_lock(lock_file):
        data: dict[str, dict[str, object]] = {}
        if os.path.isfile(queue_file) and not _is_reparse_or_link(queue_file):
            try:
                with open(queue_file, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        fname = os.path.basename(filepath)
        try:
            st = os.stat(filepath)
            size_bytes = st.st_size
        except OSError:
            size_bytes = 0
        data[fname] = {
            "filename": fname,
            "stage": stage,
            "reason": reason,
            "size_bytes": size_bytes,
            "deferred_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        }
        atomic_write_bytes(queue_file, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


def _clear_deferred_capture(filename: str, *, raw_dir: str | None = None) -> None:
    active_raw = raw_dir or RAW_DIR
    sidecar = _sidecar_for_raw(active_raw)
    queue_file = os.path.join(sidecar, "deferred-captures.json")
    if not os.path.isfile(queue_file) or _is_reparse_or_link(queue_file):
        return
    lock_file = os.path.join(sidecar, "writer.lock")
    with review_lock(lock_file):
        try:
            with open(queue_file, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict) and filename in loaded:
            loaded.pop(filename, None)
            atomic_write_bytes(queue_file, json.dumps(loaded, indent=2, sort_keys=True).encode("utf-8"))


def can_auto_run_stage(ext: str, profile: HardwareCapabilityProfile) -> tuple[bool, str]:
    """Decide whether automatic background extraction may run on the current hardware profile."""
    if ext in DOC_EXTS:
        return True, "deterministic_document_extractor"
    if ext in AUDIO_EXTS:
        if not _has_module("faster_whisper"):
            return False, "missing_faster_whisper"
        if profile.total_ram_gib < 6.0:
            return False, f"insufficient_ram:{profile.total_ram_gib:.1f}GiB"
        return True, "whisper_ready"
    if ext in IMAGE_EXTS:
        if not _has_module("PIL"):
            return False, "missing_pillow_image_gate"
        if profile.tier_id in CONSTRAINED_LLM_TIERS:
            return False, f"constrained_tier:{profile.tier_id}"
        if not profile.ollama_reachable:
            return False, "ollama_unreachable"
        has_vision = any(
            any(tok in str(m).lower() for tok in ("llava", "vision", "vl", "moondream", "minicpm-v"))
            for m in profile.installed_ollama_models
        )
        if not has_vision:
            return False, "no_installed_vision_model"
        return True, "vision_ready"
    # Refinery LLM stage (.md / .txt / .intake)
    if profile.tier_id in CONSTRAINED_LLM_TIERS:
        return False, f"constrained_tier:{profile.tier_id}"
    if not profile.ollama_reachable:
        return False, "ollama_unreachable"
    if not profile.selected_installed_generation_model:
        return False, "no_installed_generation_model"
    return True, "refinery_ready"


def handle_new_file(filepath: str) -> str | list[str] | None:
    """Dispatch a newly arrived file to the right ingestion pipeline, honoring hardware tier limits."""
    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()

    if should_ignore_watchdog_path(filepath):
        return None
    if os.path.lexists(RAW_DIR) and _is_reparse_or_link(RAW_DIR):
        log.error("Refusing to process files under symlinked 0-raw directory.")
        return None
    if not os.path.isfile(filepath):
        return None

    if not wait_for_stable_file(filepath):
        log.warning(f"File did not become stable before timeout; leaving it in 0-raw/: {filename}")
        return None

    log.info(f"New file detected: {filename}")
    profile = get_daemon_hardware_profile()

    if ext in DOC_EXTS:
        log.info("→ Routing to deterministic document extractor...")
        try:
            import ingest_multimodal as im_mod

            im_mod.RAW_DIR = RAW_DIR
            im_mod.INBOX_DIR = os.path.join(RAW_DIR, "inbox")
            im_mod.SIDECAR_DIR = _sidecar_for_raw(RAW_DIR)
            out_name = im_mod.process_file(filepath)
            _clear_deferred_capture(filename, raw_dir=RAW_DIR)
            log.info("  Document extraction complete.")
            return out_name
        except Exception as exc:
            log.error(f"  Document extraction failed: {exc}")
            return None

    if ext in AUDIO_EXTS or ext in IMAGE_EXTS:
        allowed, reason = can_auto_run_stage(ext, profile)
        if not allowed:
            stage = "audio_transcription" if ext in AUDIO_EXTS else "image_vision"
            log.info(f"→ Deferring {stage} for {filename} ({reason}); keeping file queued safely in 0-raw/.")
            _record_deferred_capture(filepath, stage=stage, reason=reason, raw_dir=RAW_DIR)
            return None
        log.info("→ Routing to multimodal ingestion...")
        try:
            import ingest_multimodal as im_mod

            im_mod.RAW_DIR = RAW_DIR
            im_mod.INBOX_DIR = os.path.join(RAW_DIR, "inbox")
            im_mod.SIDECAR_DIR = _sidecar_for_raw(RAW_DIR)
            out_name = im_mod.process_file(filepath)
            _clear_deferred_capture(filename, raw_dir=RAW_DIR)
            log.info("  Multimodal ingestion complete.")
            return out_name
        except Exception as exc:
            log.error(f"  Multimodal ingestion failed: {exc}")
            return None

    if ext in TEXT_EXTS or is_supported_refinery_target(filepath):
        allowed, reason = can_auto_run_stage(".md", profile)
        if not allowed:
            log.info(f"→ Deferring automatic Refinery extraction for {filename} ({reason}); keeping capture in 0-raw/.")
            _record_deferred_capture(filepath, stage="refinery_llm", reason=reason, raw_dir=RAW_DIR)
            return None
        log.info("→ Routing to Refinery...")
        try:
            from ollama_refinery import process_single_file

            saved = process_single_file(filepath)
            _clear_deferred_capture(filename, raw_dir=RAW_DIR)
            log.info("  Refinery complete.")
            return saved
        except Exception as exc:
            log.error(f"  Refinery failed: {exc}")
            return None

    log.info(f"→ Unsupported extension '{ext}'. Ignoring.")
    return None


class WatchdogDispatcher:
    """Non-blocking FIFO queue + single background worker thread for 0-raw/ captures."""

    def __init__(self, *, auto_start: bool = False) -> None:
        self.queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        self._deferred_signatures: dict[str, tuple[int, int, str]] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="second-brain-watchdog-worker",
            daemon=True,
        )
        self._started = False
        if auto_start:
            self.start()

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                self._worker.start()

    def _file_sig(self, filepath: str) -> tuple[int, int] | None:
        try:
            st = os.stat(filepath)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def enqueue(self, filepath: str) -> bool:
        if self._stop_event.is_set() or should_ignore_watchdog_path(filepath, raw_dir=RAW_DIR):
            return False
        abs_path = os.path.abspath(filepath)
        norm = os.path.normcase(abs_path)
        sig = self._file_sig(abs_path)
        profile = get_daemon_hardware_profile()
        cap_sig = f"{profile.tier_id}:{int(profile.ollama_reachable)}:{profile.selected_installed_generation_model or ''}"
        with self._lock:
            if norm in self._pending:
                return False
            if sig is not None and self._deferred_signatures.get(norm) == (sig[0], sig[1], cap_sig):
                return False
            self._pending.add(norm)
        self.queue.put(abs_path)
        return True

    def _worker_loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                self.queue.task_done()
                break
            norm = os.path.normcase(os.path.abspath(item))
            try:
                if not self._stop_event.is_set():
                    before_sig = self._file_sig(item)
                    result = handle_new_file(item)
                    if result is None and os.path.isfile(item) and before_sig is not None:
                        prof = get_daemon_hardware_profile()
                        cap_sig = f"{prof.tier_id}:{int(prof.ollama_reachable)}:{prof.selected_installed_generation_model or ''}"
                        with self._lock:
                            self._deferred_signatures[norm] = (before_sig[0], before_sig[1], cap_sig)
                    else:
                        with self._lock:
                            self._deferred_signatures.pop(norm, None)
                    if isinstance(result, str) and result and not result.startswith("inbox/"):
                        generated_path = os.path.join(RAW_DIR, result)
                        if os.path.isfile(generated_path) and not _is_reparse_or_link(generated_path):
                            self.enqueue(generated_path)
            except Exception as exc:
                log.error(f"Watchdog worker failed on {os.path.basename(item)}: {exc}")
            finally:
                with self._lock:
                    self._pending.discard(norm)
                self.queue.task_done()

    def drain(self) -> None:
        self.queue.join()

    def shutdown(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self.queue.put(None)
        if self._started and self._worker.is_alive():
            self._worker.join(timeout=timeout)


def scan_unprocessed_raw_files(dispatcher: WatchdogDispatcher, raw_dir: str | None = None) -> list[str]:
    """Scan 0-raw/ on daemon startup and enqueue existing unprocessed captures."""
    target_dir = raw_dir or RAW_DIR
    if not os.path.isdir(target_dir):
        return []
    if _is_reparse_or_link(target_dir):
        log.error(f"Refusing startup scan on symlinked 0-raw directory: {target_dir}")
        return []

    enqueued: list[str] = []
    for entry in sorted(os.listdir(target_dir)):
        filepath = os.path.join(target_dir, entry)
        if should_ignore_watchdog_path(filepath, raw_dir=target_dir) or not os.path.isfile(filepath):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext == ".intake":
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                    if intake_status(handle.read()) != "approved":
                        continue
            except OSError:
                continue
        if (
            ext in AUDIO_EXTS
            or ext in IMAGE_EXTS
            or ext in DOC_EXTS
            or ext in TEXT_EXTS
            or is_supported_refinery_target(filepath)
        ):
            if dispatcher.enqueue(filepath):
                enqueued.append(filepath)
    if enqueued:
        log.info(f"Startup scan enqueued {len(enqueued)} unprocessed file(s) from 0-raw/.")
    return enqueued


# ─── Model-Free Debounced Watchers for notes/ & 2-atoms/ ─────────────────────

def sync_notes_index_for_vault(*, brain_dir: str | None = None, notes_dir: str | None = None) -> dict[str, object]:
    """Incrementally sync notes.sqlite3 FTS5 for brain/notes/ (100% model-free)."""
    from dataclasses import replace as _dc_replace
    from pathlib import Path as _Path
    from notes_store import NotePaths, ensure_notes_index

    resolved_brain = os.path.abspath(brain_dir or os.path.dirname(os.path.abspath(notes_dir or NOTES_DIR)))
    npaths = NotePaths.for_brain(resolved_brain)
    if notes_dir:
        npaths = _dc_replace(npaths, notes_dir=_Path(os.path.abspath(notes_dir)))
    if os.path.lexists(npaths.notes_dir) and _is_reparse_or_link(str(npaths.notes_dir)):
        raise PermissionError(f"Refusing to sync symlinked notes directory: {npaths.notes_dir}")
    os.makedirs(npaths.notes_dir, exist_ok=True)
    indexed = ensure_notes_index(npaths)
    try:
        from agent_runtime import learn_from_notes

        learn_from_notes(brain_dir=resolved_brain)
    except Exception:
        pass
    return indexed


def sync_atoms_and_detect_stale_anchors(
    *,
    brain_dir: str | None = None,
    atoms_dir: str | None = None,
    lexical_index_path: str | None = None,
    chroma_path: str | None = None,
    sync_vectors: bool = True,
) -> dict[str, object]:
    """Incrementally sync lexical.sqlite3 FTS5, O(1) Chroma vectors (when active), and stale-anchor detection on 2-atoms/."""
    from audit_sources import audit_sources
    from index_manifest import index_manifest_is_current, load_index_manifest
    from lexical_store import sync_lexical_index

    resolved_atoms = os.path.abspath(
        atoms_dir or (os.path.join(brain_dir, "2-atoms") if brain_dir else ATOMS_DIR)
    )
    resolved_brain = os.path.abspath(brain_dir or os.path.dirname(resolved_atoms))
    sidecar = os.path.join(resolved_brain, ".sidecar")
    os.makedirs(sidecar, exist_ok=True)
    os.makedirs(resolved_atoms, exist_ok=True)
    if _is_reparse_or_link(resolved_atoms) or _is_reparse_or_link(sidecar):
        raise PermissionError("Refusing to sync symlinked 2-atoms or .sidecar directory.")
    resolved_lex = os.path.abspath(lexical_index_path or os.path.join(sidecar, "lexical.sqlite3"))
    resolved_chroma = os.path.abspath(chroma_path or os.path.join(resolved_brain, ".chroma"))
    lock_file = os.path.join(sidecar, "writer.lock")
    vector_sync: dict[str, object] = {"status": "not_active"}
    with review_lock(lock_file):
        lex_stats = sync_lexical_index(resolved_atoms, resolved_lex)
        if (
            sync_vectors
            and os.path.isdir(resolved_chroma)
            and not _is_reparse_or_link(resolved_chroma)
        ):
            manifest = load_index_manifest(resolved_chroma)
            if isinstance(manifest, dict) and manifest.get("embedding_model") and isinstance(manifest.get("embedding_dimensions"), int):
                if index_manifest_is_current(manifest, resolved_atoms):
                    vector_sync = {"status": "already_current", "generation_id": manifest.get("generation_id")}
                else:
                    try:
                        import rebuild_vectors as _rv_mod

                        vec_res = _rv_mod.upsert_changed_vectors(
                            atoms_dir=resolved_atoms,
                            store_path=resolved_chroma,
                            collection_name="atoms",
                            model=str(manifest["embedding_model"]),
                            pre_verified_current=False,
                            _writer_lock_held=True,
                        )
                        vector_sync = {
                            "status": "synced",
                            "embedded": vec_res.get("embedded", 0),
                            "deleted": vec_res.get("deleted", 0),
                            "generation_id": vec_res.get("generation_id"),
                        }
                    except Exception as exc:
                        log.warning(f"Incremental Chroma vector sync skipped/degraded: {exc}")
                        vector_sync = {"status": "degraded", "reason": str(exc)[:300]}

    reports = audit_sources(atoms_dir=resolved_atoms, brain_dir=resolved_brain, demote_stale=False)
    stale = [
        {
            "atom_id": rep["atom_id"],
            "anchor_status": rep["anchor_status"],
            "source_status": rep["source_status"],
            "action": rep["action"],
        }
        for rep in reports
        if rep.get("action") == "review_required"
        or rep.get("anchor_status") in {"stale_or_unverifiable", "unreadable"}
    ]
    if stale:
        log.warning(f"Stale or unverifiable source anchors detected on {len(stale)} atom(s): {[s['atom_id'] for s in stale]}")
    stale_report_path = os.path.join(sidecar, "stale-anchors.json")
    with review_lock(lock_file):
        atomic_write_bytes(
            stale_report_path,
            json.dumps(
                {
                    "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                    "total_atoms": len(reports),
                    "stale_count": len(stale),
                    "stale_atoms": stale,
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8"),
        )
    return {
        "lexical_sync": lex_stats,
        "vector_sync": vector_sync,
        "total_atoms": len(reports),
        "stale_count": len(stale),
        "stale_atoms": stale,
    }


def sync_notes_and_atoms_once(
    *,
    brain_dir: str | None = None,
    notes_dir: str | None = None,
    atoms_dir: str | None = None,
    lexical_index_path: str | None = None,
) -> dict[str, object]:
    """Run one model-free FTS5 sync + stale-anchor check across notes/ and 2-atoms/."""
    notes_res = sync_notes_index_for_vault(brain_dir=brain_dir, notes_dir=notes_dir)
    atoms_res = sync_atoms_and_detect_stale_anchors(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        lexical_index_path=lexical_index_path,
    )
    return {"notes": notes_res, "atoms": atoms_res}


class DebouncedMarkdownSyncHandler(FileSystemEventHandler):
    """Lightweight debounced watcher coalescing rapid editor saves in notes/ or 2-atoms/."""

    def __init__(self, watched_dir: str, callback, *, debounce_seconds: float = 0.35) -> None:
        super().__init__()
        self.watched_dir = os.path.abspath(watched_dir)
        self.callback = callback
        self.debounce_seconds = max(0.02, float(debounce_seconds))
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._stopped = False

    def _schedule(self, path: str) -> None:
        if _should_ignore_markdown_watch_path(path, self.watched_dir):
            return
        with self._lock:
            if self._stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(self.debounce_seconds, self._fire)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
            if self._stopped:
                return
        try:
            self.callback()
        except Exception as exc:
            log.error(f"Debounced index sync failed for {self.watched_dir}: {exc}")

    def flush(self) -> None:
        with self._lock:
            pending = self._timer
            self._timer = None
        if pending is not None:
            pending.cancel()
            self._fire()

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def on_created(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._schedule(event.src_path)
            self._schedule(event.dest_path)


# ─── Raw Inbox Event Handler ─────────────────────────────────────────────────

class InboxHandler(FileSystemEventHandler):

    def __init__(self, dispatcher: WatchdogDispatcher | None = None) -> None:
        super().__init__()
        self.dispatcher = dispatcher if dispatcher is not None else WatchdogDispatcher(auto_start=True)

    def on_created(self, event):
        if not event.is_directory:
            self.dispatcher.enqueue(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        try:
            destination_parent = os.path.realpath(os.path.dirname(os.path.abspath(event.dest_path)))
            raw_real = os.path.realpath(os.path.abspath(RAW_DIR))
        except OSError:
            return
        if os.path.normcase(destination_parent) == os.path.normcase(raw_real):
            self.dispatcher.enqueue(event.dest_path)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_daemon(*, once: bool = False, auto_sync: bool = False, sync_interval: int = 300) -> int:
    ensure_dirs()
    for watched in (RAW_DIR, NOTES_DIR, ATOMS_DIR):
        if os.path.lexists(watched) and _is_reparse_or_link(watched):
            log.error(f"Refusing to watch symlinked vault directory: {watched}")
            return 1

    sync_notes_and_atoms_once(
        brain_dir=BRAIN_DIR,
        notes_dir=NOTES_DIR,
        atoms_dir=ATOMS_DIR,
        lexical_index_path=LEXICAL_INDEX_PATH,
    )

    dispatcher = WatchdogDispatcher(auto_start=True)
    if once:
        scan_unprocessed_raw_files(dispatcher, RAW_DIR)
        dispatcher.drain()
        dispatcher.shutdown()
        sync_notes_and_atoms_once(
            brain_dir=BRAIN_DIR,
            notes_dir=NOTES_DIR,
            atoms_dir=ATOMS_DIR,
            lexical_index_path=LEXICAL_INDEX_PATH,
        )
        if auto_sync:
            from vault_sync import run_auto_sync_cycle

            run_auto_sync_cycle(brain_dir=BRAIN_DIR)
        return 0

    if Observer is None:
        raise RuntimeError(
            "Continuous filesystem watching requires 'watchdog'; install 'second-brain[watchdog]' (or run with '--once')."
        )

    sync_sched = None
    try:
        from vault_sync import AutoSyncScheduler, get_auto_sync_status

        auto_st = get_auto_sync_status(brain_dir=BRAIN_DIR)
        if auto_sync or bool(auto_st.get("enabled")):
            iv = int(sync_interval or auto_st.get("interval_seconds") or 300)
            sync_sched = AutoSyncScheduler(BRAIN_DIR, interval_seconds=iv)
            sync_sched.start()
            log.info(f"  Background E2EE auto-sync active (interval={iv}s)")
    except Exception as exc:
        log.warning(f"  Auto-sync scheduler not started: {exc}")

    profile = get_daemon_hardware_profile()
    log.info("=" * 50)
    log.info("  Second Brain Watchdog Daemon — ACTIVE")
    log.info(f"  Hardware tier: {profile.tier_label} (Ollama reachable={profile.ollama_reachable})")
    log.info(f"  Watching 0-raw/:   {RAW_DIR}")
    log.info(f"  Watching notes/:   {NOTES_DIR}")
    log.info(f"  Watching 2-atoms/: {ATOMS_DIR}")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 50)

    raw_handler = InboxHandler(dispatcher=dispatcher)
    notes_handler = DebouncedMarkdownSyncHandler(
        NOTES_DIR,
        lambda: sync_notes_index_for_vault(brain_dir=BRAIN_DIR, notes_dir=NOTES_DIR),
    )
    atoms_handler = DebouncedMarkdownSyncHandler(
        ATOMS_DIR,
        lambda: sync_atoms_and_detect_stale_anchors(
            brain_dir=BRAIN_DIR,
            atoms_dir=ATOMS_DIR,
            lexical_index_path=LEXICAL_INDEX_PATH,
        ),
    )
    observer = Observer()
    observer.schedule(raw_handler, RAW_DIR, recursive=False)
    observer.schedule(notes_handler, NOTES_DIR, recursive=False)
    observer.schedule(atoms_handler, ATOMS_DIR, recursive=False)
    observer.start()

    try:
        scan_unprocessed_raw_files(dispatcher, RAW_DIR)
        agent_tick_at = 0.0
        while True:
            time.sleep(1.0)
            now_m = time.monotonic()
            if now_m < agent_tick_at:
                continue
            agent_tick_at = now_m + 30.0
            try:
                from agent_runtime import run_scheduled_goals

                run_scheduled_goals()
            except Exception as exc:
                log.warning(f"Agent schedule tick failed: {exc}")
    except KeyboardInterrupt:
        log.info("Shutting down watchdog...")
    finally:
        if sync_sched is not None:
            sync_sched.stop(timeout=5.0)
        notes_handler.stop()
        atoms_handler.stop()
        observer.stop()
        observer.join(timeout=10.0)
        dispatcher.shutdown(timeout=10.0)
        log.info("Watchdog stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Second Brain continuous watchdog daemon.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Sync notes/atoms FTS + stale anchors and process unhandled 0-raw/ files once, then exit cleanly.",
    )
    parser.add_argument(
        "--auto-sync",
        action="store_true",
        help="Run background E2EE vault auto-sync loop alongside file watching.",
    )
    parser.add_argument(
        "--sync-interval",
        type=int,
        default=300,
        help="Background E2EE auto-sync interval in seconds (default 300).",
    )
    args = parser.parse_args(argv)
    return run_daemon(once=args.once, auto_sync=args.auto_sync, sync_interval=args.sync_interval)


if __name__ == "__main__":
    raise SystemExit(main())
