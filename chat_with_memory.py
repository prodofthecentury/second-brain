# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
#     "rich",
# ]
# ///

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

import ollama
from rich.console import Console
from rich.markup import escape as rich_escape
from rich.panel import Panel

from scripts.config import ATOMS_DIR, GEN_MODEL
from scripts.answer_policy import ANSWER_DRAFT_SCHEMA, AnswerRequest, answer_from_memory, build_grounding_prompt, validate_standard_answer
from scripts.recall import (
    MemoryRecallUnavailable,
    RecallOptions,
    consume_last_degradation_warning,
    search_memory as recall_search_memory,
)

console = Console()


def search_memory(
    query: str,
    *,
    projects: tuple[str, ...] = (),
    retrieval_mode: str = "auto",
    allow_cross_project: bool = False,
):
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
        console.print(f"[bold yellow]Memory recall unavailable: {exc}[/bold yellow]")
        return []
    degradation = consume_last_degradation_warning()
    if degradation:
        console.print(Panel(f"[bold yellow]{degradation}[/bold yellow]", border_style="yellow"))
    return direct + neighbors


def chat_loop(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Interactive Grounded Memory Chat")
    parser.add_argument("--project", action="append", default=[], help="Filter recall by project:<id> tag.")
    parser.add_argument(
        "--mode",
        choices=("auto", "rrf", "lexical", "dense"),
        default="auto",
        help="Retrieval ranking mode.",
    )
    parser.add_argument(
        "--allow-cross-project",
        action="store_true",
        help="Allow linked neighbor references across project boundaries.",
    )
    parser.add_argument(
        "--answer-mode",
        choices=("standard", "preference"),
        default="standard",
        help="Epistemic answer contract mode ('standard' empirical facts or 'preference' owner preferences).",
    )
    args = parser.parse_args(argv)
    projects = tuple(p.strip() for p in args.project if p.strip())

    scope_label = f" | projects={','.join(projects)}" if projects else ""
    console.print(
        Panel.fit(
            f"[bold cyan]Ollama Memory Interface (Graph RAG)[/bold cyan]\n"
            f"mode={args.mode} | contract={args.answer_mode}{scope_label}\nType 'exit' to quit.",
            border_style="cyan",
        )
    )
    
    messages = []
    
    while True:
        try:
            user_input = console.input("\n[bold green]You>[/bold green] ")
        except (KeyboardInterrupt, EOFError):
            break
            
        if user_input.lower() in ['exit', 'quit']:
            break
        if not user_input.strip():
            continue
            
        with console.status("[bold dark_gray]Searching semantic memory...[/bold dark_gray]"):
            retrieved_atoms = search_memory(
                user_input,
                projects=projects,
                retrieval_mode=args.mode,
                allow_cross_project=args.allow_cross_project,
            )
            
        if retrieved_atoms:
            explanations = "; ".join(
                f"{item.filename}[{','.join(item.match_reasons)}]" for item in retrieved_atoms
            )
            console.print(f"[dim green]✓ Relevant memories retrieved: {explanations}[/dim green]")
        else:
            console.print("[dim yellow]⚠ No relevant memories found.[/dim yellow]")

        if not retrieved_atoms:
            full_response = answer_from_memory("", lambda: "")
            console.print(f"\n[bold blue]Ollama>[/bold blue]")
            console.print(full_response, markup=False)
            messages.append({"role": "user", "content": user_input})
            messages.append({"role": "assistant", "content": full_response})
            continue
            
        system_prompt = build_grounding_prompt(
            AnswerRequest(query=user_input, mode=args.answer_mode, result_limit=len(retrieved_atoms)),
            retrieved_atoms,
        )
        
        # Inject context invisibly for this turn
        turn_messages = [{"role": "system", "content": system_prompt}] + messages + [{"role": "user", "content": user_input}]
        
        full_response = ""
        try:
            response_stream = ollama.chat(
                model=GEN_MODEL, messages=turn_messages, format=ANSWER_DRAFT_SCHEMA, stream=True
            )
            for chunk in response_stream:
                content = chunk["message"]["content"]
                full_response += content
            answer = validate_standard_answer(
                full_response, retrieved_atoms, atoms_dir=ATOMS_DIR, mode=args.answer_mode
            )
            full_response = answer.text
            console.print(f"\n[bold blue]Ollama>[/bold blue] {rich_escape(full_response)}")
        except Exception as e:
            console.print(f"\n[bold red]Error connecting to Ollama: {e}[/bold red]")
            continue
            
        # Append to history
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": full_response})

if __name__ == "__main__":
    chat_loop()
