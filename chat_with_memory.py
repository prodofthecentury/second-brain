# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "ollama",
#     "rich",
# ]
# ///

import ollama
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from scripts.config import (
    GEN_MODEL,
)
from scripts.recall import (
    MemoryRecallUnavailable,
    RecallOptions,
    format_context,
    search_memory as recall_search_memory,
)

console = Console()

def search_memory(query):
    try:
        direct, neighbors = recall_search_memory(query, RecallOptions(n_results=3, follow_links=True))
    except MemoryRecallUnavailable as exc:
        console.print(f"[bold yellow]Memory recall unavailable: {exc}[/bold yellow]")
        return ""
    return format_context(direct + neighbors)

def chat_loop():
    console.print(Panel.fit("[bold cyan]Ollama Memory Interface (Graph RAG)[/bold cyan]\nType 'exit' to quit.", border_style="cyan"))
    
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
            memory_context = search_memory(user_input)
            
        if memory_context:
            console.print("[dim green]✓ Relevant memories retrieved.[/dim green]")
        else:
            console.print("[dim yellow]⚠ No relevant memories found.[/dim yellow]")
            
        system_prompt = f"""
You are a brilliant research assistant with a perfect memory, powered by the user's Second Brain vault.
Answer the user's question. Use the context provided below from the vault.
If the context is irrelevant to the question, you can answer from your general knowledge, but always prioritize the vault context.
If you use the vault context, cite the filename.

VAULT CONTEXT:
{memory_context}
"""
        
        # Inject context invisibly for this turn
        turn_messages = [{"role": "system", "content": system_prompt}] + messages + [{"role": "user", "content": user_input}]
        
        console.print("\n[bold blue]Ollama>[/bold blue] ", end="")
        
        full_response = ""
        try:
            response_stream = ollama.chat(model=GEN_MODEL, messages=turn_messages, stream=True)
            for chunk in response_stream:
                content = chunk["message"]["content"]
                full_response += content
                print(content, end="", flush=True)
            print()
        except Exception as e:
            console.print(f"\n[bold red]Error connecting to Ollama: {e}[/bold red]")
            continue
            
        # Append to history
        messages.append({"role": "user", "content": user_input})
        messages.append({"role": "assistant", "content": full_response})

if __name__ == "__main__":
    chat_loop()
