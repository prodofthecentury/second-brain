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

import os
import sys
import time
import logging

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileMovedEvent

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import RAW_DIR, AUDIO_EXTS, IMAGE_EXTS, TEXT_EXTS, ensure_dirs
from stable_file import wait_for_stable_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchdog")

# ─── Skip These Files ─────────────────────────────────────────────────────────
SKIP_NAMES = {"reading-list.md", "README.md"}


def handle_new_file(filepath: str):
    """Dispatch a newly arrived file to the right ingestion pipeline."""
    filename = os.path.basename(filepath)
    ext      = os.path.splitext(filename)[1].lower()

    if filename in SKIP_NAMES:
        return
    if not os.path.isfile(filepath):
        return

    if not wait_for_stable_file(filepath):
        log.warning(f"File did not become stable before timeout; leaving it in the inbox: {filename}")
        return

    log.info(f"New file detected: {filename}")

    if ext in AUDIO_EXTS or ext in IMAGE_EXTS:
        log.info("→ Routing to multimodal ingestion...")
        try:
            from ingest_multimodal import process_file
            process_file(filepath)
            log.info("  Multimodal ingestion complete.")
        except Exception as e:
            log.error(f"  Multimodal ingestion failed: {e}")

    elif ext in TEXT_EXTS:
        log.info("→ Routing to Refinery...")
        try:
            from ollama_refinery import process_single_file
            process_single_file(filepath)
            log.info("  Refinery complete.")
        except Exception as e:
            log.error(f"  Refinery failed: {e}")

    else:
        log.info(f"→ Unsupported extension '{ext}'. Ignoring.")


# ─── File System Event Handler ────────────────────────────────────────────────

class InboxHandler(FileSystemEventHandler):

    def on_created(self, event):
        if not event.is_directory:
            handle_new_file(event.src_path)

    def on_moved(self, event):
        # A file dragged into the folder shows as a 'moved' event on some OSes
        destination_parent = os.path.dirname(os.path.abspath(event.dest_path))
        if not event.is_directory and destination_parent == os.path.abspath(RAW_DIR):
            handle_new_file(event.dest_path)


# ─── Main ─────────────────────────────────────────────────────────────────────

def run_daemon():
    ensure_dirs()

    log.info("=" * 50)
    log.info("  Second Brain Watchdog Daemon — ACTIVE")
    log.info(f"  Watching: {RAW_DIR}")
    log.info("  Drop any file into 0-raw/ to process it instantly.")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 50)

    handler  = InboxHandler()
    observer = Observer()
    observer.schedule(handler, RAW_DIR, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("Shutting down watchdog...")
        observer.stop()

    observer.join()
    log.info("Watchdog stopped.")


if __name__ == "__main__":
    run_daemon()
