# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Server-sent events for product chat.

The desktop app, dashboard, and ``POST /v1/chat/stream`` all consume the
same ``chat_events`` token stream. This module only writes those events
onto an HTTP response.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable

from scripts.api_owner_auth import MAX_SSE_SECONDS


def stream_sse(handler, events: Iterable[dict], *, on_open: Callable[[], None] | None = None) -> None:
    """Write events as text/event-stream; connection closes at end (no chunked encoding needed)."""
    it = iter(events)
    sentinel = object()
    first = next(it, sentinel)
    if on_open is not None:
        on_open()

    handler.close_connection = True
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Connection", "close")
    handler.end_headers()
    deadline = time.monotonic() + MAX_SSE_SECONDS

    def _emit(event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        handler.wfile.write(f"event: {event.get('type', 'message')}\ndata: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()

    try:
        if first is not sentinel:
            _emit(first)
        for event in it:
            _emit(event)
            if time.monotonic() > deadline:
                handler.wfile.write(b'event: error\ndata: {"type":"error","error":"stream time limit reached"}\n\n')
                handler.wfile.flush()
                break
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
        return
    except Exception as exc:
        try:
            _emit({"type": "error", "error": f"Stream aborted: {exc.__class__.__name__}"})
            _emit({"type": "done", "total_ms": 0.0, "elapsed_ms": 0.0})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            return
