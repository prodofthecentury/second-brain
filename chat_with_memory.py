# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#     "ollama",
#     "rich",
# ]
# ///
"""Canonical CLI shim delegating interactive grounded memory chat to `scripts/streaming_chat.py`."""

from __future__ import annotations

import argparse
import os
import sys

import ollama
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel

from scripts.config import ATOMS_DIR, BRAIN_DIR, GEN_MODEL
from scripts.hardware_profile import loopback_ollama_base_url
from scripts.host_adapters import HostConsentError
from scripts.recall import (
    MemoryRecallUnavailable,
    RecallOptions,
    consume_last_degradation_warning,
    search_memory as recall_search_memory,
)
from scripts import streaming_chat

console = Console()


def _stream_local_ollama_chat(*, endpoint_url: str, model: str, messages: list[dict], schema: dict):
    """Backward-compatible helper retained for loopback Ollama streaming callers."""
    safe_base = loopback_ollama_base_url(endpoint_url)
    if not safe_base:
        raise HostConsentError(
            f"403 Forbidden: local Ollama streaming requires a loopback endpoint (http://127.0.0.1 or http://[::1]); got {endpoint_url!r}."
        )
    chat_attr = getattr(ollama, "chat", None)
    client_attr = getattr(ollama, "Client", None)
    if (
        chat_attr is not None
        and not getattr(chat_attr, "__module__", "ollama").startswith("ollama")
        and getattr(client_attr, "__module__", "ollama").startswith("ollama")
    ):
        return chat_attr(
            model=model,
            messages=messages,
            format=schema,
            stream=True,
        )
    if getattr(client_attr, "__module__", "ollama").startswith("ollama"):
        client = ollama.Client(host=safe_base, timeout=120.0, follow_redirects=False, trust_env=False)
    else:
        client = ollama.Client(host=safe_base)
    return client.chat(
        model=model,
        messages=messages,
        format=schema,
        stream=True,
    )


def search_memory(
    query: str,
    *,
    projects: tuple[str, ...] = (),
    retrieval_mode: str = "auto",
    allow_cross_project: bool = False,
):
    """Backward-compatible recall helper delegating to `scripts/recall.search_memory`."""
    consume_last_degradation_warning()
    try:
        direct, neighbors = recall_search_memory(
            query,
            RecallOptions(
                n_results=3,
                follow_links=True,
                projects=projects,
                allow_cross_project=allow_cross_project,
                retrieval_mode=retrieval_mode,
            ),
        )
    except MemoryRecallUnavailable as exc:
        console.print(f"[bold yellow]Memory recall unavailable: {rich_escape(str(exc))}[/bold yellow]")
        return []
    degradation = consume_last_degradation_warning()
    if degradation:
        console.print(Panel(f"[bold yellow]{rich_escape(degradation)}[/bold yellow]", border_style="yellow"))
    return direct + neighbors


def _translate_legacy_argv(argv: list[str] | None = None) -> list[str]:
    parser = argparse.ArgumentParser(description="Interactive Grounded Memory Chat")
    parser.add_argument("query", nargs="?", default="", help="Optional single question; omit for interactive REPL.")
    parser.add_argument("--project", action="append", default=[], help="Filter recall by project:<id> tag.")
    parser.add_argument(
        "--mode",
        choices=("auto", "rrf", "lexical", "dense"),
        default="auto",
        help="Retrieval ranking mode.",
    )
    parser.add_argument("--allow-cross-project", action="store_true", help="Allow cross-project wikilinks.")
    parser.add_argument(
        "--answer-mode",
        choices=("standard", "preference", "research"),
        default="standard",
        help="Epistemic answer contract mode.",
    )
    parser.add_argument("--host-id", default="ollama_local", help="Configured AI host profile ID.")
    parser.add_argument("--model", default=None, help="Explicit model override for the selected host.")
    parser.add_argument("--auto-model", action="store_true", help="Auto-select best installed local model.")
    parser.add_argument("--pin", action="store_true", help="Automatically pin grounded answers into working notes.")
    args = parser.parse_args(argv)

    chat_mode = "research" if args.answer_mode == "research" else "verified"
    forwarded: list[str] = [
        "--mode",
        chat_mode,
        "--retrieval-mode",
        args.mode,
        "--view",
        args.answer_mode,
        "--host-id",
        args.host_id,
    ]
    if not (args.query and str(args.query).strip()):
        forwarded.append("--interactive")
    else:
        forwarded.append(str(args.query))
    for proj in args.project:
        if str(proj or "").strip():
            forwarded.extend(["--project", str(proj).strip()])
    if args.allow_cross_project:
        forwarded.append("--allow-cross-project")
    if args.model:
        forwarded.extend(["--model", str(args.model)])
    if args.auto_model:
        forwarded.append("--auto-model")
    if args.pin:
        forwarded.append("--pin")
    return forwarded


def chat_loop(argv: list[str] | None = None) -> int:
    """Delegate legacy chat CLI options directly to `scripts.streaming_chat.main`."""
    return streaming_chat.main(_translate_legacy_argv(argv))


def main(argv: list[str] | None = None) -> int:
    return chat_loop(argv)


if __name__ == "__main__":
    raise SystemExit(main())
