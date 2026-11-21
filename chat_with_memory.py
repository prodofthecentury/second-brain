# /// script
# requires-python = ">=3.11,<3.14"
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

from scripts.config import ATOMS_DIR, BRAIN_DIR, GEN_MODEL
from scripts.answer_policy import (
    ANSWER_DRAFT_SCHEMA,
    AnswerRequest,
    NO_APPROVED_MEMORY_RESPONSE,
    answer_from_memory,
    build_grounding_prompt,
    collect_unverified_leads,
    format_unverified_leads_section,
    validate_standard_answer,
)
from scripts.cognitive_core import (
    peek_lexical_candidate_atom_ids,
    record_grounded_answer_telemetry,
    record_retrieval_telemetry,
)
from scripts.hardware_profile import detect_hardware_profile, loopback_ollama_base_url
from scripts.host_adapters import (
    HostConsentError,
    _audit_outbound_call,
    _redact_outbound_with_map,
    _rehydrate_draft_claims,
    filter_atoms_for_host,
    generate_grounded_answer_via_host,
    load_host_policies,
    validate_endpoint_network_boundary,
    validate_model_identifier,
)
from scripts.recall import (
    MemoryRecallUnavailable,
    RecallOptions,
    consume_last_degradation_warning,
    search_memory as recall_search_memory,
)

console = Console()


def _stream_local_ollama_chat(*, endpoint_url: str, model: str, messages: list[dict], schema: dict):
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
        choices=("standard", "preference", "research"),
        default="standard",
        help="Epistemic answer contract mode ('standard' empirical facts, 'preference' owner preferences, or 'research' with unverified leads).",
    )
    parser.add_argument(
        "--host-id",
        default="ollama_local",
        help="Configured AI host profile ID (default: ollama_local; online hosts require explicit consent).",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Explicit model override for the selected host.",
    )
    parser.add_argument(
        "--auto-model",
        action="store_true",
        help="Auto-select best installed local model from hardware capability profile.",
    )
    args = parser.parse_args(argv)
    projects = tuple(p.strip() for p in args.project if p.strip())

    selected_model = args.model
    if not selected_model and args.auto_model and args.host_id == "ollama_local":
        hw = detect_hardware_profile(ollama_timeout=2.0, use_cache=True)
        selected_model = hw.selected_installed_generation_model or GEN_MODEL

    scope_label = f" | projects={','.join(projects)}" if projects else ""
    console.print(
        Panel.fit(
            f"[bold cyan]Second Brain Grounded Memory Interface[/bold cyan]\n"
            f"host={rich_escape(args.host_id)} | model={rich_escape(selected_model or 'default')} | "
            f"mode={rich_escape(args.mode)} | contract={rich_escape(args.answer_mode)}{rich_escape(scope_label)}\n"
            "Type 'exit' to quit.",
            border_style="cyan",
        )
    )

    while True:
        try:
            user_input = console.input("\n[bold green]You>[/bold green] ")
        except (KeyboardInterrupt, EOFError):
            break

        if user_input.lower() in ["exit", "quit"]:
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

        try:
            candidate_ids = peek_lexical_candidate_atom_ids(user_input)
            record_retrieval_telemetry(
                [item.filename for item in retrieved_atoms],
                candidate_atom_ids=candidate_ids,
                mode=args.mode,
            )
        except Exception:
            pass

        if retrieved_atoms:
            explanations = "; ".join(
                f"{item.filename}[{','.join(item.match_reasons)}]" for item in retrieved_atoms
            )
            console.print(f"[dim green]✓ Relevant memories retrieved: {rich_escape(explanations)}[/dim green]")
        else:
            console.print("[dim yellow]⚠ No relevant memories found.[/dim yellow]")

        unverified_leads = ()
        if args.answer_mode == "research":
            if len(projects) <= 1:
                unverified_leads = collect_unverified_leads(
                    user_input,
                    atoms_dir=ATOMS_DIR,
                    brain_dir=BRAIN_DIR,
                    project_scope=projects[0] if projects else None,
                    allow_cross_project=args.allow_cross_project,
                    retrieval_mode=args.mode,
                )
            else:
                seen_leads: dict[str, object] = {}
                for proj_item in projects:
                    for ld in collect_unverified_leads(
                        user_input,
                        atoms_dir=ATOMS_DIR,
                        brain_dir=BRAIN_DIR,
                        project_scope=proj_item,
                        allow_cross_project=args.allow_cross_project,
                        retrieval_mode=args.mode,
                    ):
                        seen_leads.setdefault(ld.lead_id, ld)
                unverified_leads = tuple(list(seen_leads.values())[:5])

        if not retrieved_atoms:
            full_response = answer_from_memory("", lambda: "")
            if args.answer_mode == "research" and unverified_leads:
                full_response = f"{full_response}\n\n{format_unverified_leads_section(unverified_leads)}"
            try:
                record_grounded_answer_telemetry([], abstained=True, mode=args.answer_mode)
            except Exception:
                pass
            console.print(f"\n[bold blue]{rich_escape(args.host_id)}>[/bold blue]")
            console.print(full_response, markup=False)
            continue

        full_response = ""
        try:
            ans_req = AnswerRequest(
                query=user_input,
                mode=args.answer_mode,
                project_scope=projects[0] if len(projects) == 1 else None,
                result_limit=len(retrieved_atoms),
            )
            if args.host_id != "ollama_local":
                answer = generate_grounded_answer_via_host(
                    ans_req,
                    retrieved_atoms,
                    host_id=args.host_id,
                    model_override=selected_model,
                    atoms_dir=ATOMS_DIR,
                    brain_dir=BRAIN_DIR,
                    unverified_leads=unverified_leads if args.answer_mode == "research" else None,
                )
            else:
                policies = load_host_policies()
                local_rule = policies.get("ollama_local")
                if local_rule is None or not local_rule.enabled:
                    raise HostConsentError("Host 'ollama_local' is disabled in host-sharing-policy.json.")
                validate_endpoint_network_boundary(local_rule)
                if args.answer_mode == "preference" and not local_rule.allow_preference_atoms:
                    raise HostConsentError("Host 'ollama_local' is not authorized to receive preference atoms.")
                effective_model = validate_model_identifier(
                    (selected_model or "").strip() or local_rule.default_model or GEN_MODEL,
                    provider="ollama",
                )
                scoped_atoms = filter_atoms_for_host(
                    retrieved_atoms,
                    local_rule,
                    mode=args.answer_mode,
                    atoms_dir=ATOMS_DIR,
                    brain_dir=BRAIN_DIR,
                )
                if not scoped_atoms:
                    full_response = NO_APPROVED_MEMORY_RESPONSE
                    if args.answer_mode == "research" and unverified_leads:
                        full_response = f"{full_response}\n\n{format_unverified_leads_section(unverified_leads)}"
                    try:
                        record_grounded_answer_telemetry([], abstained=True, mode=args.answer_mode)
                    except Exception:
                        pass
                    console.print(f"\n[bold blue]{rich_escape(args.host_id)}>[/bold blue]")
                    console.print(full_response, markup=False)
                    continue
                grounding_prompt = build_grounding_prompt(
                    ans_req,
                    scoped_atoms,
                    atoms_dir=ATOMS_DIR,
                    brain_dir=BRAIN_DIR,
                )
                redaction, pii_placeholder_map = _redact_outbound_with_map(
                    grounding_prompt,
                    redact_pii=local_rule.redact_pii,
                    fail_on_secret_detection=local_rule.fail_on_secret_detection,
                )
                # Stateless single-turn prompt prevents prior-turn assistant claims from bleeding across turns.
                response_stream = _stream_local_ollama_chat(
                    endpoint_url=local_rule.endpoint_url,
                    model=effective_model,
                    messages=[{"role": "user", "content": redaction.sanitized_text}],
                    schema=ANSWER_DRAFT_SCHEMA,
                )
                for chunk in response_stream:
                    content = chunk["message"]["content"]
                    full_response += content
                from hardware_profile import strip_reasoning_think_blocks

                cleaned_response = strip_reasoning_think_blocks(full_response) or "{}"
                rehydrated_draft = _rehydrate_draft_claims(cleaned_response, pii_placeholder_map)
                answer = validate_standard_answer(
                    rehydrated_draft,
                    scoped_atoms,
                    atoms_dir=ATOMS_DIR,
                    mode=args.answer_mode,
                    unverified_leads=unverified_leads,
                )
                _audit_outbound_call(
                    brain_dir=BRAIN_DIR,
                    rule=local_rule,
                    model=effective_model,
                    atom_ids=[item.filename for item in scoped_atoms],
                    pii_redactions=redaction.pii_redaction_count,
                    outcome="grounded_answer" if answer.answerable else "abstained",
                )
            cited_atoms = [ev.atom_id for cl in answer.claims for ev in cl.evidence]
            try:
                record_grounded_answer_telemetry(cited_atoms, abstained=not answer.answerable, mode=args.answer_mode)
            except Exception:
                pass
            full_response = answer.text
            console.print(f"\n[bold blue]{rich_escape(args.host_id)}>[/bold blue]")
            console.print(full_response, markup=False)
        except HostConsentError as e:
            console.print(f"\n[bold yellow]Host policy blocked ({rich_escape(args.host_id)}): {rich_escape(str(e))}[/bold yellow]")
            continue
        except Exception as e:
            console.print(f"\n[bold red]Error invoking AI host ({rich_escape(args.host_id)}): {rich_escape(str(e))}[/bold red]")
            continue

if __name__ == "__main__":
    chat_loop()
