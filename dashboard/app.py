# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "streamlit",
#     "plotly",
#     "networkx",
#     "pandas",
#     "ollama",
#     "httpx",
#     "pydantic",
#     "filelock",
# ]
# ///
"""
Feature 5 — Local Web Dashboard
A beautiful, Tokyo Night-themed, fully interactive Streamlit web dashboard.
Provides chat with memory, 2D/3D knowledge graph, vault browser & editor,
Web Scout control, and system action triggers with live streaming logs.

Install dependencies with `uv sync --frozen`, then run:
    uv run --frozen streamlit run dashboard/app.py --server.address 127.0.0.1
"""

from collections.abc import Sequence
import glob
import hashlib
import html
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import networkx as nx
import ollama
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ─── Path Setup ──────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APP_DIR)
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, str(SCRIPTS_DIR))

from scripts.config import (
    BRAIN_DIR as _DEFAULT_BRAIN_DIR,
    CHROMA_PATH as _DEFAULT_CHROMA_PATH,
    LEXICAL_INDEX_PATH as _DEFAULT_LEXICAL_INDEX_PATH,
    TELEMETRY_DB_PATH as _DEFAULT_TELEMETRY_DB_PATH,
    GEN_MODEL,
    _WINDOWS_RESERVED_STEMS,
    _is_reparse_or_link,
    read_house_rules,
    today,
    now_slug,
    is_content_markdown,
    normalize_markdown_filename,
    resolve_active_brain_dir,
    safe_file_in_dir,
)
from scripts.recall import (
    RecallOptions,
    consume_last_degradation_warning,
    normalize_project_tag,
    search_memory as recall_search_memory,
    suggest_duplicate_atoms,
)
from scripts.answer_policy import (
    AnswerRequest,
    answer_from_memory,
    format_research_leads,
    validate_standard_answer,
)
from scripts.atom_mutator import apply_atom_edit, apply_atom_merge
from scripts.audit_sources import audit_sources
from scripts.review_atom import apply_review_transition, approved_revision_is_current, review_lock
from scripts.memory_format import split_frontmatter
from scripts.memory_review import list_review_queue, parse_atom_record, section_text
from scripts.review_details import atom_review_details
from scripts.dashboard_auth import assert_loopback_address, require_dashboard_login
from scripts.hardware_profile import detect_hardware_profile
from scripts.inbox_workflow import inspect_inbox_item, list_inbox_items, promote_inbox_item, reject_inbox_item
import scripts.host_adapters as _host_adapters_mod
from scripts.host_adapters import (
    HostConsentError,
    HostSharingRule,
    filter_atoms_for_host,
    generate_grounded_answer_via_host,
    load_host_policies,
    save_host_policies,
    validate_endpoint_network_boundary,
    validate_model_identifier,
)
from scripts.cognitive_core import (
    compute_memory_fitness_report,
    peek_lexical_candidate_atom_ids,
    record_atom_feedback,
    record_grounded_answer_telemetry,
    record_retrieval_telemetry,
    save_experience_record,
)
from scripts.skill_playbooks import (
    deprecate_skill_playbook,
    inspect_skill_candidates,
    list_playbooks,
    promote_skill_playbook,
)
from scripts.executive_planner import (
    ExecutivePlanRequest,
    assemble_executive_plan,
)
from scripts.notes_store import (
    NoteError,
    NotePaths,
    backlinks,
    create_note,
    delete_note,
    extract_action_items,
    get_note,
    list_notes,
    update_note,
    verify_note_excerpt,
)
import scripts.bulk_import as _bulk_import_mod
from scripts.bulk_import import import_folder
from scripts.doc_extractors import DOC_EXTRACTOR_EXTS, extract_document
from scripts.ide_connector import (
    PRIVACY_NOTICE as IDE_PRIVACY_NOTICE,
    connect as ide_connect,
    disconnect as ide_disconnect,
    host_targets as ide_host_targets,
    status as ide_connector_status,
)
import scripts.streaming_chat as _streaming_chat_mod
from scripts.streaming_chat import CHAT_MODES, ChatPaths, chat_events, warmup
from scripts.vault_ledger import (
    commit_ledger_checkpoint,
    compute_vault_merkle,
    prove_atom_membership,
    verify_merkle_proof,
    verify_vault_ledger,
)
from scripts.knowledge_graph import build_knowledge_graph, detect_contradictions
from scripts.time_travel import (
    create_snapshot,
    diff_snapshots,
    list_snapshots,
    recall_at_snapshot,
    verify_snapshot,
)
from scripts.vault_sync import (
    init_sync as vault_init_sync,
    join_sync as vault_join_sync,
    sync_pull as vault_sync_pull,
    sync_push as vault_sync_push,
    sync_status as vault_sync_status,
    sync_verify as vault_sync_verify,
)
from scripts.vault_workspaces import (
    create_vault as ws_create_vault,
    federated_recall as ws_federated_recall,
    list_vaults as ws_list_vaults,
    remove_vault as ws_remove_vault,
    switch_vault as ws_switch_vault,
    update_vault_policy as ws_update_vault_policy,
)
from scripts.web_clipper import (
    create_clipper_pairing as clipper_create_pairing,
    list_clipper_pairings as clipper_list_pairings,
    revoke_clipper_pairing as clipper_revoke_pairing,
)

# Resolve active vault directories dynamically so env overrides (BRAIN_VAULT_DIR) or persisted vault paths stay isolated per run.
_env_vault_override = (os.environ.get("BRAIN_VAULT_DIR") or "").strip()
BRAIN_DIR = os.path.abspath(_env_vault_override) if _env_vault_override else resolve_active_brain_dir()
_vault_overridden = bool(_env_vault_override) or (os.path.abspath(BRAIN_DIR) != os.path.abspath(_DEFAULT_BRAIN_DIR))
RAW_DIR = os.path.join(BRAIN_DIR, "0-raw")
INBOX_DIR = os.path.join(RAW_DIR, "inbox")
DESK_DIR = os.path.join(BRAIN_DIR, "1-desk")
ATOMS_DIR = os.path.join(BRAIN_DIR, "2-atoms")
THREADS_DIR = os.path.join(BRAIN_DIR, "3-threads")
EXPERIENCES_DIR = os.path.join(BRAIN_DIR, "4-experiences")
PLAYBOOKS_DIR = os.path.join(BRAIN_DIR, "5-playbooks")
SOURCES_DIR = os.path.join(BRAIN_DIR, "sources")
BRIEFINGS_DIR = os.path.join(BRAIN_DIR, "briefings")
NOTES_DIR = os.path.join(BRAIN_DIR, "notes")
NOTE_SNAPSHOTS_DIR = os.path.join(RAW_DIR, "note-snapshots")
READING_LIST = os.path.join(RAW_DIR, "reading-list.md")
HOUSE_RULES = os.path.join(BRAIN_DIR, "house-rules.md")
SIDECAR_DIR = os.path.join(BRAIN_DIR, ".sidecar")
CHROMA_PATH = os.path.join(BRAIN_DIR, ".chroma") if _vault_overridden else _DEFAULT_CHROMA_PATH
LEXICAL_INDEX_PATH = os.path.join(SIDECAR_DIR, "lexical.sqlite3") if _vault_overridden else _DEFAULT_LEXICAL_INDEX_PATH
TELEMETRY_DB_PATH = os.path.join(SIDECAR_DIR, "telemetry.sqlite3") if _vault_overridden else _DEFAULT_TELEMETRY_DB_PATH
SUPPORTED_IMPORT_FORMATS = (".md", ".markdown", ".txt", ".pdf", ".docx", ".epub", ".html", ".htm", ".csv", ".tsv")
_API_KEY_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_UNSET = object()

# ─── Page Settings & Styling ──────────────────────────────────────────────────
st.set_page_config(
    page_title="Second Brain 2.0 Dashboard",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Enforce strict loopback interface binding (127.0.0.1 only, reject 0.0.0.0 or external interfaces).
try:
    assert_loopback_address(st.get_option("server.address"))
except PermissionError as _bind_exc:
    st.error(str(_bind_exc))
    st.stop()

# Do not inspect or render vault data until the local owner session is unlocked.
dashboard_authenticated = require_dashboard_login(st)
if not dashboard_authenticated:
    st.stop()

for _vault_subdir in (
    RAW_DIR,
    INBOX_DIR,
    DESK_DIR,
    ATOMS_DIR,
    THREADS_DIR,
    EXPERIENCES_DIR,
    PLAYBOOKS_DIR,
    SOURCES_DIR,
    BRIEFINGS_DIR,
    NOTES_DIR,
    NOTE_SNAPSHOTS_DIR,
    CHROMA_PATH,
    SIDECAR_DIR,
    os.path.join(RAW_DIR, "archive"),
):
    os.makedirs(_vault_subdir, exist_ok=True)

# Custom Tokyo Night stylesheet
st.markdown("""
    <style>
    /* Global Styles */
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }
    
    h1, h2, h3, h4, h5, h6 {
        font-family: 'Outfit', sans-serif;
        font-weight: 600;
        background: linear-gradient(135deg, #00f2fe 0%, #4facfe 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    
    /* Main Container overrides */
    .stApp {
        background-color: #0d0f18;
    }
    
    /* Custom cards */
    .glass-card {
        background: rgba(22, 26, 45, 0.6);
        border: 1px solid rgba(0, 242, 254, 0.15);
        border-radius: 12px;
        padding: 1.5rem;
        margin-bottom: 1rem;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
        backdrop-filter: blur(8px);
        -webkit-backdrop-filter: blur(8px);
    }
    
    .stats-card {
        text-align: center;
        border-left: 4px solid #00f2fe;
    }
    
    .stats-val {
        font-size: 2.2rem;
        font-weight: 800;
        color: #00f2fe;
        font-family: 'Outfit', sans-serif;
    }
    
    .stats-label {
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.1em;
        color: #8f9bb3;
    }

    /* Sidebar styles */
    section[data-testid="stSidebar"] {
        background-color: #090a10;
        border-right: 1px solid rgba(0, 242, 254, 0.1);
    }
    
    /* Statuses */
    .status-badge {
        display: inline-block;
        padding: 0.25em 0.6em;
        font-size: 75%;
        font-weight: 700;
        line-height: 1;
        text-align: center;
        white-space: nowrap;
        vertical-align: baseline;
        border-radius: 0.25rem;
    }
    .status-ok { background-color: rgba(40, 167, 69, 0.2); color: #28a745; border: 1px solid #28a745; }
    .status-pending { background-color: rgba(255, 193, 7, 0.2); color: #ffc107; border: 1px solid #ffc107; }
    .status-fail { background-color: rgba(220, 53, 69, 0.2); color: #dc3545; border: 1px solid #dc3545; }
    </style>
""", unsafe_allow_html=True)


# ─── Data & Helpers ───────────────────────────────────────────────────────────

def get_counts():
    """Counts files in each vault directory."""
    counts = {
        "atoms": 0,
        "notes": 0,
        "playbooks": 0,
        "threads": 0,
        "experiences": 0,
        "sources": 0,
        "raw": 0,
        "inbox": 0,
    }
    if os.path.exists(ATOMS_DIR) and not _is_reparse_or_link(ATOMS_DIR):
        counts["atoms"] = len([f for f in os.listdir(ATOMS_DIR) if is_content_markdown(f)])
    if os.path.exists(NOTES_DIR) and not _is_reparse_or_link(NOTES_DIR):
        counts["notes"] = len([f for f in os.listdir(NOTES_DIR) if is_content_markdown(f)])
    if os.path.exists(PLAYBOOKS_DIR) and not _is_reparse_or_link(PLAYBOOKS_DIR):
        counts["playbooks"] = len([f for f in os.listdir(PLAYBOOKS_DIR) if is_content_markdown(f)])
    if os.path.exists(THREADS_DIR) and not _is_reparse_or_link(THREADS_DIR):
        counts["threads"] = len([f for f in os.listdir(THREADS_DIR) if is_content_markdown(f)])
    if os.path.exists(EXPERIENCES_DIR) and not _is_reparse_or_link(EXPERIENCES_DIR):
        counts["experiences"] = len([f for f in os.listdir(EXPERIENCES_DIR) if is_content_markdown(f)])
    if os.path.exists(SOURCES_DIR) and not _is_reparse_or_link(SOURCES_DIR):
        counts["sources"] = len([f for f in os.listdir(SOURCES_DIR) if is_content_markdown(f)])
    if os.path.exists(RAW_DIR) and not _is_reparse_or_link(RAW_DIR):
        counts["raw"] = len([
            f for f in os.listdir(RAW_DIR)
            if os.path.isfile(os.path.join(RAW_DIR, f)) and f.endswith(".md") and f not in ("reading-list.md", "README.md")
        ])
    if os.path.exists(INBOX_DIR) and not _is_reparse_or_link(INBOX_DIR):
        counts["inbox"] = len([
            f for f in os.listdir(INBOX_DIR)
            if os.path.isfile(os.path.join(INBOX_DIR, f)) and (f.endswith(".md") or f.endswith(".intake")) and f != "README.md"
        ])
    return counts

def _writable_process_cwd() -> str:
    """Return a writable working directory (BRAIN_DIR parent if writable, else system temp dir)."""
    candidate = Path(BRAIN_DIR).resolve().parent
    try:
        if candidate.is_dir() and os.access(str(candidate), os.W_OK | os.X_OK):
            return str(candidate)
    except OSError:
        pass
    return tempfile.gettempdir()


def _daemon_launcher_hint() -> str:
    launcher = r".\run_daemon.ps1" if os.name == "nt" else "./run_daemon.sh"
    return (
        f"ℹ️ Launch the real-time folder daemon via `{launcher}` (repo checkout) "
        "or `second-brain daemon` (installed CLI)."
    )


def run_script_process(script_rel_path: str, extra_args: Sequence[str] = ()):
    """Launch a Python script using sys.executable and a wheel-safe writable cwd."""
    try:
        script_name = Path(script_rel_path).name
        script_abs = (SCRIPTS_DIR / script_name).resolve()
        if script_abs.parent != SCRIPTS_DIR.resolve() or not script_abs.is_file():
            raise FileNotFoundError(f"Script not found in {SCRIPTS_DIR}: {script_name}")
        cmd = [sys.executable, str(script_abs), *(str(arg) for arg in extra_args)]
        child_env = {**os.environ, "BRAIN_VAULT_DIR": BRAIN_DIR}
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=_writable_process_cwd(),
            env=child_env,
        )
        return process
    except Exception as e:
        st.error(f"Failed to start {script_rel_path}: {e}")
        return None

def stream_logs_ui(process, progress_text):
    """Render logs to st.code in real time."""
    if not process:
        return
    
    log_area = st.empty()
    status_bar = st.info(f"⏳ {progress_text}...")
    log_lines = []
    
    while True:
        line = process.stdout.readline()
        if not line and process.poll() is not None:
            break
        if line:
            log_lines.append(line.strip())
            # Keep last 30 lines
            log_area.code("\n".join(log_lines[-30:]))
            
    process.wait()
    status_bar.empty()
    if process.returncode == 0:
        st.success("✅ Operation completed successfully!")
        st.balloons()
    else:
        st.error(f"❌ Failed with exit code {process.returncode}")

def make_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", str(text or "").lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    slug = slug[:max_len].rstrip("-") or "capture"
    if slug.split(".", 1)[0].lower() in _WINDOWS_RESERVED_STEMS:
        slug = f"capture-{slug}"
    return slug

def get_ollama_model_names() -> list[str]:
    try:
        local_rule = load_host_policies().get("ollama_local")
        if local_rule is None or not local_rule.enabled:
            return []
        validate_endpoint_network_boundary(local_rule)
        response = ollama.Client(
            host=local_rule.endpoint_url,
            follow_redirects=False,
            trust_env=False,
        ).list()
    except Exception:
        return []

    models = response.get("models", response) if isinstance(response, dict) else getattr(response, "models", [])
    names = []
    for model in models or []:
        if isinstance(model, dict):
            name = model.get("name") or model.get("model")
        else:
            name = getattr(model, "model", None) or getattr(model, "name", None)
        if name:
            names.append(name)
    return names

def safe_wikilink_filename(link: str) -> str | None:
    target = normalize_markdown_filename(link)
    if safe_file_in_dir(ATOMS_DIR, target, allowed_exts={".md"}, must_exist=False):
        return target
    return None


def validate_api_key_env_identifier(raw_env: str | None, *, transport: str) -> str | None:
    """Validate api_key_env as an uppercase env var identifier (never a raw API secret)."""
    cleaned = (raw_env or "").strip()
    if not cleaned:
        if transport == "online":
            raise ValueError(
                "Online hosts require an uppercase environment variable identifier (e.g. OPENAI_API_KEY)."
            )
        return None
    if not _API_KEY_ENV_RE.fullmatch(cleaned):
        raise ValueError(
            "api_key_env must be an uppercase environment variable identifier matching "
            "[A-Z][A-Z0-9_]{1,63} (never paste a raw API key)."
        )
    return cleaned


def set_host_sharing_profile(
    host_id: str,
    *,
    enabled: bool,
    endpoint_url: str | None = None,
    default_model: str | None = None,
    allowed_projects: tuple[str, ...] | list[str] = (),
    allow_unscoped_atoms: bool | None = None,
    allow_preference_atoms: bool | None = None,
    redact_pii: bool | None = None,
    fail_on_secret_detection: bool | None = None,
    api_key_env: object = _UNSET,
):
    """Validate endpoint/model/project scopes/redaction/env-var name and persist a per-host sharing profile."""
    policies = load_host_policies()
    if host_id not in policies:
        raise KeyError(f"Unknown AI host '{host_id}'.")
    cur = policies[host_id]

    resolved_api_key_env = (
        cur.api_key_env
        if api_key_env is _UNSET
        else validate_api_key_env_identifier(str(api_key_env or ""), transport=cur.transport)
    )
    resolved_redact_pii = (
        True
        if cur.transport == "online"
        else (cur.redact_pii if redact_pii is None else bool(redact_pii))
    )
    resolved_fail_secret = (
        True
        if cur.transport == "online"
        else (cur.fail_on_secret_detection if fail_on_secret_detection is None else bool(fail_on_secret_detection))
    )

    delegate = getattr(_host_adapters_mod, "set_host_sharing_profile", None)
    if callable(delegate):
        sig = inspect.signature(delegate)
        required_keys = {"redact_pii", "fail_on_secret_detection", "api_key_env"}
        if required_keys.issubset(sig.parameters):
            kwargs: dict[str, object] = {
                "enabled": enabled,
                "allowed_projects": tuple(allowed_projects),
                "redact_pii": resolved_redact_pii,
                "fail_on_secret_detection": resolved_fail_secret,
                "api_key_env": resolved_api_key_env,
            }
            if endpoint_url is not None and "endpoint_url" in sig.parameters:
                kwargs["endpoint_url"] = endpoint_url
            if default_model is not None and "default_model" in sig.parameters:
                kwargs["default_model"] = default_model
            if allow_unscoped_atoms is not None and "allow_unscoped_atoms" in sig.parameters:
                kwargs["allow_unscoped_atoms"] = allow_unscoped_atoms
            if allow_preference_atoms is not None and "allow_preference_atoms" in sig.parameters:
                kwargs["allow_preference_atoms"] = allow_preference_atoms
            return delegate(host_id, **kwargs)

    normalized_projects = tuple(
        "*" if str(p).strip() == "*" else normalize_project_tag(str(p).strip())
        for p in allowed_projects
        if str(p).strip()
    )
    validated_model = validate_model_identifier(
        (default_model or "").strip() or cur.default_model,
        provider=cur.provider,
    )
    clean_endpoint = ((endpoint_url or "").strip() or cur.endpoint_url).rstrip("/")
    updated = HostSharingRule(
        host_id=cur.host_id,
        provider=cur.provider,
        transport=cur.transport,
        endpoint_url=clean_endpoint,
        default_model=validated_model,
        api_key_env=resolved_api_key_env,
        enabled=bool(enabled),
        allowed_projects=normalized_projects,
        allow_unscoped_atoms=(
            cur.allow_unscoped_atoms if allow_unscoped_atoms is None else bool(allow_unscoped_atoms)
        ),
        allow_preference_atoms=(
            cur.allow_preference_atoms if allow_preference_atoms is None else bool(allow_preference_atoms)
        ),
        redact_pii=resolved_redact_pii,
        fail_on_secret_detection=resolved_fail_secret,
    )
    if updated.enabled or clean_endpoint != cur.endpoint_url:
        validate_endpoint_network_boundary(updated)
    policies[host_id] = updated
    return save_host_policies(policies)


_FEEDBACK_SIGNAL_TO_KIND = {
    "endorsed": "helpful",
    "helpful": "helpful",
    "corrected": "correction",
    "correction": "correction",
    "superseded": "supersession",
    "supersession": "supersession",
}


def _submit_atom_feedback(atom_id: str, feedback: str, *, actor: str, note: str = "") -> object:
    """Invoke record_atom_feedback with aligned feedback_kind, actor, and note (never logging supersession as a correction)."""
    norm_fb = str(feedback or "").strip().lower()
    if norm_fb not in _FEEDBACK_SIGNAL_TO_KIND:
        raise ValueError(f"Unsupported feedback signal: {feedback!r}")
    mapped_kind = _FEEDBACK_SIGNAL_TO_KIND[norm_fb]
    is_helpful = mapped_kind == "helpful"
    sig = inspect.signature(record_atom_feedback)
    params = sig.parameters

    if mapped_kind == "supersession" and "feedback_kind" not in params and "feedback" not in params:
        # Backwards-compatibility guard: never record supersession as a false negative correction.
        return {
            "atom_id": atom_id,
            "feedback_kind": "supersession",
            "helpful": False,
            "actor": actor,
            "note": note,
        }

    kwargs: dict[str, object] = {}
    if "helpful" in params:
        kwargs["helpful"] = is_helpful
    if "feedback_kind" in params:
        kwargs["feedback_kind"] = mapped_kind
    if "actor" in params:
        kwargs["actor"] = actor
    if "note" in params:
        kwargs["note"] = note
    if "db_path" in params:
        kwargs["db_path"] = TELEMETRY_DB_PATH
    if "brain_dir" in params:
        kwargs["brain_dir"] = BRAIN_DIR
    if "feedback" in params and "feedback_kind" not in params:
        return record_atom_feedback(atom_id, norm_fb, **kwargs)
    return record_atom_feedback(atom_id, **kwargs)


def _active_note_paths() -> NotePaths:
    return NotePaths.for_brain(BRAIN_DIR)


def _import_single_document(source_path: str, *, tags: tuple[str, ...] = ()) -> dict:
    """Import a single local document into working notes via bulk_import.import_file or doc_extractors fallback."""
    note_paths = _active_note_paths()
    import_file_fn = getattr(_bulk_import_mod, "import_file", None)
    if callable(import_file_fn):
        return import_file_fn(source_path, paths=note_paths, tags=tags)

    src = Path(source_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"Document not found: {src}")
    ext = src.suffix.lower()
    if ext in DOC_EXTRACTOR_EXTS:
        extracted = extract_document(src)
        created = create_note(
            title=extracted.title or src.stem,
            body=extracted.markdown.strip(),
            tags=list(dict.fromkeys((*tags, f"format:{ext.lstrip('.')}"))) if tags else [f"format:{ext.lstrip('.')}"],
            origin="owner_import",
            paths=note_paths,
        )
    elif ext in {".md", ".markdown", ".txt"}:
        text = src.read_bytes().decode("utf-8-sig")
        created = create_note(
            title=src.stem,
            body=text,
            tags=list(tags),
            origin="owner_import",
            paths=note_paths,
        )
    else:
        raise ValueError(f"Unsupported document extension '{ext}'; expected one of {SUPPORTED_IMPORT_FORMATS}.")
    return {**created, "source_file": src.name, "format": ext.lstrip(".")}


def _collect_research_leads(query: str, *, project_scope: str | None = None) -> list[dict[str, object]]:
    """Collect non-promoted pending atoms and quarantined inbox leads strictly for owner Research view."""
    helper = getattr(_streaming_chat_mod, "_collect_unverified_leads", None)
    chat_paths = ChatPaths(
        brain_dir=BRAIN_DIR,
        atoms_dir=ATOMS_DIR,
        lexical_index_path=LEXICAL_INDEX_PATH,
        chroma_path=CHROMA_PATH,
        telemetry_db_path=TELEMETRY_DB_PATH,
    )
    if callable(helper):
        return list(helper(query, chat_paths, project_scope=project_scope))

    q_terms = {t for t in re.findall(r"[a-z0-9]{3,}", (query or "").casefold())}
    if not q_terms:
        return []
    need = min(2, len(q_terms))
    leads: list[dict[str, object]] = []
    for rec in list_review_queue(ATOMS_DIR, {"pending", "needs_edit"}):
        fname = str(rec.get("filename") or "")
        claim_txt = str(rec.get("claim") or "")
        if len(q_terms & set(re.findall(r"[a-z0-9]{3,}", claim_txt.casefold()))) >= need:
            leads.append({
                "id": f"L{len(leads) + 1}",
                "filename": fname,
                "status": str(rec.get("review_status") or "pending"),
                "claim": claim_txt,
                "excerpt": "",
            })
    if not project_scope:
        for item in list_inbox_items(INBOX_DIR, raw_dir=RAW_DIR, brain_dir=BRAIN_DIR):
            preview_txt = str(getattr(item, "preview", "") or "")
            fname = str(getattr(item, "filename", "") or "")
            if len(q_terms & set(re.findall(r"[a-z0-9]{3,}", f"{fname} {preview_txt}".casefold()))) >= need:
                leads.append({
                    "id": f"L{len(leads) + 1}",
                    "filename": f"0-raw/inbox/{fname}",
                    "status": "quarantined_inbox",
                    "claim": preview_txt[:220],
                    "excerpt": preview_txt[:260],
                })
    return leads[:5]


def _list_notes_filtered(*, query: str = "", tag: str = "", limit: int = 25, offset: int = 0):
    """Query working notes with optional tag filtering and pagination."""
    note_paths = _active_note_paths()
    clean_tag = tag.strip()
    sig = inspect.signature(list_notes)
    if "tag" in sig.parameters:
        return list_notes(query=query, tag=clean_tag or None, limit=limit, offset=offset, paths=note_paths)
    if not clean_tag:
        return list_notes(query=query, limit=limit, offset=offset, paths=note_paths)
    all_items, _ = list_notes(query=query, limit=2000, offset=0, paths=note_paths)
    want = clean_tag.casefold()
    filtered = [rec for rec in all_items if any(t.casefold() == want for t in rec.tags)]
    return filtered[offset : offset + limit], len(filtered)

def _extract_note_excerpt_candidates(body: str) -> list[str]:
    """Extract clean sentence and bullet candidates (>=12 chars) from a working note body."""
    candidates: list[str] = []
    for raw_line in (body or "").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("```"):
            continue
        cleaned = re.sub(r"^(?:[-*]|\d+[.)])\s+", "", stripped).strip()
        if 12 <= len(cleaned) <= 600 and cleaned not in candidates:
            candidates.append(cleaned)
        for sent in re.split(r"(?<=[.!?])\s+", cleaned):
            s = sent.strip()
            if 12 <= len(s) <= 600 and s not in candidates:
                candidates.append(s)
    return candidates[:60]

def _defang_markdown_preview(md_text: str) -> str:
    """Neutralize external HTTP/HTTPS image tags so local Markdown preview never leaks network beacons."""
    return re.sub(
        r"!\[([^\]]*)\]\((https?://[^)\s]+)([^)]*)\)",
        r"`[External image blocked in local preview: \1 -> \2]`",
        md_text or "",
        flags=re.IGNORECASE,
    )

def _extract_backing_claim_units(atom_filename: str) -> list[str]:
    """Read a backing procedural atom's Claim section and return exact copyable grounding lines."""
    path = safe_file_in_dir(ATOMS_DIR, atom_filename, allowed_exts={".md"}, must_exist=True)
    if not path or _is_reparse_or_link(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return []
    parsed = split_frontmatter(raw)
    body = "\n".join(parsed[1]) if parsed else raw
    m = re.search(r"^##\s+Claim\s*\r?\n(.*?)(?=^##\s+|\Z)", body or "", flags=re.MULTILINE | re.DOTALL)
    claim_block = m.group(1).strip() if m else section_text(body, "Claim")
    units: list[str] = []
    full_flat = " ".join((claim_block or "").split())
    if full_flat:
        units.append(full_flat)
    for raw_line in (claim_block or "").splitlines():
        s = raw_line.strip()
        if not s:
            continue
        bm = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", s)
        line_unit = " ".join((bm.group(1) if bm else s).split())
        if line_unit and line_unit not in units:
            units.append(line_unit)
        for sent in re.split(r"(?<=[.!?])\s+", line_unit):
            st_sent = sent.strip()
            if len(st_sent) >= 5 and st_sent not in units:
                units.append(st_sent)
    return units

def _read_ledger_history() -> list[dict]:
    """Read hash-chained checkpoint entries from brain/.sidecar/vault-ledger.jsonl safely."""
    if os.path.lexists(SIDECAR_DIR) and _is_reparse_or_link(SIDECAR_DIR):
        return []
    ledger_path = os.path.join(SIDECAR_DIR, "vault-ledger.jsonl")
    if not os.path.isfile(ledger_path) or _is_reparse_or_link(ledger_path):
        return []
    rows: list[dict] = []
    try:
        with open(ledger_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    rows.append({
                        "seq": obj.get("seq"),
                        "timestamp": obj.get("timestamp", ""),
                        "actor": obj.get("actor", ""),
                        "leaf_count": obj.get("leaf_count", 0),
                        "approved_atoms": obj.get("approved_atoms", 0),
                        "accepted_playbooks": obj.get("accepted_playbooks", 0),
                        "merkle_root": str(obj.get("merkle_root", "")),
                        "entry_sha256": str(obj.get("entry_sha256", "")),
                        "note": obj.get("note", ""),
                    })
    except OSError:
        return []
    rows.reverse()
    return rows


# ─── Chat RAG Core ────────────────────────────────────────────────────────────

def search_memory(
    query,
    n_results=3,
    follow_links=True,
    projects=(),
    retrieval_mode="auto",
    allow_cross_project=False,
    enable_fitness_boost=False,
    active_context_profile=(),
):
    consume_last_degradation_warning()
    try:
        direct, neighbors = recall_search_memory(
            query,
            RecallOptions(
                n_results=n_results,
                follow_links=follow_links,
                projects=tuple(projects),
                allow_cross_project=allow_cross_project,
                retrieval_mode=retrieval_mode,
                enable_fitness_boost=enable_fitness_boost,
                active_context_profile=tuple(active_context_profile),
                telemetry_db_path=TELEMETRY_DB_PATH,
            ),
            atoms_dir=ATOMS_DIR,
            index_path=LEXICAL_INDEX_PATH,
            chroma_path=CHROMA_PATH,
        )
    except Exception as e:
        st.error(f"Recall Error: {e}")
        return [], []
    degradation = consume_last_degradation_warning()
    if degradation:
        st.warning(degradation)

    return [item.as_dict() for item in direct], [item.as_dict() for item in neighbors]


# ─── Knowledge Graph Builder ──────────────────────────────────────────────────

_EDGE_PRIORITY = {"contested_by": 4, "superseded_by": 3, "link": 2, "topic_overlap": 1}

def build_network_graph(
    *,
    project_scope: str | None = None,
    include_pending: bool = False,
    include_threads: bool = True,
    include_topic_overlap: bool = True,
):
    G = nx.Graph()
    kg = build_knowledge_graph(
        brain_dir=BRAIN_DIR,
        atoms_dir=ATOMS_DIR,
        project_scope=(project_scope or "").strip() or None,
        include_pending=include_pending,
    )
    G.graph["kg_summary"] = kg

    for node in kg.get("nodes", []):
        fname = str(node["filename"])
        raw_title = str(node.get("atom_id") or fname.replace(".md", ""))
        raw_claim = str(node.get("claim") or "")
        G.add_node(
            fname,
            title=html.escape(raw_title),
            escaped_filename=html.escape(fname),
            type="atom",
            approved_current=bool(node.get("approved_current")),
            review_status=str(node.get("effective_status") or node.get("review_status") or "pending"),
            memory_type=str(node.get("memory_type") or ""),
            superseded_by=str(node.get("superseded_by") or ""),
            tags=list(node.get("contexts") or []),
            claim=html.escape(raw_claim),
        )

    for edge in kg.get("edges", []):
        kind = str(edge.get("kind") or "link")
        if kind == "topic_overlap" and not include_topic_overlap:
            continue
        u, v = str(edge["from"]), str(edge["to"])
        if u not in G or v not in G or u == v:
            continue
        if G.has_edge(u, v):
            existing_kind = str(G.edges[u, v].get("kind") or "link")
            if _EDGE_PRIORITY.get(kind, 0) <= _EDGE_PRIORITY.get(existing_kind, 0):
                continue
        G.add_edge(
            u,
            v,
            kind=kind,
            jaccard=edge.get("jaccard"),
            shared_terms=edge.get("shared_terms", []),
        )

    # OverlayContradiction / contested edges if detected among visible nodes
    radar = detect_contradictions(
        brain_dir=BRAIN_DIR,
        atoms_dir=ATOMS_DIR,
        project_scope=(project_scope or "").strip() or None,
        include_pending=include_pending,
    )
    G.graph["contradiction_count"] = radar.get("contradiction_count", 0)
    for alert in radar.get("alerts", []):
        ua = str(alert["atom_a"]["filename"])
        ub = str(alert["atom_b"]["filename"])
        if ua in G and ub in G and ua != ub:
            G.add_edge(ua, ub, kind="contested_by", reasons=alert.get("reasons", []))

    # Optional 3-threads synthesis overlay (only linking to visible non-rejected/non-quarantined atoms)
    if include_threads and os.path.isdir(THREADS_DIR) and not _is_reparse_or_link(THREADS_DIR):
        threads = [f for f in sorted(os.listdir(THREADS_DIR)) if is_content_markdown(f)]
        for thread in threads:
            thread_path = safe_file_in_dir(THREADS_DIR, thread, allowed_exts={".md"})
            if not thread_path or not os.path.isfile(thread_path) or _is_reparse_or_link(thread_path):
                continue
            try:
                with open(thread_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            raw_links = re.findall(r"\[\[(.*?)\]\]", content)
            valid_targets = []
            for link in raw_links:
                cleaned = link.split("|", 1)[0].strip()
                target = safe_wikilink_filename(cleaned)
                if target and target in G:
                    valid_targets.append(target)
            if valid_targets or not (project_scope or "").strip():
                G.add_node(
                    thread,
                    title=html.escape(thread.replace(".md", "")),
                    escaped_filename=html.escape(thread),
                    type="thread",
                    approved_current=False,
                    review_status="synthesis",
                    memory_type="thread",
                    superseded_by="",
                    tags=["Synthesis"],
                    claim="",
                )
                for target in valid_targets:
                    G.add_edge(thread, target, kind="link")

    return G

_EDGE_STYLE_MAP = {
    "link": {"label": "Explicit Link", "color": "rgba(0, 242, 254, 0.60)", "width": 2.0, "dash": "solid"},
    "superseded_by": {"label": "Superseded Lineage", "color": "rgba(255, 158, 0, 0.80)", "width": 2.0, "dash": "dash"},
    "contested_by": {"label": "Contested / Conflict", "color": "rgba(255, 0, 127, 0.90)", "width": 2.6, "dash": "dot"},
    "topic_overlap": {"label": "Topic Overlap", "color": "rgba(157, 78, 221, 0.35)", "width": 1.2, "dash": "dot"},
}

def draw_network_plotly(G, dimension="2D"):
    if not G.nodes():
        return None

    is_3d = dimension == "3D"
    pos = nx.spring_layout(G, dim=3, seed=42) if is_3d else nx.spring_layout(G, seed=42)
    traces = []

    for kind, style in _EDGE_STYLE_MAP.items():
        ex, ey, ez = [], [], []
        for u, v, edata in G.edges(data=True):
            if str(edata.get("kind") or "link") != kind:
                continue
            if u in pos and v in pos:
                if is_3d:
                    x0, y0, z0 = pos[u]
                    x1, y1, z1 = pos[v]
                    ex.extend([x0, x1, None])
                    ey.extend([y0, y1, None])
                    ez.extend([z0, z1, None])
                else:
                    x0, y0 = pos[u]
                    x1, y1 = pos[v]
                    ex.extend([x0, x1, None])
                    ey.extend([y0, y1, None])
        if not ex:
            continue
        if is_3d:
            traces.append(
                go.Scatter3d(
                    x=ex, y=ey, z=ez,
                    name=style["label"],
                    line=dict(width=style["width"], color=style["color"], dash=style["dash"]),
                    hoverinfo="none",
                    mode="lines",
                )
            )
        else:
            traces.append(
                go.Scatter(
                    x=ex, y=ey,
                    name=style["label"],
                    line=dict(width=style["width"], color=style["color"], dash=style["dash"]),
                    hoverinfo="none",
                    mode="lines",
                )
            )

    node_x, node_y, node_z = [], [], []
    node_text, node_color, node_size = [], [], []

    for node in G.nodes():
        if node not in pos:
            continue
        if is_3d:
            x, y, z = pos[node]
            node_x.append(x)
            node_y.append(y)
            node_z.append(z)
        else:
            x, y = pos[node]
            node_x.append(x)
            node_y.append(y)

        ndata = G.nodes[node]
        title = ndata.get("title") or html.escape(str(node))
        esc_file = ndata.get("escaped_filename") or html.escape(str(node))
        ntype = ndata.get("type", "atom")
        status_lbl = str(ndata.get("review_status", "pending"))
        esc_status = html.escape(status_lbl)
        is_verified = bool(ndata.get("approved_current")) or status_lbl == "accepted"
        sup_by = ndata.get("superseded_by", "")
        claim_snip = (ndata.get("claim") or "")[:120]
        badge = "Verified Approved" if is_verified else ("Synthesis Thread" if ntype == "thread" else f"Unverified ({esc_status})")
        hover_parts = [f"<b>{title}</b>", f"Status: {badge}", f"File: {esc_file}"]
        if claim_snip:
            hover_parts.append(f"Claim: {claim_snip}")
        node_text.append("<br>".join(hover_parts))

        if ntype == "thread":
            node_color.append("#00f2fe")
            node_size.append(15 if is_3d else 22)
        elif sup_by:
            node_color.append("#6c757d")
            node_size.append(8 if is_3d else 12)
        elif is_verified:
            node_color.append("#2ecc71")
            node_size.append(10 if is_3d else 15)
        elif status_lbl == "needs_edit":
            node_color.append("#ff4d6d")
            node_size.append(9 if is_3d else 14)
        else:
            node_color.append("#ffb703")
            node_size.append(9 if is_3d else 13)

    if is_3d:
        traces.append(
            go.Scatter3d(
                x=node_x, y=node_y, z=node_z,
                name="Nodes",
                mode="markers",
                hoverinfo="text",
                text=node_text,
                marker=dict(showscale=False, color=node_color, size=node_size, line=dict(color="#0d0f18", width=1)),
            )
        )
        fig = go.Figure(
            data=traces,
            layout=go.Layout(
                showlegend=True,
                legend=dict(font=dict(color="#c0caf5", size=11), bgcolor="rgba(13,15,24,0.6)"),
                hovermode="closest",
                margin=dict(b=0, l=0, r=0, t=0),
                paper_bgcolor="rgba(0,0,0,0)",
                scene=dict(
                    xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, title="", visible=False),
                    yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, title="", visible=False),
                    zaxis=dict(showgrid=False, zeroline=False, showticklabels=False, title="", visible=False),
                    bgcolor="rgba(0,0,0,0)",
                ),
            ),
        )
    else:
        traces.append(
            go.Scatter(
                x=node_x, y=node_y,
                name="Nodes",
                mode="markers",
                hoverinfo="text",
                text=node_text,
                marker=dict(showscale=False, color=node_color, size=node_size, line=dict(color="#0d0f18", width=1.5)),
            )
        )
        fig = go.Figure(
            data=traces,
            layout=go.Layout(
                showlegend=True,
                legend=dict(font=dict(color="#c0caf5", size=11), bgcolor="rgba(13,15,24,0.6)"),
                hovermode="closest",
                margin=dict(b=0, l=0, r=0, t=0),
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, visible=False),
                yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, visible=False),
            ),
        )

    return fig


# ─── Sidebar Panel ────────────────────────────────────────────────────────────

st.sidebar.markdown("""
    <div style="text-align: center; margin-bottom: 1.4rem;">
        <h1 style="margin: 0; font-size: 1.8rem; background: linear-gradient(135deg, #00f2fe 0%, #9d4edd 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">🧠 Second Brain 2.0</h1>
        <p style="color: #657b83; font-size: 0.8rem; margin: 0.2rem 0 0 0;">Verifiable Semantic Memory System</p>
    </div>
""", unsafe_allow_html=True)

# Count stats
counts = get_counts()

# Render side metrics across all vault tiers
cols = st.sidebar.columns(2)
with cols[0]:
    st.markdown(f"""
        <div class="glass-card stats-card">
            <div class="stats-val">{counts['atoms']}</div>
            <div class="stats-label">Atoms</div>
        </div>
    """, unsafe_allow_html=True)
with cols[1]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #2ecc71;">
            <div class="stats-val">{counts['notes']}</div>
            <div class="stats-label">Notes</div>
        </div>
    """, unsafe_allow_html=True)

cols_pb = st.sidebar.columns(2)
with cols_pb[0]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #4facfe;">
            <div class="stats-val">{counts['playbooks']}</div>
            <div class="stats-label">Playbooks</div>
        </div>
    """, unsafe_allow_html=True)
with cols_pb[1]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #9d4edd;">
            <div class="stats-val">{counts['threads']}</div>
            <div class="stats-label">Threads</div>
        </div>
    """, unsafe_allow_html=True)

cols2 = st.sidebar.columns(2)
with cols2[0]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #ffb703;">
            <div class="stats-val">{counts['experiences']}</div>
            <div class="stats-label">Episodes</div>
        </div>
    """, unsafe_allow_html=True)
with cols2[1]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #ff9e00;">
            <div class="stats-val">{counts['sources']}</div>
            <div class="stats-label">Sources</div>
        </div>
    """, unsafe_allow_html=True)

cols3 = st.sidebar.columns(2)
with cols3[0]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #00b4d8;">
            <div class="stats-val">{counts['raw']}</div>
            <div class="stats-label">Raw Captures</div>
        </div>
    """, unsafe_allow_html=True)
with cols3[1]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #ff007f;">
            <div class="stats-val">{counts['inbox']}</div>
            <div class="stats-label">Quarantined</div>
        </div>
    """, unsafe_allow_html=True)

st.sidebar.markdown("<br>", unsafe_allow_html=True)

# Navigation Menu
nav_selection = st.sidebar.radio(
    "NAVIGATION",
    [
        "💬 Chat with Memory",
        "📝 Working Notes & Import",
        "🕸️ Knowledge Graph",
        "🗄️ Vault Explorer",
        "📥 Inbox & Web Scout",
        "🧬 Host API, Quarantine & Fitness",
        "🛡️ Vault Proof & Time-Travel",
        "⚙️ System Control",
    ],
    index=0
)

# ─── Navigation Logic ──────────────────────────────────────────────────────────

if _flash_banner := st.session_state.pop("_dashboard_flash_success", None):
    st.success(str(_flash_banner))

# Initialize chat messages session state
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []


# PAGE 1: CHAT WITH MEMORY
if nav_selection == "💬 Chat with Memory":
    st.title("💬 Chat with Memory")
    st.subheader("Interactive Verifiable Recall & Grounded Synthesis")

    chat_mode_labels = {
        "stream": "⚡ Stream Synthesis ([V#]/[N#]/[P#] + Grounding Audit)",
        "instant": "⚡ Instant Source Cards (<20 ms, No Model Call)",
        "verified": "🔒 Fast Verified Claim Selection",
        "strict_audit_answer": "🛡️ Strict Schema-Validated Host Answer (Multi-Host)",
    }
    answer_view_labels = {
        "standard": "✅ Standard (Verified Claims & Notes)",
        "preference": "🎯 Preference (Owner Style & Guardrails)",
        "research": "🔬 Research (+ Separated Unverified Leads)",
    }
    top_c1, top_c2, top_c3, top_c4 = st.columns([2.1, 1.5, 1.0, 0.9])
    with top_c1:
        selected_chat_mode = st.selectbox(
            "Chat Execution Mode",
            options=list(chat_mode_labels.keys()),
            format_func=lambda k: chat_mode_labels[k],
            index=0,
            key="chat_exec_mode",
        )
    with top_c2:
        rag_answer_view = st.selectbox(
            "Answer View",
            options=list(answer_view_labels.keys()),
            format_func=lambda k: answer_view_labels[k],
            index=0,
            key="chat_answer_view",
        )
    with top_c3:
        if st.button("🔥 Warmup Local Model", width="stretch"):
            with st.spinner("Preloading local Ollama model into memory..."):
                try:
                    wres = warmup()
                    st.success(f"Loaded `{wres['model']}` in {wres['warmup_ms']} ms.")
                except Exception as exc:
                    st.error(f"Warmup unavailable: {exc}")
    with top_c4:
        if st.button("🧹 Clear Chat History", width="stretch"):
            st.session_state.chat_history = []
            st.rerun()

    policies_map = load_host_policies()
    enabled_host_ids = [hid for hid, r in sorted(policies_map.items()) if r.enabled]
    host_ids = enabled_host_ids or sorted(policies_map.keys()) or ["ollama_local"]
    default_host_idx = host_ids.index("ollama_local") if "ollama_local" in host_ids else 0

    # RAG Settings Box
    with st.expander("⚙️ Retrieval & Host Parameters", expanded=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            rag_retrieval_mode = st.selectbox(
                "Retrieval Mode",
                ["auto", "lexical", "dense", "rrf"],
                index=0,
                key="chat_retrieval_mode",
            )
            chat_host_id = st.selectbox(
                "AI Host (Enabled Hosts)",
                options=host_ids,
                index=default_host_idx,
                key="chat_host_id",
                help="Select any owner-enabled AI host configured in `host-sharing-policy.json`.",
            )
            rag_n_results = st.slider("Atoms to Retrieve (Strict Audit Mode)", 1, 10, 4)
        with c2:
            rag_follow = st.checkbox("Follow Graph Links (Graph RAG)", value=True)
            rag_cross_project = st.checkbox("Allow Cross-Project Links", value=False)
            rag_fitness_boost = st.checkbox("Fitness & Freshness Boost (Approved Only)", value=False)
            rag_project_scope = st.text_input("Project Scope (optional, e.g. project:second_brain)", value="", key="chat_project_scope")
        with c3:
            active_host_rule = policies_map.get(chat_host_id)
            default_host_model = active_host_rule.default_model if active_host_rule else GEN_MODEL
            installed_models = get_ollama_model_names()
            if active_host_rule is None or active_host_rule.provider == "ollama":
                default_local_model = (
                    policies_map["ollama_local"].default_model
                    if "ollama_local" in policies_map
                    else GEN_MODEL
                )
                model_options = list(dict.fromkeys([
                    default_local_model,
                    GEN_MODEL,
                    *installed_models,
                    "llama3.2:3b",
                    "qwen2.5-coder:7b",
                    "qwen3.6:latest",
                    "mistral",
                    "phi3",
                ]))
                ollama_preset = st.selectbox("Local Ollama Model Preset", model_options, key="chat_ollama_preset")
                custom_override = st.text_input(
                    "Model Override (optional)",
                    value="",
                    placeholder=ollama_preset,
                    key="chat_model_override",
                )
                st_model = custom_override.strip() or ollama_preset
            else:
                custom_override = st.text_input(
                    f"Model Override ({active_host_rule.provider})",
                    value="",
                    placeholder=default_host_model,
                    key="chat_model_override",
                )
                st_model = custom_override.strip() or default_host_model
            rag_context_profile = st.text_input("Active Context Profile (optional, e.g. windows, python)", value="", key="chat_context_profile")

    def _render_research_leads_box(leads_text: str | None, label: str = "UNVERIFIED LEADS — NOT OWNER-APPROVED"):
        if not leads_text:
            return
        st.warning(f"⚠️ **{label}** — Exploratory candidate/quarantined leads only; never treat as owner-approved fact.")
        st.code(leads_text, language="text")

    def _render_source_cards(sources: list[dict], retrieval_ms: float | None = None):
        if not sources:
            return
        v_cnt = sum(1 for s in sources if s.get("tier") == "verified")
        p_cnt = sum(1 for s in sources if s.get("tier") == "preference")
        n_cnt = sum(1 for s in sources if s.get("tier") == "note")
        l_cnt = sum(1 for s in sources if s.get("tier") == "unverified_lead")
        timing_lbl = f" in {retrieval_ms} ms" if retrieval_ms is not None else ""
        lead_lbl = f" · {l_cnt} Unverified Leads [L#]" if l_cnt else ""
        with st.expander(
            f"📚 Retrieved Sources{timing_lbl}: {v_cnt} Verified [V#] · {n_cnt} Working Notes [N#] · {p_cnt} Preferences [P#]{lead_lbl}",
            expanded=False,
        ):
            for src in sources:
                cid = str(src.get("citation_id", "?"))
                tier = str(src.get("tier", "note"))
                fname = str(src.get("filename", ""))
                if tier == "verified":
                    st.markdown(f"**`[{cid}]` ✅ Verified Atom `{fname}`**")
                    st.code(str(src.get("claim", "")), language="text")
                    if src.get("excerpt"):
                        st.caption(f"Anchored evidence: {str(src['excerpt'])[:260]}")
                elif tier == "preference":
                    st.markdown(f"**`[{cid}]` 🎯 Owner Preference `{fname}`**")
                    st.code(str(src.get("claim", "")), language="text")
                elif tier == "unverified_lead":
                    st.markdown(f"**`[{cid}]` ⚠️ Unverified Lead `{fname}`** *(NOT OWNER-APPROVED)*")
                    st.code(str(src.get("claim") or src.get("excerpt") or ""), language="text")
                else:
                    st.markdown(f"**`[{cid}]` 📝 Working Note `{fname}`** *(unverified note)*")
                    st.code(f"{src.get('title') or fname}: {str(src.get('excerpt') or '')[:260]}", language="text")

    def _render_grounding_badge(grounding: dict | None):
        if not grounding:
            return
        cited = grounding.get("cited_sources") or []
        invalid = grounding.get("invalid_citations") or []
        uncited = grounding.get("uncited_sentences") or []
        ft_ms = grounding.get("first_token_ms")
        ft_txt = f" · first token {ft_ms} ms" if ft_ms is not None else ""
        if grounding.get("fully_cited"):
            st.success(
                f"🛡️ **Grounding Audit Passed (Synthesis)**: Every factual sentence cites valid vault sources "
                f"({', '.join(f'`[{c}]`' for c in cited)}){ft_txt}."
            )
        else:
            details = []
            if cited:
                details.append(f"cited: {', '.join(f'`[{c}]`' for c in cited)}")
            if invalid:
                details.append(f"invalid tags: {', '.join(f'`[{c}]`' for c in invalid)}")
            if uncited:
                details.append(f"{len(uncited)} uncited sentence(s)")
            st.warning(
                f"⚠️ **Grounding Audit Notice (Synthesis)**: {'; '.join(details) or 'no `[V#]`/`[N#]` citations emitted'}{ft_txt}."
            )
            if uncited:
                with st.expander("Inspect Uncited Synthesis Sentences", expanded=False):
                    for s in uncited:
                        st.caption(f"• {s}")

    # Display chat messages
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant":
                if msg.get("sources"):
                    _render_source_cards(msg["sources"], msg.get("retrieval_ms"))
                if msg.get("claims") and msg.get("draft_json") is not None and msg.get("retrieved_atoms") is not None:
                    replay = validate_standard_answer(
                        msg["draft_json"],
                        msg["retrieved_atoms"],
                        atoms_dir=ATOMS_DIR,
                        mode=msg.get("answer_mode", "standard"),
                    )
                    st.text(replay.text)
                elif msg.get("mode") == "stream":
                    st.caption("🔬 *Streamed synthesis over `[V#]` verified claims & `[N#]` working notes (not canonical verified fact)*")
                    st.markdown(_defang_markdown_preview(str(msg.get("content", ""))))
                    _render_grounding_badge(msg.get("grounding"))
                else:
                    st.text(msg.get("content", answer_from_memory("", lambda: "")))
                if msg.get("research_leads_text"):
                    _render_research_leads_box(
                        msg.get("research_leads_text"),
                        msg.get("research_leads_label") or "UNVERIFIED LEADS — NOT OWNER-APPROVED",
                    )
                if msg.get("verified_claims_detail"):
                    with st.expander("🔏 Cryptographic Evidence Anchors", expanded=False):
                        st.json(msg["verified_claims_detail"])
            else:
                st.text(msg.get("content", ""))

    # Handle input
    if user_prompt := st.chat_input("Ask your second brain anything..."):
        st.session_state.chat_history.append({"role": "user", "content": user_prompt})
        with st.chat_message("user"):
            st.text(user_prompt)

        with st.chat_message("assistant"):
            project_tuple = tuple(p.strip() for p in rag_project_scope.split(",") if p.strip())
            primary_scope = project_tuple[0] if project_tuple else None

            if selected_chat_mode in CHAT_MODES:
                sources_list: list[dict] = []
                retrieval_ms: float | None = None
                grounding_event: dict | None = None
                verified_detail: list[dict] = []
                research_leads_text: str = ""
                research_leads_label: str = "UNVERIFIED LEADS — NOT OWNER-APPROVED"
                final_text = ""

                if selected_chat_mode == "stream":
                    st.caption("🔬 *Streamed synthesis over `[V#]` verified claims & `[N#]` working notes (not canonical verified fact)*")
                stream_box = st.empty()
                token_chunks: list[str] = []

                chat_paths = ChatPaths(
                    brain_dir=BRAIN_DIR,
                    atoms_dir=ATOMS_DIR,
                    lexical_index_path=LEXICAL_INDEX_PATH,
                    chroma_path=CHROMA_PATH,
                    telemetry_db_path=TELEMETRY_DB_PATH,
                )
                chat_sig = inspect.signature(chat_events).parameters
                chat_kwargs: dict[str, object] = {
                    "mode": selected_chat_mode,
                    "paths": chat_paths,
                    "project_scope": primary_scope,
                    "model_override": st_model if selected_chat_mode != "instant" else None,
                    "include_preferences": (rag_answer_view == "preference"),
                }
                if "answer_view" in chat_sig:
                    chat_kwargs["answer_view"] = rag_answer_view
                if "allow_unverified_leads" in chat_sig:
                    chat_kwargs["allow_unverified_leads"] = (rag_answer_view == "research")
                if "retrieval_mode" in chat_sig:
                    chat_kwargs["retrieval_mode"] = rag_retrieval_mode
                if "host_id" in chat_sig:
                    chat_kwargs["host_id"] = chat_host_id

                for ev in chat_events(user_prompt, **chat_kwargs):
                    etype = ev.get("type")
                    if etype == "context":
                        sources_list = list(ev.get("sources") or [])
                        retrieval_ms = ev.get("retrieval_ms")
                        if ev.get("warning"):
                            st.warning(str(ev["warning"]))
                        _render_source_cards(sources_list, retrieval_ms)
                        if selected_chat_mode == "instant" and sources_list:
                            lines = [
                                f"Instant recall returned {len(sources_list)} source card(s) in {retrieval_ms} ms:"
                            ]
                            for src in sources_list:
                                body_txt = src.get("claim") or src.get("excerpt") or ""
                                lines.append(f"  [{src.get('citation_id')}] ({src.get('tier')}) {src.get('filename')}: {body_txt[:220]}")
                            final_text = "\n".join(lines)
                            stream_box.text(final_text)
                    elif etype == "research_leads":
                        research_leads_label = str(ev.get("label") or "UNVERIFIED LEADS — NOT OWNER-APPROVED")
                        ev_leads = list(ev.get("leads") or [])
                        research_leads_text = str(ev.get("text") or format_research_leads(ev_leads))
                    elif etype == "token":
                        piece = str(ev.get("text") or ev.get("token") or "")
                        token_chunks.append(piece)
                        stream_box.markdown(_defang_markdown_preview("".join(token_chunks) + " ▌"))
                    elif etype == "grounding":
                        grounding_event = dict(ev)
                    elif etype == "verified_answer":
                        final_text = str(ev.get("text") or ev.get("answer") or "")
                        verified_detail = list(ev.get("claims") or [])
                        stream_box.text(final_text)
                        if verified_detail:
                            with st.expander("🔏 Cryptographic Evidence Anchors", expanded=False):
                                st.json(verified_detail)
                    elif etype == "abstain":
                        final_text = str(ev.get("text") or ev.get("reason") or "")
                        stream_box.info(final_text)
                    elif etype == "error":
                        err_msg = f"Chat error: {ev.get('error')}"
                        final_text = err_msg
                        stream_box.error(err_msg)

                if selected_chat_mode == "stream" and token_chunks:
                    final_text = "".join(token_chunks)
                    stream_box.markdown(_defang_markdown_preview(final_text))
                    _render_grounding_badge(grounding_event)

                if (
                    rag_answer_view == "research"
                    and not research_leads_text
                    and "allow_unverified_leads" not in chat_sig
                ):
                    active_rule = policies_map.get(chat_host_id)
                    if active_rule is None or active_rule.transport != "online":
                        fb_leads = _collect_research_leads(user_prompt, project_scope=primary_scope)
                        research_leads_text = format_research_leads(fb_leads)

                if research_leads_text:
                    _render_research_leads_box(research_leads_text, research_leads_label)

                st.session_state.chat_history.append({
                    "role": "assistant",
                    "mode": selected_chat_mode,
                    "answer_mode": rag_answer_view,
                    "content": final_text,
                    "sources": sources_list,
                    "retrieval_ms": retrieval_ms,
                    "grounding": grounding_event,
                    "verified_claims_detail": verified_detail,
                    "research_leads_text": research_leads_text,
                    "research_leads_label": research_leads_label,
                    "claims": (),
                })
            else:
                # Strict Schema-Validated Host Answer (supports any owner-configured host in host-sharing-policy.json)
                context_profile_tuple = tuple(c.strip() for c in rag_context_profile.split(",") if c.strip())
                extra_recall_kwargs = {}
                if rag_fitness_boost:
                    extra_recall_kwargs["enable_fitness_boost"] = True
                if context_profile_tuple:
                    extra_recall_kwargs["active_context_profile"] = context_profile_tuple
                with st.spinner(f"🧠 Retrieving verified atoms & querying `{chat_host_id}`..."):
                    direct_refs, neighbor_refs = search_memory(
                        user_prompt,
                        n_results=rag_n_results,
                        follow_links=rag_follow,
                        projects=project_tuple,
                        retrieval_mode=rag_retrieval_mode,
                        allow_cross_project=rag_cross_project,
                        **extra_recall_kwargs,
                    )

                all_refs = direct_refs + neighbor_refs
                try:
                    record_retrieval_telemetry(
                        [item["filename"] for item in direct_refs],
                        candidate_atom_ids=peek_lexical_candidate_atom_ids(
                            user_prompt, index_path=LEXICAL_INDEX_PATH, atoms_dir=ATOMS_DIR
                        ),
                        mode=rag_retrieval_mode,
                        db_path=TELEMETRY_DB_PATH,
                    )
                except Exception:
                    pass

                host_rule = policies_map.get(chat_host_id)
                strict_leads_text = ""
                if rag_answer_view == "research" and (host_rule is None or host_rule.transport != "online"):
                    strict_leads = _collect_research_leads(user_prompt, project_scope=primary_scope)
                    strict_leads_text = format_research_leads(strict_leads)

                if not all_refs:
                    abstain_txt = answer_from_memory("", lambda: "")
                    st.warning("⚠ No relevant owner-approved memory found. Abstaining.")
                    st.text(abstain_txt)
                    if strict_leads_text:
                        _render_research_leads_box(strict_leads_text)
                    try:
                        record_grounded_answer_telemetry(
                            [], abstained=True, mode=rag_answer_view, db_path=TELEMETRY_DB_PATH
                        )
                    except Exception:
                        pass
                    st.session_state.chat_history.append({
                        "role": "assistant",
                        "mode": "strict_audit_answer",
                        "answer_mode": rag_answer_view,
                        "content": abstain_txt,
                        "research_leads_text": strict_leads_text,
                        "research_leads_label": "UNVERIFIED LEADS — NOT OWNER-APPROVED",
                        "claims": (),
                    })
                else:
                    try:
                        req = AnswerRequest(query=user_prompt, mode=rag_answer_view, result_limit=rag_n_results)
                        answer = generate_grounded_answer_via_host(
                            req,
                            all_refs,
                            host_id=chat_host_id,
                            model_override=st_model,
                            atoms_dir=ATOMS_DIR,
                            brain_dir=BRAIN_DIR,
                        )
                        scoped_refs = filter_atoms_for_host(all_refs, host_rule, mode=rag_answer_view) if host_rule else all_refs
                        st.info(
                            f"📚 Retrieved {len(direct_refs)} direct + {len(neighbor_refs)} linked atoms "
                            f"({len(scoped_refs)} permitted for `{chat_host_id}`)."
                        )
                        cited_atoms = [ev.atom_id for cl in answer.claims for ev in cl.evidence]
                        try:
                            record_grounded_answer_telemetry(
                                cited_atoms,
                                abstained=not answer.answerable,
                                mode=rag_answer_view,
                                db_path=TELEMETRY_DB_PATH,
                            )
                        except Exception:
                            pass
                        st.text(answer.text)
                        if strict_leads_text:
                            _render_research_leads_box(strict_leads_text)
                        replay_draft = json.dumps({"claims": [c.text for c in answer.claims]}, ensure_ascii=False)
                        st.session_state.chat_history.append({
                            "role": "assistant",
                            "mode": "strict_audit_answer",
                            "content": answer.text if not answer.answerable else "",
                            "claims": answer.claims,
                            "draft_json": replay_draft if answer.answerable else None,
                            "retrieved_atoms": scoped_refs if answer.answerable else None,
                            "answer_mode": rag_answer_view,
                            "research_leads_text": strict_leads_text,
                            "research_leads_label": "UNVERIFIED LEADS — NOT OWNER-APPROVED",
                        })
                    except HostConsentError as exc:
                        err_txt = f"Host policy blocked: {exc}"
                        st.error(err_txt)
                        try:
                            record_grounded_answer_telemetry(
                                [], abstained=True, mode=rag_answer_view, db_path=TELEMETRY_DB_PATH
                            )
                        except Exception:
                            pass
                        st.session_state.chat_history.append({
                            "role": "assistant",
                            "mode": "strict_audit_answer",
                            "content": err_txt,
                            "claims": (),
                        })
                    except Exception as exc:
                        st.error(f"Error invoking AI host `{chat_host_id}`: {exc}")


# PAGE 2: KNOWLEDGE GRAPH
elif nav_selection == "🕸️ Knowledge Graph":
    st.title("🕸️ Knowledge Graph")
    st.subheader("Verified Semantic Lineage, Contradictions & Synthesis Threads")

    col1, col2 = st.columns([3, 1])

    with col2:
        with st.container(border=True):
            st.markdown("### Graph Controls")
            graph_dim = st.radio("Dimension", ["2D", "3D"], horizontal=True)
            kg_project_scope = st.text_input("Project Scope Filter", placeholder="e.g. project:second_brain", key="kg_proj_filter")
            kg_inc_pending = st.checkbox("Include Pending Drafts", value=False, key="kg_inc_pending")
            kg_inc_overlap = st.checkbox("Show Topic-Overlap Edges", value=True, key="kg_inc_overlap")
            kg_inc_threads = st.checkbox("Overlay Synthesis Threads (`3-threads/`)", value=True, key="kg_inc_threads")

            st.markdown("### Node & Edge Legend")
            st.markdown(
                "🟢 **Verified Approved Atom**  \n"
                "🟡 **Pending Draft Atom**  \n"
                "🔴 **Needs Edit Atom**  \n"
                "⚪ **Superseded Lineage Atom**  \n"
                "🔵 **Synthesis Thread (`3-threads/`)**  \n"
                "*Rejected & quarantined atoms are strictly excluded by default.*  \n\n"
                "**Edges:**  \n"
                "— `link` (cyan solid)  \n"
                "-- `superseded_by` (amber dashed)  \n"
                "·· `contested_by` (magenta dotted)  \n"
                "·· `topic_overlap` (purple dotted)"
            )

    with col1:
        with st.spinner("Building interactive knowledge graph..."):
            G = build_network_graph(
                project_scope=kg_project_scope.strip() or None,
                include_pending=kg_inc_pending,
                include_threads=kg_inc_threads,
                include_topic_overlap=kg_inc_overlap,
            )
            kg_meta = G.graph.get("kg_summary", {})
            km1, km2, km3, km4 = st.columns(4)
            km1.metric("Rendered Nodes", G.number_of_nodes())
            km2.metric("Rendered Edges", G.number_of_edges())
            km3.metric("Orphan Approved", len(kg_meta.get("orphan_approved_atoms", [])))
            km4.metric("Contested Pairs", G.graph.get("contradiction_count", 0))

            if G.nodes():
                fig = draw_network_plotly(G, dimension=graph_dim)
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": True})
            else:
                st.info("No verified (or pending) nodes match the active graph filter.")

    st.divider()

    # Interactive node inspector
    st.markdown("### 🔍 Node Inspector")
    if G.nodes():
        selected_node = st.selectbox("Select a Node to Inspect", sorted(list(G.nodes())))
        if selected_node:
            ndata = G.nodes[selected_node]
            node_type = ndata.get("type", "atom")
            sub_folder = THREADS_DIR if node_type == "thread" else ATOMS_DIR
            node_path = safe_file_in_dir(sub_folder, selected_node, allowed_exts={".md"})

            if node_path and os.path.isfile(node_path) and not _is_reparse_or_link(node_path):
                with open(node_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()

                col_n1, col_n2 = st.columns([2, 1])
                with col_n1:
                    st.markdown(f"#### Content of `{selected_node}`")
                    st.code(content, language="markdown")
                with col_n2:
                    st.markdown("#### Metadata & Typed Relations")
                    incident_edges = [
                        {
                            "peer": v if u == selected_node else u,
                            "kind": edata.get("kind", "link"),
                        }
                        for u, v, edata in G.edges(selected_node, data=True)
                    ]
                    st.json({
                        "file": selected_node,
                        "type": node_type,
                        "approved_current": ndata.get("approved_current", False),
                        "review_status": ndata.get("review_status", ""),
                        "memory_type": ndata.get("memory_type", ""),
                        "superseded_by": ndata.get("superseded_by", ""),
                        "contexts": ndata.get("tags", []),
                        "relations": incident_edges,
                    })
            else:
                st.error("Selected node path is unavailable or unsafe.")


# PAGE 3: VAULT EXPLORER
elif nav_selection == "🗄️ Vault Explorer":
    st.title("🗄️ Vault Explorer")
    st.subheader("Inspect every vault tier; canonical atom edits return to pending review")

    vault_tab = st.selectbox(
        "Vault Category",
        [
            "2-atoms (Atoms)",
            "notes (Working Notes)",
            "5-playbooks (Skill Playbooks)",
            "3-threads (Threads)",
            "4-experiences (Experience Records)",
            "sources (Web Pages)",
            "0-raw (Raw Captures)",
            "0-raw/inbox (Quarantined MCP Inbox)",
        ],
    )

    folder_map = {
        "2-atoms (Atoms)": ATOMS_DIR,
        "notes (Working Notes)": NOTES_DIR,
        "5-playbooks (Skill Playbooks)": PLAYBOOKS_DIR,
        "3-threads (Threads)": THREADS_DIR,
        "4-experiences (Experience Records)": EXPERIENCES_DIR,
        "sources (Web Pages)": SOURCES_DIR,
        "0-raw (Raw Captures)": RAW_DIR,
        "0-raw/inbox (Quarantined MCP Inbox)": INBOX_DIR,
    }
    dir_path = folder_map[vault_tab]

    if not os.path.exists(dir_path) or _is_reparse_or_link(dir_path):
        st.info("This vault category has no files yet.")
    else:
        allowed_exts = {".md", ".intake"} if dir_path == INBOX_DIR else {".md"}
        files = sorted([
            f for f in os.listdir(dir_path)
            if os.path.isfile(os.path.join(dir_path, f))
            and not _is_reparse_or_link(os.path.join(dir_path, f))
            and any(f.endswith(ext) for ext in allowed_exts)
            and f != "README.md"
            and not (dir_path == RAW_DIR and f == "reading-list.md")
        ])
        search_query = st.text_input("🔍 Search filenames", "")

        if search_query:
            files = [f for f in files if search_query.lower() in f.lower()]

        if not files:
            st.warning("No files matched the search criteria.")
        else:
            col_list, col_view = st.columns([1, 2])

            with col_list:
                st.markdown("#### File List")
                selected_file = st.radio("Select File", files, label_visibility="collapsed")

            with col_view:
                if selected_file:
                    file_path = safe_file_in_dir(dir_path, selected_file, allowed_exts={".md", ".intake"})
                    if not file_path or _is_reparse_or_link(file_path):
                        st.error("Selected note path is invalid.")
                        st.stop()
                    raw_file_bytes = Path(file_path).read_bytes()
                    file_content = raw_file_bytes.decode("utf-8", errors="replace")
                    file_sha256_hex = hashlib.sha256(raw_file_bytes).hexdigest()

                    st.markdown(f"### Preview: `{selected_file}`")

                    # Only canonical atom revisions may be edited in this UI.
                    if dir_path == ATOMS_DIR:
                        preview_mode = st.radio("View Mode", ["Preview", "Edit Source Code"], horizontal=True)
                    elif dir_path == INBOX_DIR:
                        preview_mode = "Preview"
                        st.caption("Quarantined MCP captures in `0-raw/inbox/` are read-only here; use the Quarantine tab to inspect and promote to `0-raw/`.")
                    elif dir_path == NOTES_DIR:
                        preview_mode = "Preview"
                        st.caption("Working notes in `notes/` are read-only in Vault Explorer; edit or verify excerpts in `📝 Working Notes & Import`.")
                    else:
                        preview_mode = "Preview"
                        st.caption("Raw captures, playbooks, experiences, source evidence, and synthesis threads are read-only here.")

                    if preview_mode == "Preview":
                        with st.container(border=True):
                            st.code(file_content, language="markdown")
                    else:
                        edited_content = st.text_area("Source Code", file_content, height=450)
                        if st.button("💾 Save Changes"):
                            try:
                                apply_atom_edit(
                                    atom_path=file_path,
                                    edited_content=edited_content,
                                    expected_sha256=file_sha256_hex,
                                    actor="local_dashboard_owner",
                                    brain_dir=BRAIN_DIR,
                                    atoms_dir=ATOMS_DIR,
                                )
                                st.session_state["_dashboard_flash_success"] = (
                                    "Atom saved as a new pending revision; recall index refreshed."
                                )
                            except Exception as exc:
                                st.error(f"Atom save failed: {exc}")
                                st.stop()
                            st.rerun()

                    if dir_path == ATOMS_DIR and preview_mode == "Preview":
                        record = None
                        current_approval = False
                        try:
                            record = parse_atom_record(file_path)
                            current_approval = approved_revision_is_current(
                                file_path, brain_dir=BRAIN_DIR, atoms_dir=ATOMS_DIR
                            )
                            approval_text = "verified current owner approval" if current_approval else f"{record['review_status']} (not eligible for recall)"
                            review_details = atom_review_details(
                                file_content,
                                atom_path=file_path,
                                atoms_dir=ATOMS_DIR,
                                brain_dir=BRAIN_DIR,
                            )
                            st.caption(f"Lifecycle: {approval_text} · atom SHA-256: `{review_details['atom_sha256']}`")
                            st.markdown("#### Evidence and trust status")
                            st.caption(f"Stored evidence excerpt: {review_details['stored_quote'] or '(missing)'}")
                            anchor = review_details["anchor"]
                            if all(anchor.values()):
                                st.caption(f"Stored anchor: `{anchor['evidence_source']}` · bytes {anchor['evidence_start']}–{anchor['evidence_end']} · source SHA-256 `{anchor['evidence_sha256']}`")
                                source_state = "current" if review_details["anchor_current"] and review_details["current_source_sha256"] == anchor["evidence_sha256"] else "STALE / UNVERIFIABLE"
                                st.caption(f"Current source SHA-256: `{review_details['current_source_sha256'] or 'unavailable'}` · anchor {source_state}")
                                if review_details["current_quote"] is not None:
                                    st.code(review_details["current_quote"] or "(empty source slice)", language=None)
                            else:
                                st.warning(review_details["source_error"] or "No complete verified source anchor is stored on this atom.")
                            if review_details["source_error"] and all(anchor.values()):
                                st.error(f"Evidence source unavailable or unsafe: {review_details['source_error']}")

                            st.markdown("#### Related atoms")
                            if review_details["related_atoms"]:
                                for related in review_details["related_atoms"]:
                                    if related.get("error"):
                                        st.caption(f"`{related['filename']}` · {related['error']}")
                                    else:
                                        st.caption(f"`{related['filename']}` · {related['review_status']} · {related['claim']}")
                            else:
                                st.caption("No related atom links recorded.")
                        except Exception as exc:
                            st.warning(f"Atom metadata could not be validated: {exc}")
                        st.markdown("#### Owner review actions")
                        st.caption(
                            "Consequences: **Approve** anchors exact source bytes for trusted recall; "
                            "**Quarantine** flags `needs_edit` and blocks recall; **Demote** resets to `pending` "
                            "for re-verification; **Reject** permanently excludes this revision while keeping it auditable."
                        )
                        approve_col, quarantine_col, demote_col, reject_col = st.columns(4)
                        expected_hash = file_sha256_hex
                        if record and approve_col.button("Approve exact revision", key=f"approve-{selected_file}"):
                            try:
                                apply_review_transition(
                                    atom_path=file_path,
                                    action="approve",
                                    expected_sha256=expected_hash,
                                    actor="local_dashboard_owner",
                                    brain_dir=BRAIN_DIR,
                                    atoms_dir=ATOMS_DIR,
                                )
                                st.session_state["_dashboard_flash_success"] = (
                                    "Atom approved; source evidence verified and recall index refreshed."
                                )
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Approval failed: {exc}")
                        if record and quarantine_col.button("Quarantine (needs_edit)", key=f"quarantine-{selected_file}"):
                            try:
                                apply_review_transition(
                                    atom_path=file_path,
                                    action="quarantine",
                                    expected_sha256=expected_hash,
                                    actor="local_dashboard_owner",
                                    brain_dir=BRAIN_DIR,
                                    atoms_dir=ATOMS_DIR,
                                )
                                st.session_state["_dashboard_flash_success"] = (
                                    "Atom quarantined as needs_edit and excluded from recall."
                                )
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Quarantine failed: {exc}")
                        if record and demote_col.button("Demote to pending", key=f"demote-{selected_file}"):
                            try:
                                apply_review_transition(
                                    atom_path=file_path,
                                    action="demote",
                                    expected_sha256=expected_hash,
                                    actor="local_dashboard_owner",
                                    brain_dir=BRAIN_DIR,
                                    atoms_dir=ATOMS_DIR,
                                )
                                st.session_state["_dashboard_flash_success"] = (
                                    "Atom demoted to pending; inspect current source evidence before re-approval."
                                )
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Demotion failed: {exc}")
                        if record and reject_col.button("Reject exact revision", key=f"reject-{selected_file}"):
                            try:
                                apply_review_transition(
                                    atom_path=file_path,
                                    action="reject",
                                    expected_sha256=expected_hash,
                                    actor="local_dashboard_owner",
                                    brain_dir=BRAIN_DIR,
                                    atoms_dir=ATOMS_DIR,
                                )
                                st.session_state["_dashboard_flash_success"] = (
                                    "Atom rejected and recall index refreshed."
                                )
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Rejection failed: {exc}")

                        with st.expander("🔀 Merge / Supersede Another Atom Into This Survivor", expanded=False):
                            st.caption(
                                "Consequence: Both files are preserved on disk with CAS verification. "
                                "The selected secondary atom becomes `merged` (`superseded_by` this survivor) and leaves recall; "
                                "this survivor atom moves to `pending` so you can review the combined claim before approval."
                            )
                            other_atoms = [
                                f for f in sorted(os.listdir(ATOMS_DIR))
                                if is_content_markdown(f) and f != selected_file
                            ]
                            if other_atoms:
                                merge_target = st.selectbox("Atom to supersede (retire)", other_atoms, key=f"merge-target-{selected_file}")
                                merge_rationale = st.text_input("Merge rationale (required)", key=f"merge-rationale-{selected_file}")
                                if st.button("Execute Two-Atom CAS Merge", key=f"merge-btn-{selected_file}"):
                                    target_path = safe_file_in_dir(ATOMS_DIR, merge_target, allowed_exts={".md"})
                                    if not target_path or not merge_rationale.strip():
                                        st.error("Select a valid target atom and provide a non-empty merge rationale.")
                                    else:
                                        try:
                                            with open(target_path, "rb") as tf:
                                                target_hash = hashlib.sha256(tf.read()).hexdigest()
                                            apply_atom_merge(
                                                survivor_path=file_path,
                                                survivor_expected_sha256=expected_hash,
                                                superseded_path=target_path,
                                                superseded_expected_sha256=target_hash,
                                                actor="local_dashboard_owner",
                                                rationale=merge_rationale.strip(),
                                                brain_dir=BRAIN_DIR,
                                                atoms_dir=ATOMS_DIR,
                                            )
                                            st.session_state["_dashboard_flash_success"] = (
                                                f"Merged `{merge_target}` into survivor `{selected_file}`; survivor queued as pending."
                                            )
                                            st.rerun()
                                        except Exception as exc:
                                            st.error(f"Merge failed: {exc}")
                            else:
                                st.caption("No other atoms available to merge.")

                        with st.expander("🔍 Read-Only Duplicate Suggestions", expanded=False):
                            dupes = [
                                d for d in suggest_duplicate_atoms(ATOMS_DIR, brain_dir=BRAIN_DIR)
                                if selected_file in (d["atom_a"], d["atom_b"])
                            ]
                            if dupes:
                                for d in dupes:
                                    peer = d["atom_b"] if d["atom_a"] == selected_file else d["atom_a"]
                                    peer_status = d["atom_b_status"] if d["atom_a"] == selected_file else d["atom_a_status"]
                                    peer_hash = d["atom_b_sha256"] if d["atom_a"] == selected_file else d["atom_a_sha256"]
                                    peer_claim = d["atom_b_claim"] if d["atom_a"] == selected_file else d["atom_a_claim"]
                                    st.caption(
                                        f"Candidate overlap `{peer}` (Jaccard `{d['jaccard_similarity']:.2f}` · "
                                        f"status `{peer_status}` · SHA-256 `{peer_hash[:12]}…`): {peer_claim}"
                                    )
                            else:
                                st.caption("No duplicate candidates above threshold for this atom.")


# PAGE 4: INBOX & WEB SCOUT
elif nav_selection == "📥 Inbox & Web Scout":
    st.title("📥 Inbox & Web Scout")
    st.subheader("Feed Your AI Engine")

    col_inbox, col_scout = st.columns(2)

    with col_inbox:
        with st.container(border=True):
            st.markdown("### 📥 Quick Capture Raw Note (`0-raw/`)")
            inbox_title = st.text_input("Topic/Title (optional)")
            inbox_content = st.text_area("Write note or copy-paste text here...", height=200)
            inbox_source = st.text_input("Source details (web link, books, etc.)", "Web Dashboard")

            if st.button("📥 Add to Raw Captures"):
                if not inbox_content.strip():
                    st.warning("Note content cannot be empty.")
                elif (os.path.lexists(RAW_DIR) and _is_reparse_or_link(RAW_DIR)) or (
                    os.path.lexists(SIDECAR_DIR) and _is_reparse_or_link(SIDECAR_DIR)
                ):
                    st.error("Refusing to write capture through symlinked vault directory.")
                else:
                    os.makedirs(RAW_DIR, exist_ok=True)
                    os.makedirs(SIDECAR_DIR, exist_ok=True)
                    clean_title = re.sub(r"[\x00-\x1f\x7f]+", " ", str(inbox_title or "")).strip()
                    title_slug = make_slug(clean_title)
                    base_stamp = now_slug()
                    filename = f"{base_stamp}-dashboard-{title_slug}.md"

                    safe_inbox_source = (
                        re.sub(r"[\x00-\x1f\x7f]+", " ", str(inbox_source or ""))
                        .replace("--", "—")
                        .replace(">", "")
                        .replace("<", "")
                        .strip()[:200]
                    )
                    normalized_body = (
                        inbox_content.replace("\r\n", "\n")
                        .replace("\r", "\n")
                        .replace("\x00", "")
                        .rstrip("\n")
                    )
                    content = (
                        "<!-- second_brain_origin: user_local -->\n"
                        f"<!-- Capture via Dashboard on {today()} -->\n"
                        f"<!-- Source: {safe_inbox_source} -->\n\n"
                        f"{normalized_body}\n"
                    )
                    encoded_content = content.encode("utf-8")
                    suffix = 1
                    wrote_ok = False
                    with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
                        if _is_reparse_or_link(RAW_DIR):
                            st.error("Refusing to write capture through symlinked 0-raw directory.")
                        else:
                            while True:
                                filepath = safe_file_in_dir(RAW_DIR, filename, allowed_exts={".md"}, must_exist=False)
                                if not filepath:
                                    st.error("Invalid capture filename.")
                                    break
                                if os.path.lexists(filepath):
                                    filename = f"{base_stamp}-dashboard-{title_slug}-{suffix}.md"
                                    suffix += 1
                                    continue
                                fd, tmp_path = tempfile.mkstemp(dir=RAW_DIR, prefix=".capture-", suffix=".tmp")
                                fd_closed = False
                                try:
                                    if os.write(fd, encoded_content) != len(encoded_content):
                                        raise OSError("Incomplete capture write.")
                                    os.fsync(fd)
                                    os.close(fd)
                                    fd_closed = True
                                    os.replace(tmp_path, filepath)
                                    wrote_ok = True
                                finally:
                                    if not fd_closed:
                                        try:
                                            os.close(fd)
                                        except OSError:
                                            pass
                                    if os.path.exists(tmp_path):
                                        try:
                                            os.remove(tmp_path)
                                        except OSError:
                                            pass
                                break
                    if wrote_ok:
                        st.success(f"Added capture → `0-raw/{filename}`")
                        st.balloons()

    with col_scout:
        with st.container(border=True):
            st.markdown("### 🕸️ Web Scout (Add URL)")
            scout_url = st.text_input("Add url to reading list:")

            if st.button("➕ Queue URL"):
                raw_url = str(scout_url or "").strip()
                clean_url = re.sub(r"[\r\n\x00-\x1f\x7f]+", "", raw_url).strip()
                if (
                    not clean_url
                    or clean_url != raw_url
                    or any(ch.isspace() for ch in clean_url)
                    or not clean_url.startswith(("http://", "https://"))
                ):
                    st.warning("Please enter a valid single-line HTTP/HTTPS URL without whitespace or control characters.")
                elif (
                    (os.path.lexists(RAW_DIR) and _is_reparse_or_link(RAW_DIR))
                    or (os.path.lexists(SIDECAR_DIR) and _is_reparse_or_link(SIDECAR_DIR))
                    or (os.path.lexists(READING_LIST) and _is_reparse_or_link(READING_LIST))
                ):
                    st.error("Refusing to write reading list through symlink or reparse point.")
                else:
                    os.makedirs(RAW_DIR, exist_ok=True)
                    os.makedirs(SIDECAR_DIR, exist_ok=True)
                    reading_dir = os.path.dirname(READING_LIST)
                    os.makedirs(reading_dir, exist_ok=True)
                    with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
                        if _is_reparse_or_link(reading_dir) or (
                            os.path.lexists(READING_LIST) and _is_reparse_or_link(READING_LIST)
                        ):
                            st.error("Refusing to write reading list through symlink or reparse point.")
                        else:
                            contents = ""
                            if os.path.isfile(READING_LIST):
                                with open(READING_LIST, "r", encoding="utf-8", errors="replace") as f:
                                    contents = f.read()
                            existing_urls = {
                                m.group(2)
                                for ln in contents.splitlines()
                                if (m := re.match(r"^-\s+\[(.)\]\s+(\S+)(.*)$", ln.strip()))
                            }
                            if clean_url not in existing_urls:
                                prefix = contents if (not contents or contents.endswith("\n")) else f"{contents}\n"
                                updated_bytes = f"{prefix}- [ ] {clean_url}\n".encode("utf-8")
                                fd, tmp_path = tempfile.mkstemp(dir=reading_dir, prefix=".reading-list-", suffix=".tmp")
                                fd_closed = False
                                try:
                                    if os.write(fd, updated_bytes) != len(updated_bytes):
                                        raise OSError("Incomplete reading-list write.")
                                    os.fsync(fd)
                                    os.close(fd)
                                    fd_closed = True
                                    os.replace(tmp_path, READING_LIST)
                                finally:
                                    if not fd_closed:
                                        try:
                                            os.close(fd)
                                        except OSError:
                                            pass
                                    if os.path.exists(tmp_path):
                                        try:
                                            os.remove(tmp_path)
                                        except OSError:
                                            pass
                                st.success("URL added to reading list!")
                            else:
                                st.info("URL is already in the reading list.")

            st.markdown("### 📋 Current Reading List")
            if os.path.isfile(READING_LIST) and not _is_reparse_or_link(READING_LIST):
                with open(READING_LIST, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()

                parsed_list = []
                for line in lines:
                    m = re.match(r'^-\s+\[(.)\]\s+(\S+)(.*)$', line.strip())
                    if m:
                        status_char, url, notes = m.groups()
                        status = "ok" if status_char == "x" else "fail" if status_char == "!" else "pending"
                        parsed_list.append({"URL": url, "Status": status, "Details": notes.strip()})

                if parsed_list:
                    df = pd.DataFrame(parsed_list)
                    st.dataframe(df, width="stretch")
                else:
                    st.text("Reading list is currently empty.")
            else:
                st.text("No reading list found. Add a URL to create it.")

    # Trigger Web Scout button
    st.divider()
    st.markdown("### 🚀 Synchronize Web Scout")
    st.markdown("Run the Scout script to crawl all queued `[ ]` URLs, extract article text, and place them in the ingest pipeline.")

    if st.button("🕸️ Run Web Scout Now"):
        proc = run_script_process("scripts/ollama_scout.py")
        stream_logs_ui(proc, "Active Web Scout running")


# PAGE 4B: HOST API, QUARANTINE & COGNITIVE FITNESS
elif nav_selection == "🧬 Host API, Quarantine & Fitness":
    st.title("🧬 Host Ecosystem, Quarantine & Cognitive Core")
    tab_quarantine, tab_hosts, tab_fitness, tab_playbooks, tab_planner = st.tabs([
        "🛡️ Quarantined Inbox (0-raw/inbox)",
        "🔌 Hardware Tier & AI Host Consent",
        "🧠 Cognitive Fitness & Experiences",
        "📘 Skill Playbooks",
        "🧭 Executive Planner",
    ])

    with tab_quarantine:
        st.subheader("Quarantined Captures Awaiting Owner Promotion")
        st.caption("Captures from local MCP, Loopback Memory API, and Experience Proposals remain isolated in `0-raw/inbox/` until CAS-verified promotion.")
        q_items = list_inbox_items(INBOX_DIR, raw_dir=RAW_DIR, brain_dir=BRAIN_DIR)
        if not q_items:
            st.success("Quarantined inbox is clean (0 pending captures).")
        else:
            selected_q = st.selectbox("Select Quarantined Capture", [item.filename for item in q_items])
            if selected_q:
                q_detail = inspect_inbox_item(selected_q, inbox_dir=INBOX_DIR, raw_dir=RAW_DIR, brain_dir=BRAIN_DIR)
                st.write(f"**SHA-256:** `{q_detail['sha256']}` | **Origin:** `{q_detail['origin']}` | **Size:** `{q_detail['size_bytes']}` bytes")
                if q_detail["risk_flags"]:
                    st.warning(f"Security Risk Flags Detected: {', '.join(q_detail['risk_flags'])}")
                st.code(str(q_detail["content"]), language="markdown")

                actor_name = st.text_input("Owner Actor Identity", value="owner", key="inbox_actor_input")
                ack_risks = False
                if q_detail["risk_flags"]:
                    ack_risks = st.checkbox("I have inspected the raw capture above and acknowledge the flagged risks (forged frontmatter and beacons will be stripped/defanged).")
                col_p, col_r = st.columns(2)
                with col_p:
                    if st.button("✅ Promote to 0-raw/ (CAS-Verified)", key="btn_promote_inbox"):
                        try:
                            dec = promote_inbox_item(
                                selected_q,
                                expected_sha256=str(q_detail["sha256"]),
                                actor=actor_name,
                                acknowledge_risks=ack_risks,
                                inbox_dir=INBOX_DIR,
                                raw_dir=RAW_DIR,
                                brain_dir=BRAIN_DIR,
                            )
                            st.session_state["_dashboard_flash_success"] = (
                                f"Promoted to `0-raw/{dec['promoted_filename']}` (SHA-256 `{dec['promoted_sha256']}`)."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Promotion blocked: {exc}")
                with col_r:
                    if st.button("🗑️ Reject & Archive Capture", key="btn_reject_inbox"):
                        try:
                            reject_inbox_item(
                                selected_q,
                                expected_sha256=str(q_detail["sha256"]),
                                actor=actor_name,
                                inbox_dir=INBOX_DIR,
                                raw_dir=RAW_DIR,
                                brain_dir=BRAIN_DIR,
                            )
                            st.session_state["_dashboard_flash_success"] = f"Rejected and archived `{selected_q}`."
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Rejection failed: {exc}")

    with tab_hosts:
        st.subheader("Hardware Capability Profile")
        hw = detect_hardware_profile(ollama_timeout=2.0, use_cache=True)
        st.info(
            f"**{hw.tier_label}** (`{hw.tier_id}`)\n\n"
            f"- **Memory / Compute:** {hw.available_ram_gib:.1f} GiB free / {hw.total_ram_gib:.1f} GiB total | {hw.gpu_name}\n"
            f"- **Recommended Model:** `{hw.recommended_generation_model}` (Installed match: `{hw.selected_installed_generation_model or 'none'}`)\n"
            f"- **Recommended Retrieval:** `{hw.recommended_retrieval_mode}` (P95 target <= {hw.retrieval_p95_slo_ms:.0f} ms)\n"
            f"- **Rationale:** {hw.rationale}"
        )

        st.divider()
        st.subheader("Per-Host AI Data-Sharing Consent Policy")
        st.caption(
            "Online hosts are disabled by default, require HTTPS + public DNS validation, enforce PII/credential redaction, "
            "and read credentials exclusively from environment variable names (raw API keys are never stored or displayed)."
        )
        policies = load_host_policies()
        chosen_host = st.selectbox("Select AI Host Profile", sorted(policies.keys()), key="host_policy_select")
        if chosen_host:
            rule = policies[chosen_host]
            is_online_host = rule.transport == "online"
            en_val = st.checkbox("Enable Context Sharing with this Host", value=rule.enabled, key=f"en_{chosen_host}")
            ep_val = st.text_input("Endpoint URL", value=rule.endpoint_url, key=f"ep_{chosen_host}")
            scopes_text = st.text_input(
                "Allowed Project Scopes (comma-separated, e.g. `project:second_brain` or `*`)",
                value=", ".join(rule.allowed_projects),
                key=f"sc_{chosen_host}",
            )
            hc1, hc2 = st.columns(2)
            with hc1:
                unscoped_val = st.checkbox("Allow Unscoped Atoms", value=rule.allow_unscoped_atoms, key=f"un_{chosen_host}")
                pref_val = st.checkbox("Allow Owner Subjective Preference Atoms", value=rule.allow_preference_atoms, key=f"pr_{chosen_host}")
            with hc2:
                redact_val = st.checkbox(
                    "Redact Outbound PII" + (" (enforced True on online hosts)" if is_online_host else ""),
                    value=True if is_online_host else bool(rule.redact_pii),
                    disabled=is_online_host,
                    key=f"rp_{chosen_host}",
                )
                fail_secret_val = st.checkbox(
                    "Block Outbound Payload on Secret Detection" + (" (enforced True on online hosts)" if is_online_host else ""),
                    value=True if is_online_host else bool(rule.fail_on_secret_detection),
                    disabled=is_online_host,
                    key=f"fs_{chosen_host}",
                )
            model_val = st.text_input("Default Model Name", value=rule.default_model, key=f"md_{chosen_host}")
            key_env_val = st.text_input(
                "API Key Environment Variable Name (`[A-Z][A-Z0-9_]{1,63}`, never a raw API secret)",
                value=rule.api_key_env or "",
                placeholder="OPENAI_API_KEY" if is_online_host else "(optional for local loopback hosts)",
                key=f"ke_{chosen_host}",
            )
            cand_env_name = key_env_val.strip()
            if not cand_env_name:
                if is_online_host:
                    st.warning("Online hosts require a valid uppercase environment variable name (e.g. `OPENAI_API_KEY`).")
                else:
                    st.caption("Credential environment variable: *(not configured — local unauthenticated endpoint)*")
            elif _API_KEY_ENV_RE.fullmatch(cand_env_name):
                env_is_set = bool((os.environ.get(cand_env_name) or "").strip())
                status_txt = "SET ✅" if env_is_set else "NOT SET ⚠️"
                st.caption(f"Environment variable `{cand_env_name}` status: **{status_txt}** *(raw secret value is never read into UI)*")
            else:
                st.warning("Invalid `api_key_env`: must match uppercase `[A-Z][A-Z0-9_]{1,63}` (never paste a raw API key).")

            if st.button("💾 Save Host Sharing Policy", key=f"save_{chosen_host}"):
                try:
                    new_scopes = tuple(s.strip() for s in scopes_text.split(",") if s.strip())
                    saved_to = set_host_sharing_profile(
                        chosen_host,
                        enabled=en_val,
                        endpoint_url=ep_val.strip() or rule.endpoint_url,
                        default_model=model_val.strip() or rule.default_model,
                        allowed_projects=new_scopes,
                        allow_unscoped_atoms=unscoped_val,
                        allow_preference_atoms=pref_val,
                        redact_pii=True if is_online_host else redact_val,
                        fail_on_secret_detection=True if is_online_host else fail_secret_val,
                        api_key_env=cand_env_name,
                    )
                    st.success(f"Updated policy for `{chosen_host}` ({saved_to}).")
                except Exception as exc:
                    st.error(f"Could not save policy: {exc}")

    with tab_fitness:
        st.subheader("Memory Fitness & Procedural Skill Maturity")
        report = compute_memory_fitness_report(
            atoms_dir=ATOMS_DIR,
            brain_dir=BRAIN_DIR,
            db_path=TELEMETRY_DB_PATH,
        )
        qs = report["query_summary"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Recall Queries", qs["total_recall_events"])
        m2.metric("Answer Turns", qs["total_answer_events"])
        m3.metric("High-Utility Atoms", len(report["high_utility_verified_atoms"]))
        m4.metric("Pending Bottlenecks", len(report["pending_review_bottlenecks"]))

        if report["high_utility_verified_atoms"]:
            st.markdown("#### Verified High-Utility Memories")
            st.dataframe(pd.DataFrame(report["high_utility_verified_atoms"]), width="stretch")
        if report["pending_review_bottlenecks"]:
            st.markdown("#### Highest-Priority Pending Atoms to Review")
            st.dataframe(pd.DataFrame(report["pending_review_bottlenecks"]), width="stretch")
        if report["procedure_maturity"]:
            st.markdown("#### Procedural Maturity Stages (`procedural_draft -> verified_procedure -> skill_candidate`)")
            st.dataframe(pd.DataFrame(report["procedure_maturity"]), width="stretch")

        st.divider()
        st.markdown("#### 🎯 Owner Atom Feedback (`endorsed` / `corrected` / `superseded`)")
        st.caption(
            "Endorse high-value verified atoms, log factual corrections, or record supersession lineage notes. "
            "`superseded` is recorded strictly as supersession (never as a false negative correction); "
            "unacknowledged `corrected` flags automatically pause downstream Skill Playbook eligibility."
        )
        approved_atom_files = []
        if os.path.isdir(ATOMS_DIR) and not _is_reparse_or_link(ATOMS_DIR):
            for fname in sorted(os.listdir(ATOMS_DIR)):
                if is_content_markdown(fname):
                    ap = safe_file_in_dir(ATOMS_DIR, fname, allowed_exts={".md"})
                    if ap and not _is_reparse_or_link(ap) and approved_revision_is_current(
                        ap, brain_dir=BRAIN_DIR, atoms_dir=ATOMS_DIR
                    ):
                        approved_atom_files.append(fname)

        fb_c1, fb_c2, fb_c3 = st.columns([2, 1.2, 1.2])
        with fb_c1:
            if approved_atom_files:
                fb_atom = st.selectbox("Approved Atom", approved_atom_files, key="fb_atom_sel")
            else:
                fb_atom = st.text_input("Approved Atom Filename", placeholder="e.g. structured-logging.md", key="fb_atom_txt")
        with fb_c2:
            fb_kind = st.selectbox("Feedback Signal", ["endorsed", "corrected", "superseded"], key="fb_kind_sel")
        with fb_c3:
            fb_actor = st.text_input("Owner Actor", value="local_dashboard_owner", key="fb_actor_in")
        fb_note = st.text_input("Optional Feedback Note / Context", value="", key="fb_note_in")
        if st.button("🗳️ Record Owner Atom Feedback", key="btn_record_fb"):
            if not str(fb_atom).strip():
                st.warning("Select or enter an approved atom filename.")
            else:
                try:
                    _submit_atom_feedback(
                        str(fb_atom).strip(),
                        fb_kind,
                        actor=fb_actor.strip() or "local_dashboard_owner",
                        note=fb_note.strip(),
                    )
                    st.session_state["_dashboard_flash_success"] = (
                        f"Recorded `{fb_kind}` feedback on `{fb_atom}`."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Could not record atom feedback: {exc}")

        st.divider()
        st.markdown("#### 📝 Log Completed Task Experience (`4-experiences/`)")
        ex_c1, ex_c2 = st.columns([3, 1])
        with ex_c1:
            exp_goal = st.text_input("Task Goal", key="exp_goal")
        with ex_c2:
            exp_success = st.toggle("Task Succeeded", value=True, key="exp_success_toggle")
        exp_res = st.text_area("Outcome Summary", height=90, key="exp_res")
        ex_m1, ex_m2, ex_m3 = st.columns(3)
        with ex_m1:
            exp_ctx = st.text_input("Context Tags (comma-separated)", placeholder="project:second_brain, windows", key="exp_ctx")
        with ex_m2:
            exp_tools = st.text_input("Tools Used (comma-separated)", placeholder="pytest, uv, streamlit", key="exp_tools")
        with ex_m3:
            exp_refs = st.text_input("Referenced Atoms (comma-separated)", placeholder="structured-logging.md", key="exp_refs")
        ex_p1, ex_p2, ex_p3 = st.columns(3)
        with ex_p1:
            exp_plan = st.text_area("Initial Plan Steps (one per line)", height=85, key="exp_plan")
        with ex_p2:
            exp_actions = st.text_area("Ordered Actions Taken (one per line)", height=85, key="exp_actions")
        with ex_p3:
            exp_lessons = st.text_area("Lessons Learned (one per line)", height=85, key="exp_lessons")
        ex_r1, ex_r2 = st.columns(2)
        with ex_r1:
            exp_reasoning = st.text_area("Reasoning Summary (optional)", height=75, key="exp_reasoning")
        with ex_r2:
            exp_reflection = st.text_area("Post-Task Reflection (optional)", height=75, key="exp_reflection")
        ex_cf0, ex_cf1, ex_cf2 = st.columns([1.2, 1.4, 1.4])
        with ex_cf0:
            exp_track_conf = st.checkbox("Record Confidence Calibration", value=False, key="exp_track_conf")
        with ex_cf1:
            exp_conf_before = st.slider(
                "Confidence Before (`0.0–1.0`)",
                min_value=0.0,
                max_value=1.0,
                value=0.5,
                step=0.05,
                disabled=not exp_track_conf,
                key="exp_conf_before",
            )
        with ex_cf2:
            exp_conf_after = st.slider(
                "Confidence After (`0.0–1.0`)",
                min_value=0.0,
                max_value=1.0,
                value=0.8,
                step=0.05,
                disabled=not exp_track_conf,
                key="exp_conf_after",
            )
        exp_proc = st.text_input("Optional Candidate Procedure Title", key="exp_proc")
        exp_prop = st.checkbox("Propose Candidate Procedure to Quarantined `0-raw/inbox/`", value=False, key="exp_prop")
        if st.button("📝 Record Immutable Experience", key="btn_save_exp"):
            try:
                exp_sig = inspect.signature(save_experience_record).parameters
                exp_kwargs: dict[str, object] = {
                    "goal": exp_goal,
                    "result_summary": exp_res,
                    "success": bool(exp_success),
                    "context": [x.strip() for x in exp_ctx.split(",") if x.strip()],
                    "actions": [ln.strip() for ln in exp_actions.splitlines() if ln.strip()],
                    "tools_used": [x.strip() for x in exp_tools.split(",") if x.strip()],
                    "referenced_atoms": [x.strip() for x in exp_refs.split(",") if x.strip()],
                    "lessons": [ln.strip() for ln in exp_lessons.splitlines() if ln.strip()],
                    "candidate_procedure_title": exp_proc or None,
                    "propose_to_inbox": exp_prop,
                    "experiences_dir": EXPERIENCES_DIR,
                    "inbox_dir": INBOX_DIR,
                    "brain_dir": BRAIN_DIR,
                }
                if "plan" in exp_sig:
                    exp_kwargs["plan"] = [ln.strip() for ln in exp_plan.splitlines() if ln.strip()]
                if "reasoning_summary" in exp_sig:
                    exp_kwargs["reasoning_summary"] = exp_reasoning.strip() or None
                if "reflection" in exp_sig:
                    exp_kwargs["reflection"] = exp_reflection.strip() or None
                if "confidence_before" in exp_sig:
                    exp_kwargs["confidence_before"] = float(exp_conf_before) if exp_track_conf else None
                if "confidence_after" in exp_sig:
                    exp_kwargs["confidence_after"] = float(exp_conf_after) if exp_track_conf else None
                saved_exp = save_experience_record(**exp_kwargs)
                st.success(f"Saved `4-experiences/{saved_exp['filename']}` (SHA-256 `{saved_exp['sha256']}`).")
            except Exception as exc:
                st.error(f"Could not save experience: {exc}")

    with tab_playbooks:
        st.subheader("Owner-Gated Skill Playbooks (`brain/5-playbooks/`)")
        st.caption(
            "Playbooks are human-readable operational checklists anchored by exact SHA-256 to approved `procedural` atoms. "
            "They are never executed as code and automatically become inactive if any backing atom is edited, demoted, or receives unacknowledged corrections."
        )

        candidates = inspect_skill_candidates(
            atoms_dir=ATOMS_DIR,
            brain_dir=BRAIN_DIR,
            playbooks_dir=PLAYBOOKS_DIR,
            telemetry_db_path=TELEMETRY_DB_PATH,
        )
        if candidates:
            st.markdown("#### Approved Procedural Atoms & Promotion Readiness")
            st.dataframe(pd.DataFrame(candidates), width="stretch")
        else:
            st.info("No currently owner-approved `procedural` atoms found in `2-atoms/`.")

        with st.expander("➕ Promote Approved Procedural Atom(s) to Skill Playbook (CAS-Locked)", expanded=False):
            cand_map = {str(c["atom_id"]): str(c["sha256"]) for c in candidates}
            selected_backing = st.multiselect(
                "Backing Approved Procedural Atoms (CAS SHA-256 bound)",
                options=sorted(cand_map.keys()),
                key="pb_backing_atoms",
            )
            if selected_backing:
                with st.expander("📋 Copyable Grounded `Claim` Units from Selected Backing Atoms", expanded=True):
                    st.caption(
                        "Each playbook step MUST match an exact whole `Claim`, `Claim` list item, or full `Claim` sentence below "
                        "(unapproved `Evidence` excerpts are never accepted as operational step grounding)."
                    )
                    for aid in selected_backing:
                        claim_units = _extract_backing_claim_units(aid)
                        st.markdown(f"**`{aid}`**")
                        if claim_units:
                            st.code("\n".join(claim_units), language="text")
                        else:
                            st.caption("No Claim lines found.")

            pb_id_in = st.text_input("Playbook ID (slug, e.g. `staged-vector-rebuild`)", key="pb_id_in")
            pb_title_in = st.text_input("Playbook Title", key="pb_title_in")
            pb_actor_in = st.text_input("Human Owner Actor", value="local_dashboard_owner", key="pb_actor_in")
            pb_trigger_in = st.text_input("Trigger & Applicability", key="pb_trigger_in")
            pb_pre_in = st.text_area("Preconditions & Inputs (one per line)", height=80, key="pb_pre_in")
            pb_steps_in = st.text_area(
                "Ordered Steps (one per line — each step must match an exact whole Claim, Claim list item, or full Claim sentence of a selected backing procedural atom; Evidence is never accepted)",
                height=110,
                key="pb_steps_in",
            )
            pb_out_in = st.text_area("Expected Outputs & Verification (one per line)", height=80, key="pb_out_in")
            pb_rec_in = st.text_area("Failure Modes & Rollback (one per line)", height=80, key="pb_rec_in")
            pb_prev_sha = st.text_input(
                "Existing Playbook SHA-256 (leave empty for initial creation; required when updating an existing playbook)",
                value="",
                key="pb_prev_sha",
            )
            pbo1, pbo2, pbo3 = st.columns(3)
            with pbo1:
                pb_early = st.checkbox("Allow Early Owner Promotion (< 3 grounded citations)", value=False, key="pb_early")
            with pbo2:
                pb_ack_corr = st.checkbox("Acknowledge Existing Backing Atom Corrections", value=False, key="pb_ack_corr")
            with pbo3:
                pb_multi_proj = st.checkbox("Allow Multi-Project Backing Scope (`allow_multi_project`)", value=False, key="pb_multi_proj")
            pb_rationale_in = st.text_input("Promotion / Override Rationale", value="", key="pb_rationale_in")

            if st.button("✅ Promote Playbook (CAS-Verified)", key="btn_promote_playbook"):
                try:
                    promoted_res = promote_skill_playbook(
                        playbook_id=pb_id_in,
                        title=pb_title_in,
                        backing_atoms=[(aid, cand_map[aid]) for aid in selected_backing],
                        trigger=pb_trigger_in,
                        preconditions=[ln.strip() for ln in pb_pre_in.splitlines() if ln.strip()],
                        steps=[ln.strip() for ln in pb_steps_in.splitlines() if ln.strip()],
                        expected_outputs=[ln.strip() for ln in pb_out_in.splitlines() if ln.strip()],
                        failure_recovery=[ln.strip() for ln in pb_rec_in.splitlines() if ln.strip()],
                        actor=pb_actor_in,
                        rationale=pb_rationale_in,
                        allow_early_owner_override=pb_early,
                        acknowledge_corrections=pb_ack_corr,
                        expected_playbook_sha256=pb_prev_sha.strip() or None,
                        allow_multi_project=pb_multi_proj,
                        brain_dir=BRAIN_DIR,
                        atoms_dir=ATOMS_DIR,
                        playbooks_dir=PLAYBOOKS_DIR,
                        telemetry_db_path=TELEMETRY_DB_PATH,
                    )
                    st.session_state["_dashboard_flash_success"] = (
                        f"Promoted `5-playbooks/{promoted_res['filename']}` (SHA-256 `{promoted_res['sha256']}`)."
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"Playbook promotion blocked: {exc}")

        st.divider()
        st.markdown("#### Registered Skill Playbooks & Cryptographic Integrity")
        all_playbooks = list_playbooks(
            active_only=False,
            playbooks_dir=PLAYBOOKS_DIR,
            brain_dir=BRAIN_DIR,
            atoms_dir=ATOMS_DIR,
            telemetry_db_path=TELEMETRY_DB_PATH,
        )
        if not all_playbooks:
            st.caption("No Playbooks present in `brain/5-playbooks/` yet.")
        else:
            pb_rows = [
                {
                    "playbook_id": pb.playbook_id,
                    "filename": pb.filename,
                    "valid": pb.valid,
                    "status": pb.status,
                    "reason": pb.reason,
                    "title": pb.title,
                    "contexts": ", ".join(pb.contexts),
                    "backing_atoms": ", ".join(pb.backing_atoms),
                    "sha256": pb.sha256,
                }
                for pb in all_playbooks
            ]
            st.dataframe(pd.DataFrame(pb_rows), width="stretch")

            active_choices = {pb.filename: pb.sha256 for pb in all_playbooks if pb.status != "deprecated"}
            if active_choices:
                with st.expander("🗑️ Deprecate Playbook (CAS-Locked)", expanded=False):
                    dep_target = st.selectbox("Playbook to Deprecate", sorted(active_choices.keys()), key="dep_pb_target")
                    dep_actor = st.text_input("Human Owner Actor", value="local_dashboard_owner", key="dep_pb_actor")
                    dep_reason = st.text_input("Deprecation Rationale (required)", key="dep_pb_reason")
                    if st.button("🗑️ Deprecate Selected Playbook", key="btn_deprecate_pb"):
                        try:
                            dep_res = deprecate_skill_playbook(
                                dep_target,
                                expected_sha256=active_choices[dep_target],
                                actor=dep_actor,
                                rationale=dep_reason,
                                brain_dir=BRAIN_DIR,
                                playbooks_dir=PLAYBOOKS_DIR,
                            )
                            st.session_state["_dashboard_flash_success"] = (
                                f"Deprecated `5-playbooks/{dep_res['filename']}` (new SHA-256 `{dep_res['sha256']}`)."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Playbook deprecation blocked: {exc}")

    with tab_planner:
        st.subheader("Grounded Executive Planner")
        st.caption(
            "Synthesizes structured execution plans strictly from active verified Playbooks and owner-approved Atoms. "
            "Every step carries cryptographic source/playbook anchors; unsupported goals abstain explicitly."
        )
        plan_goal = st.text_input("Task Goal", placeholder="e.g. Triage failing background workers and inspect logs", key="plan_goal_in")
        pc1, pc2, pc3 = st.columns(3)
        with pc1:
            plan_mode = st.selectbox("Planning Mode", ["deterministic", "model_assisted"], index=0, key="plan_mode_in")
            host_keys = sorted(load_host_policies().keys())
            default_idx = host_keys.index("ollama_local") if "ollama_local" in host_keys else 0
            plan_host = st.selectbox("AI Host (used when model_assisted)", host_keys, index=default_idx, key="plan_host_in")
        with pc2:
            plan_project = st.text_input("Project Scope (optional, e.g. project:second_brain)", value="", key="plan_proj_in")
            plan_contexts = st.text_input("Active Context Profile (comma-separated, e.g. windows, python)", value="", key="plan_ctx_in")
        with pc3:
            plan_prefs = st.checkbox("Include Subjective Owner Preferences (as non-factual guardrails)", value=False, key="plan_prefs_in")
            plan_save = st.checkbox("Save Read-Only Briefing to `brain/briefings/`", value=False, key="plan_save_in")

        if st.button("🧭 Assemble Grounded Executive Plan", key="btn_run_planner"):
            if not plan_goal.strip():
                st.warning("Enter a non-empty task goal.")
            else:
                try:
                    req = ExecutivePlanRequest(
                        goal=plan_goal.strip(),
                        project_scope=plan_project.strip() or None,
                        active_context_profile=tuple(c.strip() for c in plan_contexts.split(",") if c.strip()),
                        include_preferences=plan_prefs,
                        mode=plan_mode,
                        host_id=plan_host,
                    )
                    plan_result = assemble_executive_plan(
                        req,
                        brain_dir=BRAIN_DIR,
                        atoms_dir=ATOMS_DIR,
                        playbooks_dir=PLAYBOOKS_DIR,
                        briefings_dir=BRIEFINGS_DIR,
                        lexical_index_path=LEXICAL_INDEX_PATH,
                        chroma_path=CHROMA_PATH,
                        telemetry_db_path=TELEMETRY_DB_PATH,
                        save_briefing=plan_save,
                    )
                    if plan_result.plan_ready:
                        st.success(f"Plan assembled ({plan_result.mode_used}): {plan_result.summary}")
                    else:
                        st.warning(f"Abstained ({plan_result.mode_used}): {plan_result.summary}")
                    if plan_result.saved_briefing_path:
                        st.caption(f"Saved read-only briefing: `{plan_result.saved_briefing_path}`")
                    st.code(plan_result.render_markdown(), language="markdown")
                    with st.expander("🔍 Inspect Structured Plan JSON & Citations", expanded=False):
                        st.json(plan_result.as_dict())
                except Exception as exc:
                    st.error(f"Executive planning failed: {exc}")


# PAGE: WORKING NOTES & IMPORT (PHASE 4)
elif nav_selection == "📝 Working Notes & Import":
    st.title("📝 Working Notes & Bulk Import")
    st.caption(
        "Zero-friction owner notes stored in `brain/notes/` (separate from verified atoms). "
        "Promote an exact excerpt into an immutable snapshot + SHA-256 anchored approved atom at any time."
    )
    note_paths = _active_note_paths()
    tab_notes_browse, tab_notes_create, tab_notes_import, tab_notes_tasks = st.tabs([
        "📚 Browse, Edit & Verify Excerpt",
        "➕ New Working Note",
        "📦 Bulk & Document Import",
        "✅ Action Items & Tasks",
    ])

    with tab_notes_browse:
        nf1, nf2, nf3, nf4 = st.columns([2.2, 1.4, 0.8, 0.8])
        with nf1:
            note_q = st.text_input("Search Working Notes (FTS5)", value="", key="notes_fts_q")
        with nf2:
            note_tag = st.text_input("Filter by Tag", placeholder="e.g. project:second_brain", key="notes_tag_q")
        with nf3:
            note_limit = st.selectbox("Page Size", [15, 25, 50, 100], index=1, key="notes_page_limit")
        with nf4:
            note_offset = st.number_input("Offset", min_value=0, step=int(note_limit), value=0, key="notes_page_offset")

        note_items, note_total = _list_notes_filtered(
            query=note_q.strip(),
            tag=note_tag.strip(),
            limit=int(note_limit),
            offset=int(note_offset),
        )
        start_idx = int(note_offset) + 1 if note_items else 0
        end_idx = int(note_offset) + len(note_items)
        st.caption(f"Showing {start_idx}–{end_idx} of {note_total} matching working note(s).")

        if not note_items:
            st.info("No working notes match the current filter.")
        else:
            note_choices = {f"{n.title} ({n.filename})": n.filename for n in note_items}
            chosen_label = st.selectbox("Select Working Note", list(note_choices.keys()), key="sel_working_note")
            chosen_fn = note_choices[chosen_label]
            full_note = get_note(chosen_fn, paths=note_paths)
            note_backlinks = backlinks(chosen_fn, paths=note_paths)

            st.caption(
                f"SHA-256: `{full_note.sha256}` | Origin: `{full_note.origin}` | "
                f"Tags: `{', '.join(full_note.tags) or 'none'}` | Backlinks: `{len(note_backlinks)}`"
            )

            sub_edit, sub_preview, sub_backlinks = st.tabs([
                "✏️ Edit Markdown",
                "👁️ Rendered Preview",
                f"🔗 Backlinks ({len(note_backlinks)})",
            ])

            with sub_edit:
                edited_title = st.text_input("Title", value=full_note.title, key=f"nt_title_{chosen_fn}")
                edited_tags = st.text_input("Tags (comma-separated)", value=", ".join(full_note.tags), key=f"nt_tags_{chosen_fn}")
                edited_body = st.text_area("Markdown Body", value=full_note.body or "", height=260, key=f"nt_body_{chosen_fn}")
                nc1, nc2 = st.columns(2)
                with nc1:
                    if st.button("💾 Save Note (CAS)", key=f"btn_save_nt_{chosen_fn}"):
                        try:
                            res = update_note(
                                chosen_fn,
                                edited_body,
                                expected_sha256=full_note.sha256,
                                title=edited_title,
                                tags=[t.strip() for t in edited_tags.split(",") if t.strip()],
                                paths=note_paths,
                            )
                            st.session_state["_dashboard_flash_success"] = (
                                f"Saved `{res['filename']}` (SHA-256 `{res['sha256'][:16]}…`)."
                            )
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Note update blocked: {exc}")
                with nc2:
                    del_confirm = st.checkbox(
                        f"Confirm deletion of `{chosen_fn}`",
                        value=False,
                        key=f"chk_del_nt_{chosen_fn}",
                    )
                    if st.button("🗑️ Delete Note (CAS)", disabled=not del_confirm, key=f"btn_del_nt_{chosen_fn}"):
                        try:
                            delete_note(chosen_fn, expected_sha256=full_note.sha256, paths=note_paths)
                            st.session_state["_dashboard_flash_success"] = f"Deleted `{chosen_fn}`."
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Delete blocked: {exc}")

            with sub_preview:
                with st.container(border=True):
                    st.subheader(full_note.title)
                    st.code(full_note.body or "(empty note)", language="markdown")

            with sub_backlinks:
                if not note_backlinks:
                    st.caption("No other working notes currently link to this note via `[[wikilinks]]`.")
                else:
                    bl_rows = [
                        {
                            "filename": b.filename,
                            "title": b.title,
                            "tags": ", ".join(b.tags),
                            "updated_at": b.updated_at,
                            "origin": b.origin,
                        }
                        for b in note_backlinks
                    ]
                    st.dataframe(pd.DataFrame(bl_rows), width="stretch")

            with st.expander("✅ Verify Excerpt into Canonical Approved Atom", expanded=False):
                candidates = _extract_note_excerpt_candidates(full_note.body or "")
                picker_key = f"ver_pick_{chosen_fn}"
                exc_key = f"ver_exc_{chosen_fn}"

                def _apply_picked_excerpt(pk: str = picker_key, ek: str = exc_key):
                    picked_val = st.session_state.get(pk, "")
                    if picked_val and picked_val != "(Type or paste custom excerpt below)":
                        st.session_state[ek] = picked_val

                if candidates:
                    st.selectbox(
                        "⚡ 1-Click Sentence / Bullet Picker (from saved note)",
                        options=["(Type or paste custom excerpt below)", *candidates],
                        key=picker_key,
                        on_change=_apply_picked_excerpt,
                    )

                ver_excerpt = st.text_area(
                    "Exact Excerpt from Saved Note (min 12 chars)",
                    placeholder="Select a sentence/bullet above or paste an exact clause from the saved note...",
                    height=90,
                    key=exc_key,
                )
                vc1, vc2, vc3 = st.columns(3)
                with vc1:
                    ver_mtype = st.selectbox(
                        "Memory Type",
                        ["semantic", "procedural", "episodic", "self"],
                        index=0,
                        key=f"ver_mtype_{chosen_fn}",
                    )
                with vc2:
                    ver_tags = st.text_input(
                        "Atom Tags / Scopes (comma-separated)",
                        value=", ".join(full_note.tags),
                        key=f"ver_tags_{chosen_fn}",
                    )
                with vc3:
                    ver_actor = st.text_input("Human Owner Actor", value="local_dashboard_owner", key=f"ver_act_{chosen_fn}")
                ver_claim = st.text_input("Optional Distilled Claim (defaults to excerpt)", value="", key=f"ver_clm_{chosen_fn}")

                if st.button("🔒 Snapshot & Verify Excerpt", key=f"btn_ver_nt_{chosen_fn}"):
                    try:
                        vres = verify_note_excerpt(
                            chosen_fn,
                            ver_excerpt,
                            actor=ver_actor,
                            expected_note_sha256=full_note.sha256,
                            claim=ver_claim.strip() or None,
                            memory_type=ver_mtype,
                            tags=tuple(t.strip() for t in ver_tags.split(",") if t.strip()),
                            paths=note_paths,
                        )
                        ev_src = vres.get("evidence_source") or vres.get("snapshot_rel_path", "")
                        st.success(
                            f"Promoted approved atom `2-atoms/{vres['atom_filename']}` (SHA-256 `{vres['atom_sha256'][:16]}…`) "
                            f"anchored to immutable snapshot `{ev_src}`."
                        )
                    except Exception as exc:
                        st.error(f"Excerpt verification blocked: {exc}")

    with tab_notes_create:
        new_nt_title = st.text_input("Note Title", placeholder="e.g. Architecture Decision — Local Queue", key="new_nt_title")
        new_nt_tags = st.text_input("Tags (comma-separated)", placeholder="project:second_brain, architecture", key="new_nt_tags")
        new_nt_body = st.text_area("Note Body", height=220, key="new_nt_body")
        if st.button("➕ Create Working Note", key="btn_create_nt"):
            try:
                created = create_note(
                    new_nt_title,
                    new_nt_body,
                    tags=[t.strip() for t in new_nt_tags.split(",") if t.strip()],
                    paths=note_paths,
                )
                st.success(f"Created `brain/notes/{created['filename']}` (SHA-256 `{created['sha256'][:16]}…`).")
            except NoteError as exc:
                st.error(f"Could not create note: {exc}")

    with tab_notes_import:
        st.caption(
            "Idempotently import an Obsidian vault or local document tree into `brain/notes/` "
            "(`owner_import` origin, never auto-approved). "
            "Supported document formats: **`.md`, `.markdown`, `.txt`, `.pdf`, `.docx`, `.epub`, `.html`, `.htm`, `.csv`, `.tsv`**."
        )
        folder_placeholder = r"C:\Users\you\Documents\ObsidianVault" if os.name == "nt" else "~/Documents/ObsidianVault"
        imp_folder = st.text_input("Source Folder Path", placeholder=folder_placeholder, key="imp_folder_path")
        imp_tags = st.text_input("Optional Tags to Apply (comma-separated)", value="imported", key="imp_folder_tags")
        imp_c1, imp_c2 = st.columns(2)
        with imp_c1:
            imp_dry = st.checkbox("Dry Run (preview counts without writing files)", value=True, key="imp_dry_run")
        with imp_c2:
            imp_overwrite = st.checkbox(
                "Overwrite Locally Modified Imported Notes (`overwrite_modified`, saves `.history/` backup)",
                value=False,
                key="imp_overwrite_modified",
            )
        if st.button("📦 Run Bulk Import", key="btn_bulk_import"):
            if not imp_folder.strip():
                st.warning("Provide a local folder path to import.")
            else:
                try:
                    imp_sig = inspect.signature(import_folder).parameters
                    imp_kwargs: dict[str, object] = {
                        "tags": tuple(t.strip() for t in imp_tags.split(",") if t.strip()),
                        "dry_run": imp_dry,
                        "paths": note_paths,
                    }
                    if "overwrite_modified" in imp_sig:
                        imp_kwargs["overwrite_modified"] = imp_overwrite
                    stats = import_folder(imp_folder.strip(), **imp_kwargs)
                    sdict = stats.as_dict()
                    created_val = int(sdict.get("created", sdict.get("imported", 0)))
                    updated_val = int(sdict.get("updated", 0))
                    skipped_val = int(
                        sdict.get(
                            "skipped",
                            int(sdict.get("skipped_too_large", 0))
                            + int(sdict.get("skipped_unreadable", 0))
                            + int(sdict.get("skipped_symlink", 0))
                            + int(sdict.get("skipped_oversize", 0))
                            + int(sdict.get("skipped_binary", 0)),
                        )
                    )
                    conflicts_val = int(sdict.get("conflicts_preserved", 0))
                    mode_lbl = "Dry-Run Preview" if sdict.get("dry_run") else "Import Completed"
                    st.success(f"**{mode_lbl}** → Target `{sdict.get('notes_dir', NOTES_DIR)}`")
                    ic1, ic2, ic3, ic4, ic5, ic6 = st.columns(6)
                    ic1.metric("Scanned", sdict.get("scanned", 0))
                    ic2.metric("Created", created_val)
                    ic3.metric("Updated", updated_val)
                    ic4.metric("Unchanged", sdict.get("unchanged", 0))
                    ic5.metric("Skipped", skipped_val)
                    ic6.metric("Conflicts Preserved", conflicts_val)
                    with st.expander("🔍 Detailed Import Breakdown JSON", expanded=False):
                        st.json(sdict)
                except Exception as exc:
                    st.error(f"Bulk import blocked: {exc}")

        st.divider()
        st.markdown("#### 📄 Import Single Local Document")
        st.caption(
            "Deterministically extract and import one file (`.md`, `.txt`, `.pdf`, `.docx`, `.epub`, `.html`, `.csv`, `.tsv`) "
            "into `brain/notes/` with `owner_import` provenance."
        )
        doc_placeholder = r"C:\Users\you\Documents\paper.pdf" if os.name == "nt" else "~/Documents/paper.pdf"
        single_doc_path = st.text_input("Document File Path", placeholder=doc_placeholder, key="imp_single_file_path")
        single_doc_tags = st.text_input("Optional Document Tags (comma-separated)", value="imported", key="imp_single_file_tags")
        if st.button("📄 Import Single Document", key="btn_single_doc_import"):
            if not single_doc_path.strip():
                st.warning("Provide a local document file path to import.")
            else:
                try:
                    imported_doc = _import_single_document(
                        single_doc_path.strip(),
                        tags=tuple(t.strip() for t in single_doc_tags.split(",") if t.strip()),
                    )
                    st.success(
                        f"Imported `{imported_doc.get('source_file', '')}` → `brain/notes/{imported_doc['filename']}` "
                        f"(format `{imported_doc.get('format', 'text')}`, SHA-256 `{str(imported_doc.get('sha256', ''))[:16]}…`)."
                    )
                except Exception as exc:
                    st.error(f"Single-document import blocked: {exc}")

    with tab_notes_tasks:
        st.markdown("#### ✅ Extracted Action Items & Tasks")
        st.caption(
            "Model-free deterministic task scanner across `brain/notes/` and local raw captures (`- [ ]`, `TODO:`, `ACTION:`, `FIXME:`)."
        )
        tc1, tc2, tc3 = st.columns([2.0, 1.0, 1.0])
        with tc1:
            tasks_proj_filter = st.text_input("Optional Project Scope", placeholder="e.g. project:second_brain", key="notes_tasks_proj")
        with tc2:
            tasks_inc_completed = st.checkbox("Include Completed", value=False, key="notes_tasks_completed")
        with tc3:
            tasks_inc_raw = st.checkbox("Include Raw Captures", value=True, key="notes_tasks_raw")
        extracted_tasks = extract_action_items(
            paths=note_paths,
            include_completed=tasks_inc_completed,
            include_raw=tasks_inc_raw,
            project=tasks_proj_filter.strip() or None,
        )
        st.caption(f"Found {len(extracted_tasks)} matching action item(s).")
        if extracted_tasks:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "status": "done" if t.get("completed") else "open",
                            "priority": t.get("priority") or "",
                            "due_date": t.get("due_date") or "",
                            "source": f"{t.get('filename', '')}:{t.get('line_number', '')}",
                            "task": t.get("text", ""),
                        }
                        for t in extracted_tasks
                    ]
                ),
                width="stretch",
            )
        else:
            st.info("No matching open tasks or action items found.")


# PAGE: VAULT PROOF, CONTRADICTION RADAR & TIME-TRAVEL (PHASE 5)
elif nav_selection == "🛡️ Vault Proof & Time-Travel":
    st.title("🛡️ Vault Proof, Contradiction Radar & Time-Travel")
    st.caption(
        "Cryptographic Merkle inclusion proofs & hash-chained audit ledger, deterministic Contradiction & Drift Radar, "
        "and point-in-time Vault Snapshots with semantic diffs."
    )
    tab_merkle, tab_radar, tab_timetravel = st.tabs([
        "🔐 Merkle Audit Ledger & Proofs",
        "📡 Contradiction & Drift Radar",
        "⏳ Time-Travel Snapshots & Diff",
    ])

    with tab_merkle:
        ledger_state = verify_vault_ledger(
            brain_dir=BRAIN_DIR,
            atoms_dir=ATOMS_DIR,
            playbooks_dir=PLAYBOOKS_DIR,
        )
        merkle_live = compute_vault_merkle(
            brain_dir=BRAIN_DIR,
            atoms_dir=ATOMS_DIR,
            playbooks_dir=PLAYBOOKS_DIR,
        )
        live_leaf_cnt = ledger_state.get("leaf_count", ledger_state.get("live_leaf_count", merkle_live.get("leaf_count", 0)))
        synced_head = ledger_state.get("matches_latest_checkpoint", ledger_state.get("synced_with_head", False))
        approved_cnt = ledger_state.get("approved_atoms", merkle_live.get("approved_atoms", 0))
        accepted_pb_cnt = ledger_state.get("accepted_playbooks", merkle_live.get("accepted_playbooks", 0))

        mc1, mc2, mc3, mc4 = st.columns(4)
        mc1.metric("Hash Chain Valid", "YES" if ledger_state.get("chain_valid") else "TAMPERED / ERROR")
        mc2.metric("Ledger Checkpoints", ledger_state.get("entry_count", 0))
        mc3.metric("Verified Merkle Leaves", live_leaf_cnt)
        mc4.metric("Synced With Head", "YES" if synced_head else "UNCOMMITTED DIFF")
        st.code(
            f"Live Merkle Root : {ledger_state.get('live_merkle_root', merkle_live.get('merkle_root', ''))}\n"
            f"Head Merkle Root : {ledger_state.get('head_merkle_root') or '(no checkpoint committed yet)'}\n"
            f"Approved Atoms   : {approved_cnt} | Accepted Playbooks: {accepted_pb_cnt}",
            language="text",
        )
        if ledger_state.get("errors"):
            st.error("Ledger verification errors: " + "; ".join(ledger_state["errors"]))

        approved_leaf_atoms = [
            str(leaf["rel_path"]).split("/", 1)[-1]
            for leaf in merkle_live.get("leaves", [])
            if str(leaf.get("rel_path", "")).startswith("2-atoms/")
        ]

        lc1, lc2 = st.columns(2)
        with lc1:
            with st.container(border=True):
                st.markdown("#### 🔏 Commit Signed Merkle Checkpoint")
                ckpt_actor = st.text_input("Owner Actor", value="local_dashboard_owner", key="ldg_actor")
                ckpt_note = st.text_input("Checkpoint Note", placeholder="e.g. Post-review release checkpoint", key="ldg_note")
                if st.button("🔏 Commit Signed Merkle Checkpoint", key="btn_commit_ledger"):
                    try:
                        entry = commit_ledger_checkpoint(
                            brain_dir=BRAIN_DIR,
                            atoms_dir=ATOMS_DIR,
                            playbooks_dir=PLAYBOOKS_DIR,
                            actor=ckpt_actor,
                            note=ckpt_note,
                        )
                        st.session_state["_dashboard_flash_success"] = (
                            f"Committed checkpoint #{entry['seq']} (`{entry['entry_sha256'][:16]}…`)."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Checkpoint commit failed: {exc}")

        with lc2:
            with st.container(border=True):
                st.markdown("#### 🧾 Generate Portable Merkle Inclusion Proof")
                if approved_leaf_atoms:
                    picked_atom = st.selectbox(
                        "Select Approved Atom",
                        [*approved_leaf_atoms, "(Custom filename...)"],
                        key="ldg_prove_select",
                    )
                    if picked_atom == "(Custom filename...)":
                        prove_target = st.text_input("Custom Approved Atom Filename", placeholder="e.g. structured-logging.md", key="ldg_prove_atom")
                    else:
                        prove_target = picked_atom
                else:
                    prove_target = st.text_input("Approved Atom Filename", placeholder="e.g. structured-logging.md", key="ldg_prove_atom")

                if st.button("🧾 Prove Atom Membership", key="btn_prove_atom"):
                    if not str(prove_target).strip():
                        st.warning("Select or enter an approved atom filename.")
                    else:
                        try:
                            proof = prove_atom_membership(
                                str(prove_target).strip(),
                                brain_dir=BRAIN_DIR,
                                atoms_dir=ATOMS_DIR,
                                playbooks_dir=PLAYBOOKS_DIR,
                            )
                            verified_ok = verify_merkle_proof(proof)
                            proof["verified_offline"] = verified_ok
                            pm1, pm2, pm3 = st.columns(3)
                            pm1.metric("Offline Proof Valid", "VALID" if verified_ok else "INVALID")
                            pm2.metric("Tree Sibling Steps", len(proof.get("siblings", [])))
                            pm3.metric("Anchored to Head", "YES" if proof.get("ledger_head_entry_sha256") else "LIVE ONLY")
                            st.caption(
                                f"**Target:** `{proof['rel_path']}` · **File SHA-256:** `{proof['file_sha256'][:18]}…` · "
                                f"**Root:** `{proof['merkle_root'][:18]}…`"
                            )
                            if proof.get("siblings"):
                                st.dataframe(pd.DataFrame(proof["siblings"]), width="stretch")
                            else:
                                st.caption("Single-leaf Merkle tree (0 sibling hashes required).")
                            with st.expander("🔍 Portable Proof Bundle JSON", expanded=False):
                                st.json(proof)
                        except Exception as exc:
                            st.error(f"Proof generation blocked: {exc}")

        st.divider()
        ov1, ov2 = st.columns(2)
        with ov1:
            with st.container(border=True):
                st.markdown("#### 🧪 Offline Stateless Merkle Proof Verifier")
                st.caption("Paste any exported Merkle proof JSON bundle to cryptographically verify leaf-to-root inclusion with zero vault access.")
                raw_proof_json = st.text_area(
                    "Proof Bundle JSON",
                    placeholder='{"schema_version": 1, "rel_path": "2-atoms/...", "file_sha256": "...", "leaf_sha256": "...", "siblings": [...], "merkle_root": "..."}',
                    height=135,
                    key="offline_proof_input",
                )
                if st.button("✅ Verify Proof Bundle Offline", key="btn_verify_proof_offline"):
                    if not raw_proof_json.strip():
                        st.warning("Paste a Merkle proof JSON object first.")
                    else:
                        try:
                            parsed_bundle = json.loads(raw_proof_json)
                            is_valid = verify_merkle_proof(parsed_bundle)
                            if is_valid:
                                st.success(
                                    f"Cryptographic Merkle inclusion proof VALID for `{parsed_bundle.get('rel_path')}` "
                                    f"against root `{str(parsed_bundle.get('merkle_root', ''))[:20]}…`."
                                )
                            else:
                                st.error("Merkle inclusion proof INVALID (hash mismatch, malformed digest, or tampered sibling path).")
                        except json.JSONDecodeError as exc:
                            st.error(f"Invalid JSON payload: {exc}")

        with ov2:
            with st.container(border=True):
                st.markdown("#### 📜 Checkpoint History (`vault-ledger.jsonl`)")
                history_rows = _read_ledger_history()
                if history_rows:
                    st.dataframe(pd.DataFrame(history_rows), width="stretch")
                else:
                    st.caption("No checkpoints committed to `brain/.sidecar/vault-ledger.jsonl` yet.")

    with tab_radar:
        rc1, rc2, rc3 = st.columns([1.8, 1.3, 1.3])
        with rc1:
            radar_proj = st.text_input("Optional Project Scope", placeholder="e.g. project:second_brain", key="radar_proj")
        with rc2:
            radar_inc_pending = st.checkbox("Include Pending Drafts in Graph & Contradiction Scan", value=True, key="radar_pending")
        with rc3:
            radar_enable_nli = st.checkbox("Enable Local Ollama NLI Adjudication", value=False, key="radar_enable_nli")
        if st.button("📡 Scan Vault for Contradictions, Source Drift & Orphans", key="btn_scan_radar"):
            scope_arg = radar_proj.strip() or None
            graph_info = build_knowledge_graph(
                brain_dir=BRAIN_DIR,
                atoms_dir=ATOMS_DIR,
                project_scope=scope_arg,
                include_pending=radar_inc_pending,
            )
            radar_info = detect_contradictions(
                brain_dir=BRAIN_DIR,
                atoms_dir=ATOMS_DIR,
                project_scope=scope_arg,
                include_pending=radar_inc_pending,
                enable_nli=radar_enable_nli,
            )
            drift_alerts = radar_info.get("drift_alerts", [])
            drift_cnt = radar_info.get("drift_count", len(drift_alerts))
            orphans = radar_info.get("orphan_approved_atoms", graph_info.get("orphan_approved_atoms", []))

            gc1, gc2, gc3, gc4, gc5 = st.columns(5)
            gc1.metric("Scoped Graph Nodes", graph_info["node_count"])
            gc2.metric("Graph Edges", graph_info["edge_count"])
            gc3.metric("Contradictions", radar_info["contradiction_count"])
            gc4.metric("High Severity", radar_info["high_severity_count"])
            gc5.metric("Source / CAS Drift", drift_cnt)
            if radar_enable_nli or radar_info.get("nli_status"):
                st.caption(
                    f"Local NLI Adjudication: `{radar_info.get('nli_status', 'disabled')}` "
                    f"({radar_info.get('nli_evaluated_pairs', 0)} pair(s) evaluated)"
                )

            st.markdown("#### ⚡ Claim Polarity & Numeric Contradiction Alerts")
            if radar_info.get("alerts"):
                for idx, alert in enumerate(radar_info["alerts"], start=1):
                    sev = str(alert.get("severity", "medium")).upper()
                    a_info = alert["atom_a"]
                    b_info = alert["atom_b"]
                    title = (
                        f"[{sev}] #{idx}: `{a_info['filename']}` ↔ `{b_info['filename']}` "
                        f"({', '.join(alert.get('reasons', []))} · Jaccard {alert.get('jaccard')})"
                    )
                    with st.expander(title, expanded=(sev == "HIGH")):
                        st.markdown(f"- **Atom A (`{a_info['filename']}`, `{a_info['review_status']}`):**")
                        st.code(str(a_info.get("claim", "")), language="text")
                        st.markdown(f"- **Atom B (`{b_info['filename']}`, `{b_info['review_status']}`):**")
                        st.code(str(b_info.get("claim", "")), language="text")
                        if alert.get("shared_terms"):
                            st.caption("Shared lexical terms: `" + "`, `".join(alert["shared_terms"]) + "`")
                        if alert.get("exclusive_categories"):
                            st.caption("Mutually exclusive categories: `" + "`, `".join(alert["exclusive_categories"]) + "`")
                        if isinstance(alert.get("nli_adjudication"), dict) and alert["nli_adjudication"].get("verdict"):
                            nli_adj = alert["nli_adjudication"]
                            st.info(
                                f"Local NLI Verdict: `{nli_adj.get('verdict')}` "
                                f"(confidence: `{nli_adj.get('confidence', 'n/a')}`) — {nli_adj.get('rationale', '')}"
                            )
            else:
                st.success("Zero claim polarity or numeric contradictions detected in selected scope.")

            st.markdown("#### ⚓ Source Anchor & CAS Integrity Drift Alerts")
            if drift_alerts:
                for d_idx, dalert in enumerate(drift_alerts, start=1):
                    d_file = dalert.get("filename") or dalert.get("atom_id") or f"item-{d_idx}"
                    d_reason = dalert.get("reason") or dalert.get("status") or "Source anchor / SHA-256 drift detected"
                    with st.expander(f"⚠️ Drift #{d_idx}: `{d_file}` — {d_reason}", expanded=True):
                        st.json(dalert)
            else:
                st.success("Zero source-anchor or CAS integrity drift alerts reported.")

            st.markdown("#### 🏝️ Orphan Approved Atoms")
            if orphans:
                st.dataframe(pd.DataFrame([{"orphan_approved_atom": fn} for fn in orphans]), width="stretch")
            else:
                st.caption("No orphan approved atoms in the selected scope.")

        st.divider()
        with st.container(border=True):
            st.markdown("#### ⚓ Source Anchor Audit & 1-Click Stale Approval Demotion")
            st.caption(
                "Verify every atom's stored byte-offset source anchor against live `0-raw/` and `sources/` files. "
                "Demoting stale approvals uses CAS verification and appends an owner decision record without altering source evidence."
            )
            sa_c1, sa_c2 = st.columns([1.4, 2.0])
            with sa_c1:
                audit_actor = st.text_input("Owner Audit Actor", value="local_dashboard_owner", key="radar_demote_actor")
            with sa_c2:
                demote_confirm = st.checkbox(
                    "Confirm CAS demotion of stale approved atoms to `pending`",
                    value=False,
                    key="radar_demote_confirm",
                )
            sa_b1, sa_b2 = st.columns(2)
            audit_reports = None
            ran_demote = False
            with sa_b1:
                if st.button("🔎 Audit Source Anchors (Read-Only)", key="btn_audit_sources"):
                    try:
                        audit_reports = audit_sources(
                            atoms_dir=ATOMS_DIR,
                            brain_dir=BRAIN_DIR,
                            demote_stale=False,
                        )
                    except Exception as exc:
                        st.error(f"Source audit failed: {exc}")
            with sa_b2:
                if st.button("⬇️ Demote Stale Approvals", disabled=not demote_confirm, key="btn_demote_stale"):
                    if not audit_actor.strip():
                        st.warning("Provide a non-empty owner actor identity to demote stale approvals.")
                    else:
                        try:
                            audit_reports = audit_sources(
                                atoms_dir=ATOMS_DIR,
                                brain_dir=BRAIN_DIR,
                                demote_stale=True,
                                actor=audit_actor.strip(),
                            )
                            ran_demote = True
                        except Exception as exc:
                            st.error(f"Stale approval demotion failed: {exc}")

            if audit_reports is not None:
                stale_count = sum(
                    1
                    for r in audit_reports
                    if r.get("stale_approval") or r.get("action") in {"review_required", "would_demote"}
                )
                demoted_count = sum(
                    1 for r in audit_reports if r.get("demoted") or r.get("action") == "demoted"
                )
                am1, am2, am3 = st.columns(3)
                am1.metric("Atoms Audited", len(audit_reports))
                am2.metric("Stale Approvals", stale_count)
                am3.metric("Demoted to Pending", demoted_count)
                if ran_demote:
                    st.success(
                        f"Source audit completed: demoted {demoted_count} stale approval(s) "
                        f"(audited {len(audit_reports)}, stale detected {stale_count + demoted_count if stale_count == 0 and demoted_count else max(stale_count, demoted_count)})."
                    )
                elif stale_count == 0:
                    st.success(f"All {len(audit_reports)} audited atom(s) have current or non-stale source anchors.")
                else:
                    st.warning(f"Detected {stale_count} stale approval(s) requiring owner review or demotion.")
                if audit_reports:
                    summary_rows = [
                        {
                            "atom_id": r.get("atom_id"),
                            "review_status": r.get("review_status"),
                            "anchor_status": r.get("anchor_status"),
                            "approval_current": r.get("approval_current"),
                            "stale_approval": bool(
                                r.get("stale_approval") or r.get("action") in {"review_required", "would_demote"}
                            ),
                            "demoted": bool(r.get("demoted") or r.get("action") == "demoted"),
                            "action": r.get("action"),
                        }
                        for r in audit_reports
                    ]
                    st.dataframe(pd.DataFrame(summary_rows), width="stretch")

    with tab_timetravel:
        sc1, sc2 = st.columns([1, 1.6])
        with sc1:
            with st.container(border=True):
                st.markdown("#### 📸 Capture Point-in-Time Snapshot")
                snap_label = st.text_input("Snapshot Label", placeholder="e.g. pre-refactor", key="tt_label")
                snap_actor = st.text_input("Owner Actor", value="local_dashboard_owner", key="tt_actor")
                if st.button("📸 Capture Vault Snapshot", key="btn_take_snap"):
                    try:
                        snap_doc = create_snapshot(
                            label=snap_label,
                            actor=snap_actor,
                            brain_dir=BRAIN_DIR,
                            atoms_dir=ATOMS_DIR,
                            playbooks_dir=PLAYBOOKS_DIR,
                        )
                        st.session_state["_dashboard_flash_success"] = (
                            f"Created snapshot `{snap_doc['snapshot_id']}` "
                            f"({snap_doc['atom_count']} approved atoms, Merkle `{snap_doc['merkle_root'][:16]}…`)."
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Snapshot failed: {exc}")

        snaps = list_snapshots(brain_dir=BRAIN_DIR)
        with sc2:
            st.markdown(f"#### 🗂️ Recorded Snapshots ({len(snaps)})")
            if snaps:
                st.dataframe(pd.DataFrame(snaps), width="stretch")
            else:
                st.info("No vault snapshots captured yet. Capture a snapshot on the left to enable semantic diffs and historical recall.")

        if snaps:
            st.divider()
            snap_ids = [s["snapshot_id"] for s in snaps]
            with st.container(border=True):
                st.markdown("#### 🔐 Verify Snapshot Integrity")
                vc_sel, vc_btn = st.columns([2.2, 1.0])
                with vc_sel:
                    ver_snap_id = st.selectbox("Snapshot to Verify", snap_ids, key="tt_verify_snap")
                with vc_btn:
                    st.write("")
                    run_verify_snap = st.button("🔐 Verify Snapshot Integrity", key="btn_verify_snap", width="stretch")
                if run_verify_snap:
                    try:
                        ver_doc = verify_snapshot(ver_snap_id, brain_dir=BRAIN_DIR)
                        vm1, vm2, vm3 = st.columns(3)
                        vm1.metric("Merkle Integrity", "VALID" if ver_doc.get("valid") else "INVALID / TAMPERED")
                        vm2.metric("Snapshot Atoms", ver_doc.get("atom_count", 0))
                        vm3.metric("Snapshot Playbooks", ver_doc.get("playbook_count", 0))
                        rec_root = ver_doc.get("recorded_merkle_root") or ver_doc.get("stored_merkle_root") or ""
                        comp_root = ver_doc.get("computed_merkle_root") or ""
                        st.code(
                            f"Snapshot ID          : {ver_doc.get('snapshot_id', ver_snap_id)}\n"
                            f"Recorded Merkle Root : {rec_root}\n"
                            f"Computed Merkle Root : {comp_root}",
                            language="text",
                        )
                        if ver_doc.get("valid"):
                            st.success(f"Snapshot `{ver_doc.get('snapshot_id', ver_snap_id)}` Merkle integrity verified.")
                        else:
                            errs = ver_doc.get("errors") or ver_doc.get("issues") or ["Merkle verification failed."]
                            st.error("Snapshot integrity errors: " + "; ".join(str(e) for e in errs))
                    except Exception as exc:
                        st.error(f"Snapshot verification failed: {exc}")

            st.divider()
            dc1, dc2 = st.columns(2)
            with dc1:
                with st.container(border=True):
                    st.markdown("#### 🔍 Semantic Snapshot Diff")
                    from_snap = st.selectbox("From Snapshot", snap_ids, key="tt_from")
                    to_snap = st.selectbox("To Snapshot (or `live`)", ["live", *snap_ids], key="tt_to")
                    if st.button("🔍 Compute Semantic Diff", key="btn_diff_snap"):
                        try:
                            diff_doc = diff_snapshots(
                                from_snap,
                                to_snap,
                                brain_dir=BRAIN_DIR,
                                atoms_dir=ATOMS_DIR,
                                playbooks_dir=PLAYBOOKS_DIR,
                            )
                            dm1, dm2, dm3, dm4 = st.columns(4)
                            dm1.metric("State", "UNCHANGED" if diff_doc.get("unchanged") else "CHANGED")
                            dm2.metric("Added Atoms", len(diff_doc.get("added_atoms", [])))
                            dm3.metric("Removed Atoms", len(diff_doc.get("removed_atoms", [])))
                            dm4.metric("Modified Atoms", len(diff_doc.get("modified_atoms", [])))
                            st.caption(
                                f"Merkle `{str(diff_doc.get('merkle_root_before', ''))[:16]}…` → "
                                f"`{str(diff_doc.get('merkle_root_after', ''))[:16]}…`"
                            )
                            if diff_doc.get("added_atoms"):
                                st.markdown("**➕ Added Approved Atoms**")
                                st.dataframe(pd.DataFrame(diff_doc["added_atoms"]), width="stretch")
                            if diff_doc.get("removed_atoms"):
                                st.markdown("**➖ Removed Approved Atoms**")
                                st.dataframe(pd.DataFrame(diff_doc["removed_atoms"]), width="stretch")
                            if diff_doc.get("modified_atoms"):
                                st.markdown("**✏️ Modified Approved Atoms**")
                                st.dataframe(pd.DataFrame(diff_doc["modified_atoms"]), width="stretch")
                            pb_changes = [
                                *[{"change": "added", "playbook": p} for p in diff_doc.get("added_playbooks", [])],
                                *[{"change": "removed", "playbook": p} for p in diff_doc.get("removed_playbooks", [])],
                                *[{"change": "modified", "playbook": p} for p in diff_doc.get("modified_playbooks", [])],
                            ]
                            if pb_changes:
                                st.markdown("**📘 Skill Playbook Changes**")
                                st.dataframe(pd.DataFrame(pb_changes), width="stretch")
                            with st.expander("🔍 Raw Diff JSON", expanded=False):
                                st.json(diff_doc)
                        except Exception as exc:
                            st.error(f"Snapshot diff failed: {exc}")

            with dc2:
                with st.container(border=True):
                    st.markdown("#### ⏪ Point-in-Time Historical Recall")
                    pit_snap = st.selectbox("Point-in-Time Snapshot", snap_ids, key="tt_pit_snap")
                    pit_query = st.text_input("Historical Recall Query", placeholder="What was approved at this snapshot?", key="tt_pit_q")
                    pit_proj = st.text_input("Optional Project Scope", value="", key="tt_pit_proj")
                    pc_a, pc_b = st.columns(2)
                    with pc_a:
                        pit_topk = st.slider("Max Results (`top_k`)", 1, 20, 5, key="tt_pit_topk")
                    with pc_b:
                        pit_prefs = st.checkbox("Include Owner Preferences", value=False, key="tt_pit_prefs")
                    if st.button("⏪ Recall at Snapshot", key="btn_pit_recall"):
                        try:
                            pit_res = recall_at_snapshot(
                                pit_query,
                                pit_snap,
                                project_scope=pit_proj.strip() or None,
                                include_preferences=pit_prefs,
                                top_k=pit_topk,
                                brain_dir=BRAIN_DIR,
                            )
                            hits = pit_res.get("results", [])
                            st.caption(
                                f"Snapshot `{pit_res['snapshot_id']}` (`{pit_res['created_at']}`) · "
                                f"Merkle `{pit_res['merkle_root'][:16]}…` · **{len(hits)} hit(s)**"
                            )
                            if not hits:
                                st.info("No approved atoms in that snapshot matched the query.")
                            else:
                                for rank, h in enumerate(hits, start=1):
                                    with st.container(border=True):
                                        st.markdown(
                                            f"**#{rank} `{h['filename']}`** (score `{h['score']}` · SHA-256 `{str(h.get('file_sha256', ''))[:14]}…`)"
                                        )
                                        st.code(str(h.get("claim", "")), language="text")
                                        if h.get("contexts"):
                                            st.caption("Contexts: `" + "`, `".join(h["contexts"]) + "`")
                            with st.expander("🔍 Raw Historical Recall JSON", expanded=False):
                                st.json(pit_res)
                        except Exception as exc:
                            st.error(f"Point-in-time recall failed: {exc}")


# PAGE 5: SYSTEM CONTROL
elif nav_selection == "⚙️ System Control":
    st.title("⚙️ System Control")
    st.subheader("Manage Engine Operations & Local IDE Connectors")

    c1, c2 = st.columns(2)

    with c1:
        with st.container(border=True):
            st.markdown("### ⚙️ Engine Shift Playbooks")
            st.markdown("Process raw inbox notes, verify consistency, and cluster atomic information.")

            if st.button("🔥 Run Refinery (Night Shift)"):
                proc = run_script_process("scripts/ollama_refinery.py")
                stream_logs_ui(proc, "Ollama Refinery Engine running")

            if st.button("📚 Run Editor (Morning Shift)"):
                proc = run_script_process("scripts/ollama_editor.py")
                stream_logs_ui(proc, "Ollama Editor clustering & synthesis running")

            if st.button("🩺 Run System Doctor"):
                proc = run_script_process("scripts/doctor.py")
                stream_logs_ui(proc, "Second Brain system doctor running")

            if st.button("📊 Run Comparative SOTA Benchmark"):
                proc = run_script_process("scripts/sota_benchmark.py")
                stream_logs_ui(proc, "Comparative SOTA benchmark running")

        with st.container(border=True):
            st.markdown("### 📯 Watchdog Real-time Daemon & House Rules")
            st.info(_daemon_launcher_hint())
            if st.checkbox("View `house-rules.md`"):
                if os.path.isfile(HOUSE_RULES) and not _is_reparse_or_link(HOUSE_RULES):
                    with open(HOUSE_RULES, "r", encoding="utf-8", errors="replace") as hrf:
                        st.code(hrf.read(), language="markdown")
                else:
                    try:
                        st.code(read_house_rules(), language="markdown")
                    except OSError:
                        st.caption("No `house-rules.md` present in active vault.")

    with c2:
        with st.container(border=True):
            st.markdown("### 🔌 Local IDE & Agent MCP Connector")
            st.caption(
                "One-click trusted-stdio MCP registration with automatic `.bak-<timestamp>` backup and atomic JSON merge."
            )
            st.dataframe(pd.DataFrame(ide_connector_status()), width="stretch")

            targets_map = ide_host_targets()
            ide_keys = sorted(targets_map.keys())
            sel_ide = st.selectbox(
                "Target IDE / Agent Host",
                options=ide_keys,
                format_func=lambda k: f"{targets_map[k].label} ({k})",
                key="sys_ide_target",
            )
            sel_target = targets_map[sel_ide]
            st.caption(f"Target config file: `{sel_target.config_path}`")
            st.warning(f"🔒 **Privacy Notice:** {IDE_PRIVACY_NOTICE}")
            ide_ack = st.checkbox(
                "I acknowledge the privacy notice and authorize updating this host's local MCP configuration.",
                value=False,
                key="sys_ide_privacy_ack",
            )
            ic_preview, ic_conn, ic_disc = st.columns(3)
            with ic_preview:
                if st.button("🔍 Dry-Run Preview", key="btn_ide_dry"):
                    try:
                        st.json(ide_connect(sel_ide, dry_run=True))
                    except Exception as exc:
                        st.error(f"Dry-run failed: {exc}")
            with ic_conn:
                if st.button("🔌 Connect Host", disabled=not ide_ack, key="btn_ide_connect"):
                    try:
                        cres = ide_connect(sel_ide, dry_run=False)
                        st.session_state["_dashboard_flash_success"] = (
                            f"Connected `{cres['label']}` (`{cres['config_path']}`). {cres.get('next_step', '')}"
                        )
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Connect failed: {exc}")
            with ic_disc:
                if st.button("⏏️ Disconnect Host", key="btn_ide_disconnect"):
                    try:
                        dres = ide_disconnect(sel_ide)
                        st.session_state["_dashboard_flash_success"] = f"`{sel_ide}` status: `{dres['status']}`."
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Disconnect failed: {exc}")

    # Zero-Knowledge E2EE Multi-Device Vault Sync
    st.divider()
    with st.container(border=True):
        st.markdown("### 🔐 Zero-Knowledge E2EE Multi-Device Vault Sync")
        st.caption(
            "AES-256-GCM + HKDF-SHA256 + Ed25519 signed heads. Syncs ONLY durable Markdown and append-only logs; "
            "excludes `.sidecar/`, `.chroma/`, and security secrets."
        )
        try:
            sync_st = vault_sync_status(brain_dir=BRAIN_DIR)
        except Exception as sync_exc:
            sync_st = {"configured": False, "error": str(sync_exc), "conflict_count": 0, "conflicts": []}

        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Sync Status", "Configured" if sync_st.get("configured") else "Not Configured")
        sm2.metric("Local Epoch", sync_st.get("local_epoch", 0))
        sm3.metric("Remote Epoch", sync_st.get("remote_epoch", 0))
        sm4.metric("Conflicts", sync_st.get("conflict_count", 0))

        sc1, sc2 = st.columns(2)
        with sc1:
            sync_target_in = st.text_input(
                "Sync Target (Local Folder or https:// URL)",
                value=str(sync_st.get("target") or ""),
                key="sys_sync_target",
            )
            sync_backend_in = st.selectbox(
                "Backend",
                options=["local_folder", "http_object"],
                index=0 if sync_st.get("backend_type", "local_folder") == "local_folder" else 1,
                key="sys_sync_backend",
            )
        with sc2:
            sync_pass_in = st.text_input(
                "Optional Passphrase (leave blank for keyfile)",
                type="password",
                key="sys_sync_passphrase",
            )
            sync_rec_in = st.text_input(
                "Recovery Key (base64, required only for Join)",
                type="password",
                key="sys_sync_recovery",
            )

        sb1, sb2, sb3, sb4, sb5 = st.columns(5)
        with sb1:
            if st.button("🆕 Init Sync", key="btn_sync_init"):
                try:
                    res = vault_init_sync(
                        sync_target_in.strip(),
                        backend_type=sync_backend_in,
                        brain_dir=BRAIN_DIR,
                        passphrase=sync_pass_in or None,
                    )
                    st.success(f"Initialized E2EE vault `{res['vault_id']}` (device `{res['device_id']}`).")
                    if res.get("recovery_key_b64"):
                        st.code(f"Recovery Key (save offline safely): {res['recovery_key_b64']}", language="text")
                except Exception as exc:
                    st.error(f"Init failed: {exc}")
        with sb2:
            if st.button("🤝 Join Sync", key="btn_sync_join"):
                try:
                    res = vault_join_sync(
                        sync_target_in.strip(),
                        backend_type=sync_backend_in,
                        brain_dir=BRAIN_DIR,
                        passphrase=sync_pass_in or None,
                        recovery_key_b64=sync_rec_in.strip() or None,
                    )
                    st.success(f"Joined E2EE vault `{res['vault_id']}` as `{res['device_id']}`.")
                except Exception as exc:
                    st.error(f"Join failed: {exc}")
        with sb3:
            if st.button("⬆️ Push Encrypted", key="btn_sync_push"):
                try:
                    res = vault_sync_push(brain_dir=BRAIN_DIR, passphrase=sync_pass_in or None)
                    st.success(
                        f"Pushed epoch `{res['epoch']}` ({res['uploaded_blobs']} uploaded, {res['reused_blobs']} reused)."
                    )
                except Exception as exc:
                    st.error(f"Push failed: {exc}")
        with sb4:
            if st.button("⬇️ Pull & Merge", key="btn_sync_pull"):
                try:
                    res = vault_sync_pull(brain_dir=BRAIN_DIR, passphrase=sync_pass_in or None)
                    st.success(
                        f"Pulled epoch `{res['epoch']}` (+{res['added']} added, {res['updated']} updated, "
                        f"{res['merged_jsonl']} JSONL merged, {res['conflict_count']} conflict(s))."
                    )
                except Exception as exc:
                    st.error(f"Pull failed: {exc}")
        with sb5:
            if st.button("🛡️ Verify Remote", key="btn_sync_verify"):
                try:
                    res = vault_sync_verify(brain_dir=BRAIN_DIR, passphrase=sync_pass_in or None)
                    if res.get("valid"):
                        st.success(f"Verified remote vault cleanly ({res.get('verified_blobs', 0)} blobs).")
                    else:
                        st.error(f"Verification failed: {res.get('issues')}")
                except Exception as exc:
                    st.error(f"Verify failed: {exc}")

    # Multi-Vault Workspaces & Opt-In Federated Cross-Vault Recall
    st.divider()
    with st.container(border=True):
        st.markdown("### 🗂️ Multi-Vault Workspaces & Opt-In Federated Recall")
        st.caption(
            "Maintain isolated vault workspaces (`SecondBrain/security/vaults.json`) with optional per-vault bearer tokens. "
            "Switching atomically updates `vault-path.txt` for CLI, MCP, Dashboard, and Desktop."
        )
        try:
            ws_info = ws_list_vaults(current_brain_dir=BRAIN_DIR)
        except Exception as ws_exc:
            ws_info = {"active_vault_id": "default", "active_vault_path": BRAIN_DIR, "vaults": [], "error": str(ws_exc)}

        ws_list = list(ws_info.get("vaults") or [])
        if ws_list:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Vault ID": v["vault_id"],
                            "Label": v["label"],
                            "Active": "✅" if v["active"] else "",
                            "Federated": "opt-in" if v["federated_searchable"] else "isolated",
                            "Atoms": v["atom_count"],
                            "Notes": v["note_count"],
                            "Path": v["path"],
                        }
                        for v in ws_list
                    ]
                ),
                width="stretch",
            )
            ws_ids = [v["vault_id"] for v in ws_list]
            sel_ws = st.selectbox("Select Registered Vault", options=ws_ids, key="sys_ws_select")
            wc1, wc2, wc3 = st.columns(3)
            with wc1:
                if st.button("🔀 Switch Active Vault", key="btn_ws_switch"):
                    try:
                        sw_res = ws_switch_vault(sel_ws, current_brain_dir=BRAIN_DIR)
                        st.success(f"Switched active vault to `{sw_res['active_vault_id']}` ({sw_res['active_vault_path']}).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Switch failed: {exc}")
            with wc2:
                if st.button("🌐 Toggle Federated Opt-In", key="btn_ws_toggle_fed"):
                    try:
                        cur_v = next(v for v in ws_list if v["vault_id"] == sel_ws)
                        upd = ws_update_vault_policy(
                            sel_ws,
                            federated_searchable=not bool(cur_v["federated_searchable"]),
                            current_brain_dir=BRAIN_DIR,
                        )
                        st.success(f"`{sel_ws}` federated_searchable={upd['federated_searchable']}.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Policy update failed: {exc}")
            with wc3:
                if st.button("🧹 Unregister Vault (Keep Files)", key="btn_ws_remove"):
                    try:
                        ws_remove_vault(sel_ws, current_brain_dir=BRAIN_DIR)
                        st.success(f"Unregistered `{sel_ws}` (files preserved on disk).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Unregister failed: {exc}")

        nc1, nc2 = st.columns(2)
        with nc1:
            new_ws_label = st.text_input("New Vault Label", placeholder="e.g. Work", key="sys_ws_new_label")
        with nc2:
            new_ws_path = st.text_input("New Vault Directory", placeholder="e.g. C:\\Users\\you\\SecondBrain-Work", key="sys_ws_new_path")
        nc3, nc4, nc5 = st.columns(3)
        with nc3:
            new_ws_tok = st.checkbox("Mint Isolated Per-Vault Token", value=False, key="sys_ws_new_tok")
        with nc4:
            new_ws_fed = st.checkbox("Opt-In Federated Search", value=False, key="sys_ws_new_fed")
        with nc5:
            if st.button("➕ Create Vault Workspace", key="btn_ws_create"):
                try:
                    cr = ws_create_vault(
                        new_ws_label.strip(),
                        new_ws_path.strip(),
                        isolated_token=new_ws_tok,
                        federated_searchable=new_ws_fed,
                        current_brain_dir=BRAIN_DIR,
                    )
                    st.success(f"Created workspace `{cr['vault_id']}` at `{cr['path']}`.")
                    if cr.get("vault_token"):
                        st.code(f"Isolated Vault Token (save safely): {cr['vault_token']}", language="text")
                except Exception as exc:
                    st.error(f"Create vault failed: {exc}")

        fed_q = st.text_input("Opt-In Federated Cross-Vault Recall Query", key="sys_ws_fed_query")
        if st.button("🔎 Search Opted-In Vaults (Read-Only)", key="btn_ws_fed_search"):
            try:
                fres = ws_federated_recall(fed_q.strip(), limit=8, current_brain_dir=BRAIN_DIR)
                if fres["results"]:
                    st.dataframe(
                        pd.DataFrame(
                            [
                                {
                                    "Vault": r.get("vault_label") or r.get("vault_id"),
                                    "Atom": r.get("filename"),
                                    "Score": r.get("score"),
                                }
                                for r in fres["results"]
                            ]
                        ),
                        width="stretch",
                    )
                else:
                    st.info(f"No approved matches across opted-in vaults ({fres.get('searched_vaults')}).")
            except Exception as exc:
                st.error(f"Federated search failed: {exc}")

    # Local-First Browser Web Clipper Pairings
    st.divider()
    with st.container(border=True):
        st.markdown("### 🌐 Local-First Browser Web Clipper (Chrome / Edge / Firefox)")
        st.caption(
            "Loopback-only (`127.0.0.1`) MV3 web clipper with write-only scoped `clipper_*` tokens, "
            "verifiable SHA-256 source snapshots, and automatic DLP quarantine."
        )
        cc1, cc2 = st.columns(2)
        with cc1:
            clip_origin_in = st.text_input(
                "Extension Origin",
                placeholder="chrome-extension://<32-char-id> or moz-extension://<uuid>",
                key="sys_clip_origin",
            )
        with cc2:
            clip_label_in = st.text_input("Browser Label", value="Browser Clipper", key="sys_clip_label")
        if st.button("🔗 Pair Browser Extension & Mint Scoped Token", key="btn_clip_pair"):
            try:
                pres = clipper_create_pairing(clip_origin_in.strip(), label=clip_label_in.strip() or "Browser Clipper")
                st.success(f"Paired `{pres['label']}` (`{pres['pairing_id']}`). Paste this write-only token into the extension popup:")
                st.code(str(pres["token"]), language="text")
            except Exception as exc:
                st.error(f"Pairing failed: {exc}")
        try:
            pairings = clipper_list_pairings()
        except Exception:
            pairings = []
        if pairings:
            st.dataframe(pd.DataFrame(pairings), width="stretch")
            rev_id = st.selectbox("Revoke Paired Extension", [p["pairing_id"] for p in pairings], key="sys_clip_rev_sel")
            if st.button("🚫 Revoke Clipper Pairing", key="btn_clip_revoke"):
                try:
                    clipper_revoke_pairing(rev_id)
                    st.success(f"Revoked `{rev_id}`.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Revoke failed: {exc}")
        else:
            st.info("No browser clipper extensions paired yet.")

    # Briefing log
    st.divider()
    st.markdown("### 📰 Latest Morning Briefings")

    if not os.path.exists(BRIEFINGS_DIR):
        st.info("No briefings found.")
    else:
        brief_files = sorted(glob.glob(os.path.join(BRIEFINGS_DIR, "*.md")), reverse=True)
        if not brief_files:
            st.info("No briefings created yet.")
        else:
            selected_brief = st.selectbox("Select Briefing", [os.path.basename(f) for f in brief_files])
            if selected_brief:
                brief_path = safe_file_in_dir(BRIEFINGS_DIR, selected_brief, allowed_exts={".md"})
                if not brief_path or _is_reparse_or_link(brief_path):
                    st.error("Selected briefing path is invalid.")
                    st.stop()
                with open(brief_path, "r", encoding="utf-8", errors="replace") as f:
                    st.code(f.read(), language="markdown")
