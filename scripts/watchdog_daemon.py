# /// script
# requires-python = ">=3.11,<3.12"
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
Monitors 0-raw/ around the clock. The instant a file lands in the folder,
it fires the appropriate ingestion pipeline without waiting for a batch run.

  .md / .txt files  → Immediately routed to the Refinery (atom creation)
  Audio files       → Multimodal ingestion (Whisper transcribe) → Refinery
  Image files       → Multimodal ingestion (LLaVA describe)    → Refinery

Run this once and leave it running:
    uv run scripts/watchdog_daemon.py
"""

import argparse
import logging
import os
import queue
import sys
import threading
import time

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW_DIR, AUDIO_EXTS, IMAGE_EXTS, TEXT_EXTS, _is_reparse_or_link, ensure_dirs
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


def handle_new_file(filepath: str) -> str | list[str] | None:
    """Dispatch a newly arrived file to the right ingestion pipeline."""
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
        log.warning(f"File did not become stable before timeout; leaving it in the inbox: {filename}")
        return None

    log.info(f"New file detected: {filename}")

    if ext in AUDIO_EXTS or ext in IMAGE_EXTS:
        log.info("→ Routing to multimodal ingestion...")
        try:
            from ingest_multimodal import process_file
            out_name = process_file(filepath)
            log.info("  Multimodal ingestion complete.")
            return out_name
        except Exception as e:
            log.error(f"  Multimodal ingestion failed: {e}")
            return None

    elif ext in TEXT_EXTS or is_supported_refinery_target(filepath):
        log.info("→ Routing to Refinery...")
        try:
            from ollama_refinery import process_single_file
            saved = process_single_file(filepath)
            log.info("  Refinery complete.")
            return saved
        except Exception as e:
            log.error(f"  Refinery failed: {e}")
            return None

    else:
        log.info(f"→ Unsupported extension '{ext}'. Ignoring.")
        return None


class WatchdogDispatcher:
    """Non-blocking FIFO queue + single background worker thread for 0-raw/ captures."""

    def __init__(self, *, auto_start: bool = False) -> None:
        self.queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
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

    def enqueue(self, filepath: str) -> bool:
        if self._stop_event.is_set() or should_ignore_watchdog_path(filepath):
            return False
        abs_path = os.path.abspath(filepath)
        norm = os.path.normcase(abs_path)
        with self._lock:
            if norm in self._pending:
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
                    result = handle_new_file(item)
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
        if should_ignore_watchdog_path(filepath) or not os.path.isfile(filepath):
            continue
        ext = os.path.splitext(entry)[1].lower()
        if ext == ".intake":
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as handle:
                    if intake_status(handle.read()) != "approved":
                        continue
            except OSError:
                continue
        if ext in AUDIO_EXTS or ext in IMAGE_EXTS or ext in TEXT_EXTS or is_supported_refinery_target(filepath):
            if dispatcher.enqueue(filepath):
                enqueued.append(filepath)
    if enqueued:
        log.info(f"Startup scan enqueued {len(enqueued)} unprocessed file(s) from 0-raw/.")
    return enqueued


# ─── File System Event Handler ────────────────────────────────────────────────

class InboxHandler(FileSystemEventHandler):

    def __init__(self, dispatcher: WatchdogDispatcher | None = None) -> None:
        super().__init__()
        self.dispatcher = dispatcher if dispatcher is not None else WatchdogDispatcher(auto_start=True)

    def on_created(self, event):
        if not event.is_directory:
            self.dispatcher.enqueue(event.src_path)

    def on_moved(self, event):
        # A file dragged into the folder or atomically renamed shows as a 'moved' event
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

def run_daemon(*, once: bool = False) -> int:
    ensure_dirs()
    if os.path.lexists(RAW_DIR) and _is_reparse_or_link(RAW_DIR):
        log.error(f"Refusing to watch symlinked 0-raw directory: {RAW_DIR}")
        return 1

    dispatcher = WatchdogDispatcher(auto_start=True)
    if once:
        scan_unprocessed_raw_files(dispatcher, RAW_DIR)
        dispatcher.drain()
        dispatcher.shutdown()
        return 0

    log.info("=" * 50)
    log.info("  Second Brain Watchdog Daemon — ACTIVE")
    log.info(f"  Watching: {RAW_DIR}")
    log.info("  Drop any file into 0-raw/ to process it instantly.")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 50)

    handler = InboxHandler(dispatcher=dispatcher)
    observer = Observer()
    observer.schedule(handler, RAW_DIR, recursive=False)
    observer.start()

    try:
        scan_unprocessed_raw_files(dispatcher, RAW_DIR)
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        log.info("Shutting down watchdog...")
    finally:
        observer.stop()
        observer.join(timeout=10.0)
        dispatcher.shutdown(timeout=10.0)
        log.info("Watchdog stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Second Brain continuous 0-raw/ watchdog daemon.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Scan and process existing unhandled files in 0-raw/ once, then exit cleanly.",
    )
    args = parser.parse_args(argv)
    return run_daemon(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
