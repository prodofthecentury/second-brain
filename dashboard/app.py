# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "streamlit",
#     "plotly",
#     "networkx",
#     "pandas",
#     "ollama",
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

import os
import sys
import re
import datetime
import subprocess
import glob
import hashlib
import pandas as pd
import networkx as nx
import plotly.graph_objects as go
import streamlit as st
import ollama

# ─── Path Setup ──────────────────────────────────────────────────────────────
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APP_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from scripts.config import (
    BRAIN_DIR, ATOMS_DIR, THREADS_DIR, EXPERIENCES_DIR, PLAYBOOKS_DIR, RAW_DIR, INBOX_DIR, SOURCES_DIR, BRIEFINGS_DIR, SIDECAR_DIR,
    GEN_MODEL, READING_LIST,
    ensure_dirs, read_house_rules, today, now_slug,
    is_content_markdown,
    normalize_markdown_filename, safe_file_in_dir
)
from scripts.recall import (
    RecallOptions,
    consume_last_degradation_warning,
    search_memory as recall_search_memory,
    suggest_duplicate_atoms,
)
from scripts.answer_policy import (
    ANSWER_DRAFT_SCHEMA,
    AnswerRequest,
    NO_APPROVED_MEMORY_RESPONSE,
    answer_from_memory,
    build_grounding_prompt,
    validate_standard_answer,
)
from scripts.atom_mutator import apply_atom_edit, apply_atom_merge
from scripts.review_atom import apply_review_transition, approved_revision_is_current
from scripts.memory_review import parse_atom_record
from scripts.review_atom import review_lock
from scripts.review_details import atom_review_details
from scripts.dashboard_auth import assert_loopback_address, require_dashboard_login
from scripts.hardware_profile import detect_hardware_profile
from scripts.inbox_workflow import inspect_inbox_item, list_inbox_items, promote_inbox_item, reject_inbox_item
from scripts.host_adapters import (
    HostConsentError,
    HostSharingRule,
    _audit_outbound_call,
    _redact_outbound_with_map,
    _rehydrate_draft_claims,
    filter_atoms_for_host,
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
    PlaybookValidationError,
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
    create_note,
    delete_note,
    get_note,
    list_notes,
    update_note,
    verify_note_excerpt,
)
from scripts.bulk_import import import_folder
from scripts.ide_connector import status as ide_connector_status
from scripts.vault_ledger import (
    commit_ledger_checkpoint,
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
)

ensure_dirs()

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
    """Counts files in each directory."""
    counts = {"atoms": 0, "threads": 0, "sources": 0, "raw": 0}
    if os.path.exists(ATOMS_DIR):
        counts["atoms"] = len([f for f in os.listdir(ATOMS_DIR) if is_content_markdown(f)])
    if os.path.exists(THREADS_DIR):
        counts["threads"] = len([f for f in os.listdir(THREADS_DIR) if is_content_markdown(f)])
    if os.path.exists(SOURCES_DIR):
        counts["sources"] = len([f for f in os.listdir(SOURCES_DIR) if is_content_markdown(f)])
    if os.path.exists(RAW_DIR):
        counts["raw"] = len([f for f in os.listdir(RAW_DIR) if f.endswith(".md") and f != "reading-list.md"])
    return counts

def run_script_process(script_rel_path, extra_args=[]):
    """Launch a Python script using the active interpreter (wheel- and checkout-safe)."""
    try:
        script_abs = os.path.join(PROJECT_ROOT, script_rel_path)
        cmd = [sys.executable, script_abs] + list(extra_args)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=PROJECT_ROOT
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
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug[:max_len]

def _stream_local_ollama_chat(*, endpoint_url: str, model: str, messages: list[dict], schema: dict):
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
    client = ollama.Client(host=endpoint_url)
    return client.chat(
        model=model,
        messages=messages,
        format=schema,
        stream=True,
    )

def get_ollama_model_names() -> list[str]:
    try:
        local_rule = load_host_policies().get("ollama_local")
        if local_rule is None or not local_rule.enabled:
            return []
        validate_endpoint_network_boundary(local_rule)
        response = ollama.Client(host=local_rule.endpoint_url).list()
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
            ),
        )
    except Exception as e:
        st.error(f"Recall Error: {e}")
        return [], []
    degradation = consume_last_degradation_warning()
    if degradation:
        st.warning(degradation)

    return [item.as_dict() for item in direct], [item.as_dict() for item in neighbors]


# ─── Knowledge Graph Builder ──────────────────────────────────────────────────

def build_network_graph():
    G = nx.Graph()
    if not os.path.exists(ATOMS_DIR):
        return G
        
    # Process Atoms
    atoms = [f for f in os.listdir(ATOMS_DIR) if is_content_markdown(f)]
    for atom in atoms:
        atom_path = os.path.join(ATOMS_DIR, atom)
        with open(atom_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        title = atom.replace(".md", "")
        id_match = re.search(r'^id:\s*(.+)$', content, re.MULTILINE)
        if id_match:
            title = id_match.group(1).strip()
            
        tags = []
        tags_match = re.search(r'^tags:\s*\[(.*?)\]', content, re.MULTILINE)
        if tags_match:
            tags = [t.strip().strip('"').strip("'") for t in tags_match.group(1).split(",")]
            
        G.add_node(atom, title=title, type="atom", tags=tags)
        
        # Parse links
        links = re.findall(r'\[\[(.*?)\]\]', content)
        for link in links:
            target = safe_wikilink_filename(link)
            if target:
                G.add_edge(atom, target)
            
    # Process Threads
    if os.path.exists(THREADS_DIR):
        threads = [f for f in os.listdir(THREADS_DIR) if is_content_markdown(f)]
        for thread in threads:
            thread_path = os.path.join(THREADS_DIR, thread)
            with open(thread_path, "r", encoding="utf-8") as f:
                content = f.read()
                
            title = thread.replace(".md", "")
            G.add_node(thread, title=title, type="thread", tags=["Synthesis"])
            
            links = re.findall(r'\[\[(.*?)\]\]', content)
            for link in links:
                target = safe_wikilink_filename(link)
                if target:
                    G.add_edge(thread, target)
                
    return G

def draw_network_plotly(G, dimension="2D"):
    if not G.nodes():
        return None
        
    if dimension == "3D":
        pos = nx.spring_layout(G, dim=3, seed=42)
        
        edge_x, edge_y, edge_z = [], [], []
        for edge in G.edges():
            if edge[0] in pos and edge[1] in pos:
                x0, y0, z0 = pos[edge[0]]
                x1, y1, z1 = pos[edge[1]]
                edge_x.extend([x0, x1, None])
                edge_y.extend([y0, y1, None])
                edge_z.extend([z0, z1, None])
                
        edge_trace = go.Scatter3d(
            x=edge_x, y=edge_y, z=edge_z,
            line=dict(width=1.5, color='rgba(90, 105, 120, 0.4)'),
            hoverinfo='none',
            mode='lines'
        )
        
        node_x, node_y, node_z = [], [], []
        node_text, node_color, node_size = [], [], []
        
        for node in G.nodes():
            if node in pos:
                x, y, z = pos[node]
                node_x.append(x)
                node_y.append(y)
                node_z.append(z)
                
                title = G.nodes[node].get("title", node)
                ntype = G.nodes[node].get("type", "atom")
                node_text.append(f"<b>{title}</b><br>Type: {ntype}<br>File: {node}")
                
                if ntype == "thread":
                    node_color.append("#00f2fe")  # cyan
                    node_size.append(15)
                else:
                    node_color.append("#9d4edd")  # purple
                    node_size.append(8)
                    
        node_trace = go.Scatter3d(
            x=node_x, y=node_y, z=node_z,
            mode='markers',
            hoverinfo='text',
            text=node_text,
            marker=dict(
                showscale=False,
                color=node_color,
                size=node_size,
                line=dict(color='#0d0f18', width=1)
            )
        )
        
        fig = go.Figure(
            data=[edge_trace, node_trace],
            layout=go.Layout(
                showlegend=False,
                hovermode='closest',
                margin=dict(b=0, l=0, r=0, t=0),
                paper_bgcolor='rgba(0,0,0,0)',
                scene=dict(
                    xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, title='', visible=False),
                    yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, title='', visible=False),
                    zaxis=dict(showgrid=False, zeroline=False, showticklabels=False, title='', visible=False),
                    bgcolor='rgba(0,0,0,0)'
                )
            )
        )
    else: # 2D Graph
        pos = nx.spring_layout(G, seed=42)
        
        edge_x, edge_y = [], []
        for edge in G.edges():
            if edge[0] in pos and edge[1] in pos:
                x0, y0 = pos[edge[0]]
                x1, y1 = pos[edge[1]]
                edge_x.extend([x0, x1, None])
                edge_y.extend([y0, y1, None])
                
        edge_trace = go.Scatter(
            x=edge_x, y=edge_y,
            line=dict(width=1, color='rgba(90, 105, 120, 0.4)'),
            hoverinfo='none',
            mode='lines'
        )
        
        node_x, node_y = [], []
        node_text, node_color, node_size = [], [], []
        
        for node in G.nodes():
            if node in pos:
                x, y = pos[node]
                node_x.append(x)
                node_y.append(y)
                
                title = G.nodes[node].get("title", node)
                ntype = G.nodes[node].get("type", "atom")
                node_text.append(f"<b>{title}</b><br>Type: {ntype}<br>File: {node}")
                
                if ntype == "thread":
                    node_color.append("#00f2fe")
                    node_size.append(22)
                else:
                    node_color.append("#9d4edd")
                    node_size.append(12)
                    
        node_trace = go.Scatter(
            x=node_x, y=node_y,
            mode='markers',
            hoverinfo='text',
            text=node_text,
            marker=dict(
                showscale=False,
                color=node_color,
                size=node_size,
                line=dict(color='#0d0f18', width=1.5)
            )
        )
        
        fig = go.Figure(
            data=[edge_trace, node_trace],
            layout=go.Layout(
                showlegend=False,
                hovermode='closest',
                margin=dict(b=0, l=0, r=0, t=0),
                paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)',
                xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, visible=False),
                yaxis=dict(showgrid=False, zeroline=False, showticklabels=False, visible=False)
            )
        )
        
    return fig


# ─── Sidebar Panel ────────────────────────────────────────────────────────────

st.sidebar.markdown(f"""
    <div style="text-align: center; margin-bottom: 2rem;">
        <h1 style="margin: 0; font-size: 1.8rem; background: linear-gradient(135deg, #00f2fe 0%, #9d4edd 100%); -webkit-background-clip: text; -webkit-text-fill-color: transparent;">🧠 Second Brain 2.0</h1>
        <p style="color: #657b83; font-size: 0.8rem; margin: 0.2rem 0 0 0;">Semantic Memory System</p>
    </div>
""", unsafe_allow_html=True)

# Count stats
counts = get_counts()

# Render side metrics
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
        <div class="glass-card stats-card" style="border-left-color: #9d4edd;">
            <div class="stats-val">{counts['threads']}</div>
            <div class="stats-label">Threads</div>
        </div>
    """, unsafe_allow_html=True)

cols2 = st.sidebar.columns(2)
with cols2[0]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #ff9e00;">
            <div class="stats-val">{counts['sources']}</div>
            <div class="stats-label">Sources</div>
        </div>
    """, unsafe_allow_html=True)
with cols2[1]:
    st.markdown(f"""
        <div class="glass-card stats-card" style="border-left-color: #ff007f;">
            <div class="stats-val">{counts['raw']}</div>
            <div class="stats-label">Inbox</div>
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

# Initialize chat messages session state
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []


# PAGE 1: CHAT WITH MEMORY
if nav_selection == "💬 Chat with Memory":
    st.title("💬 Chat with Memory")
    st.subheader("Graph RAG Recall Engine")
    
    # RAG Settings Box
    with st.expander("⚙️ Retrieval Parameters", expanded=False):
        c1, c2, c3 = st.columns(3)
        with c1:
            rag_n_results = st.slider("Atoms to Retrieve", 1, 10, 3)
            rag_retrieval_mode = st.selectbox("Retrieval Mode", ["auto", "rrf", "lexical", "dense"], index=0)
        with c2:
            rag_follow = st.checkbox("Follow Graph Links (Graph RAG)", value=True)
            rag_cross_project = st.checkbox("Allow Cross-Project Links", value=False)
            rag_fitness_boost = st.checkbox("Fitness & Freshness Boost (Approved Only)", value=False)
            rag_answer_mode = st.selectbox("Answer Contract Mode", ["standard", "preference"], index=0)
        with c3:
            installed_models = get_ollama_model_names()
            model_options = list(dict.fromkeys([
                GEN_MODEL,
                *installed_models,
                "llama3.2:3b",
                "qwen2.5-coder:7b",
                "qwen3.6:latest",
                "mistral",
                "phi3",
            ]))
            st_model = st.selectbox("Ollama Model", model_options)
            rag_project_scope = st.text_input("Project Scope (optional, e.g. project:second_brain)", value="")
            rag_context_profile = st.text_input("Active Context Profile (optional, e.g. windows, python)", value="")
            
    # Clear button
    if st.button("🧹 Clear Chat History"):
        st.session_state.chat_history = []
        st.rerun()

    # Display chat messages
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            if msg["role"] == "assistant" and msg.get("claims"):
                replay = validate_standard_answer(
                    msg["draft_json"],
                    msg["retrieved_atoms"],
                    atoms_dir=ATOMS_DIR,
                    mode=msg.get("answer_mode", "standard"),
                )
                st.text(replay.text)
            elif msg["role"] == "assistant":
                st.text(msg.get("content", answer_from_memory("", lambda: "")))
            else:
                st.text(msg.get("content", ""))

    # Handle input
    if user_prompt := st.chat_input("Ask your second brain anything..."):
        # Add user query to chat history
        st.session_state.chat_history.append({"role": "user", "content": user_prompt})
        
        with st.chat_message("user"):
            st.text(user_prompt)
            
        with st.chat_message("assistant"):
            project_tuple = tuple(
                p.strip() for p in rag_project_scope.split(",") if p.strip()
            )
            context_profile_tuple = tuple(
                c.strip() for c in rag_context_profile.split(",") if c.strip()
            )
            extra_recall_kwargs = {}
            if rag_fitness_boost:
                extra_recall_kwargs["enable_fitness_boost"] = True
            if context_profile_tuple:
                extra_recall_kwargs["active_context_profile"] = context_profile_tuple
            with st.spinner("🧠 Querying vector memories..."):
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
                    candidate_atom_ids=peek_lexical_candidate_atom_ids(user_prompt),
                    mode=rag_retrieval_mode,
                )
            except Exception:
                pass

            local_rule = load_host_policies().get("ollama_local")
            policy_blocked_reason: str | None = None
            if local_rule is None or not local_rule.enabled:
                policy_blocked_reason = (
                    "Host policy blocked: 'ollama_local' is disabled in `host-sharing-policy.json`. "
                    "Enable it in the Host Consent tab before chatting."
                )
            else:
                try:
                    validate_endpoint_network_boundary(local_rule)
                    if rag_answer_mode == "preference" and not local_rule.allow_preference_atoms:
                        raise HostConsentError(
                            "Host 'ollama_local' is not authorized to receive owner subjective preference memories."
                        )
                except HostConsentError as policy_exc:
                    policy_blocked_reason = f"Host policy blocked: {policy_exc}"

            if policy_blocked_reason is not None:
                st.error(policy_blocked_reason)
                try:
                    record_grounded_answer_telemetry([], abstained=True, mode=rag_answer_mode)
                except Exception:
                    pass
                st.session_state.chat_history.append({
                    "role": "assistant",
                    "content": policy_blocked_reason,
                    "claims": (),
                })
            else:
                assert local_rule is not None
                scoped_refs = filter_atoms_for_host(all_refs, local_rule, mode=rag_answer_mode)

                if scoped_refs:
                    ref_text = build_grounding_prompt(
                        AnswerRequest(query=user_prompt, mode=rag_answer_mode, result_limit=rag_n_results),
                        scoped_refs,
                    )
                    st.info(f"📚 Retrieved {len(direct_refs)} direct atoms and {len(neighbor_refs)} linked atoms ({len(scoped_refs)} permitted by host policy).")
                else:
                    ref_text = ""
                    st.warning("⚠ No relevant owner-approved memory permitted by host policy found. The assistant will abstain.")

                response_container = st.empty()
                full_response = ""
                if not ref_text.strip():
                    full_response = NO_APPROVED_MEMORY_RESPONSE if all_refs else answer_from_memory(ref_text, lambda: "")
                    try:
                        record_grounded_answer_telemetry([], abstained=True, mode=rag_answer_mode)
                    except Exception:
                        pass
                    response_container.text(full_response)
                    st.session_state.chat_history.append({
                        "role": "assistant",
                        "content": full_response,
                        "claims": (),
                    })
                else:
                    try:
                        safe_st_model = validate_model_identifier(st_model, provider="ollama")
                        redaction, pii_placeholder_map = _redact_outbound_with_map(
                            ref_text,
                            redact_pii=local_rule.redact_pii,
                            fail_on_secret_detection=local_rule.fail_on_secret_detection,
                        )
                        response_stream = _stream_local_ollama_chat(
                            endpoint_url=local_rule.endpoint_url,
                            model=safe_st_model,
                            messages=[{"role": "user", "content": redaction.sanitized_text}],
                            schema=ANSWER_DRAFT_SCHEMA,
                        )
                        for chunk in response_stream:
                            content = chunk["message"]["content"]
                            full_response += content
                        rehydrated_draft = _rehydrate_draft_claims(full_response, pii_placeholder_map)
                        answer = validate_standard_answer(
                            rehydrated_draft, scoped_refs, atoms_dir=ATOMS_DIR, mode=rag_answer_mode
                        )
                        _audit_outbound_call(
                            brain_dir=BRAIN_DIR,
                            rule=local_rule,
                            model=safe_st_model,
                            atom_ids=[str(r.get("filename", "")) for r in scoped_refs],
                            pii_redactions=redaction.pii_redaction_count,
                            outcome="grounded_answer" if answer.answerable else "abstained",
                        )
                        cited_atoms = [ev.atom_id for cl in answer.claims for ev in cl.evidence]
                        try:
                            record_grounded_answer_telemetry(
                                cited_atoms,
                                abstained=not answer.answerable,
                                mode=rag_answer_mode,
                            )
                        except Exception:
                            pass
                        full_response = answer.text
                        response_container.text(full_response)
                        st.session_state.chat_history.append({
                            "role": "assistant",
                            "content": full_response if not answer.answerable else "",
                            "claims": answer.claims,
                            "draft_json": rehydrated_draft if answer.answerable else "",
                            "retrieved_atoms": scoped_refs if answer.answerable else [],
                            "answer_mode": rag_answer_mode,
                        })
                    except HostConsentError as e:
                        st.error(f"Host policy blocked: {e}")
                    except Exception as e:
                        st.error(f"Error invoking AI host: {e}")


# PAGE 2: KNOWLEDGE GRAPH
elif nav_selection == "🕸️ Knowledge Graph":
    st.title("🕸️ Knowledge Graph")
    st.subheader("Semantic & Graph Connections")
    
    col1, col2 = st.columns([3, 1])
    
    with col2:
        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
        st.markdown("### Settings")
        graph_dim = st.radio("Dimension", ["2D", "3D"])
        
        st.markdown("### Graph Legend")
        st.markdown("""
            🔵 **Synthesis Threads**  
            🟣 **Atomic Notes**  
            *Edges represent wikilinks between files.*
        """)
        st.markdown("</div>", unsafe_allow_html=True)
        
    with col1:
        with st.spinner("Building interactive graph visual..."):
            G = build_network_graph()
            if G.nodes():
                fig = draw_network_plotly(G, dimension=graph_dim)
                st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": True})
            else:
                st.info("No nodes in vault. Add some notes to build the graph!")
                
    st.divider()
    
    # Interactive node inspector
    st.markdown("### 🔍 Node Inspector")
    if G.nodes():
        selected_node = st.selectbox("Select a Note to Inspect", sorted(list(G.nodes())))
        if selected_node:
            node_type = G.nodes[selected_node].get("type", "atom")
            sub_folder = THREADS_DIR if node_type == "thread" else ATOMS_DIR
            node_path = safe_file_in_dir(sub_folder, selected_node, allowed_exts={".md"})
            
            if node_path:
                with open(node_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                col_n1, col_n2 = st.columns([2, 1])
                with col_n1:
                    st.markdown(f"#### Content of `{selected_node}`")
                    st.code(content, language="markdown")
                with col_n2:
                    st.markdown("#### Metadata & Relations")
                    st.json({
                        "file": selected_node,
                        "type": node_type,
                        "tags": G.nodes[selected_node].get("tags", []),
                        "outgoing_links": list(G.neighbors(selected_node))
                    })
            else:
                st.error("Selected note path does not exist on disk.")


# PAGE 3: VAULT EXPLORER
elif nav_selection == "🗄️ Vault Explorer":
    st.title("🗄️ Vault Explorer")
    st.subheader("Inspect the vault; atom edits return to pending review")
    
    vault_tab = st.selectbox(
        "Vault Category",
        [
            "2-atoms (Atoms)",
            "5-playbooks (Skill Playbooks)",
            "3-threads (Threads)",
            "4-experiences (Experience Records)",
            "sources (Web Pages)",
            "0-raw (Inbox)",
            "0-raw/inbox (Quarantined MCP Inbox)",
        ],
    )
    
    # Map category to folder path
    folder_map = {
        "2-atoms (Atoms)": ATOMS_DIR,
        "5-playbooks (Skill Playbooks)": PLAYBOOKS_DIR,
        "3-threads (Threads)": THREADS_DIR,
        "4-experiences (Experience Records)": EXPERIENCES_DIR,
        "sources (Web Pages)": SOURCES_DIR,
        "0-raw (Inbox)": RAW_DIR,
        "0-raw/inbox (Quarantined MCP Inbox)": INBOX_DIR,
    }
    dir_path = folder_map[vault_tab]
    
    if not os.path.exists(dir_path):
        st.info("This vault category has no files yet.")
    else:
        files = sorted([f for f in os.listdir(dir_path) if f.endswith(".md") or f.endswith(".intake")])
        if dir_path in (ATOMS_DIR, INBOX_DIR, EXPERIENCES_DIR, PLAYBOOKS_DIR):
            files = [filename for filename in files if filename != "README.md"]
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
                    if not file_path:
                        st.error("Selected note path is invalid.")
                        st.stop()
                    with open(file_path, "r", encoding="utf-8") as f:
                        file_content = f.read()
                        
                    st.markdown(f"### Preview: `{selected_file}`")
                    
                    # Only canonical atom revisions may be edited in this UI.
                    if dir_path == ATOMS_DIR:
                        preview_mode = st.radio("View Mode", ["Preview", "Edit Source Code"], horizontal=True)
                    elif dir_path == INBOX_DIR:
                        preview_mode = "Preview"
                        st.caption("Quarantined MCP captures in `0-raw/inbox/` are read-only here; inspect raw source before promoting to `0-raw/`.")
                    else:
                        preview_mode = "Preview"
                        st.caption("Raw captures, source evidence, and generated threads are read-only here.")
                    
                    if preview_mode == "Preview":
                        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
                        st.code(file_content, language="markdown")
                        st.markdown("</div>", unsafe_allow_html=True)
                    else:
                        edited_content = st.text_area("Source Code", file_content, height=450)
                        if st.button("💾 Save Changes"):
                            try:
                                apply_atom_edit(
                                    atom_path=file_path,
                                    edited_content=edited_content,
                                    expected_sha256=hashlib.sha256(file_content.encode("utf-8")).hexdigest(),
                                    actor="local_dashboard_owner",
                                )
                                st.success("Atom saved as a new pending revision; recall index refreshed.")
                            except Exception as exc:
                                st.error(f"Atom save failed: {exc}")
                                st.stop()
                            st.rerun()

                    if dir_path == ATOMS_DIR and preview_mode == "Preview":
                        record = None
                        current_approval = False
                        try:
                            record = parse_atom_record(file_path)
                            current_approval = approved_revision_is_current(file_path)
                            approval_text = "verified current owner approval" if current_approval else f"{record['review_status']} (not eligible for recall)"
                            review_details = atom_review_details(
                                file_content,
                                atom_path=file_path,
                                atoms_dir=ATOMS_DIR,
                                brain_dir=os.path.dirname(ATOMS_DIR),
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
                        expected_hash = hashlib.sha256(file_content.encode("utf-8")).hexdigest()
                        if record and approve_col.button("Approve exact revision", key=f"approve-{selected_file}"):
                            try:
                                apply_review_transition(
                                    atom_path=file_path,
                                    action="approve",
                                    expected_sha256=expected_hash,
                                    actor="local_dashboard_owner",
                                )
                                st.success("Atom approved; source evidence verified and recall index refreshed.")
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
                                )
                                st.success("Atom quarantined as needs_edit and excluded from recall.")
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
                                )
                                st.success("Atom demoted to pending; inspect current source evidence before re-approval.")
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
                                )
                                st.success("Atom rejected and recall index refreshed.")
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
                                            )
                                            st.success(f"Merged `{merge_target}` into survivor `{selected_file}`; survivor queued as pending.")
                                            st.rerun()
                                        except Exception as exc:
                                            st.error(f"Merge failed: {exc}")
                            else:
                                st.caption("No other atoms available to merge.")

                        with st.expander("🔍 Read-Only Duplicate Suggestions", expanded=False):
                            dupes = [
                                d for d in suggest_duplicate_atoms(ATOMS_DIR)
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
        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
        st.markdown("### 📥 Quick Capture Note")
        inbox_title = st.text_input("Topic/Title (optional)")
        inbox_content = st.text_area("Write note or copy-paste text here...", height=200)
        inbox_source = st.text_input("Source details (web link, books, etc.)", "Web Dashboard")
        
        if st.button("📥 Add to Inbox"):
            if not inbox_content.strip():
                st.warning("Note content cannot be empty.")
            else:
                title_slug = make_slug(inbox_title) if inbox_title else "capture"
                filename = f"{now_slug()}-dashboard-{title_slug}.md"
                filepath = os.path.join(RAW_DIR, filename)
                
                safe_inbox_source = (
                    inbox_source.replace("--", "—")
                    .replace(">", "")
                    .replace("<", "")
                    .replace("\r", " ")
                    .replace("\n", " ")
                    .strip()[:200]
                )
                content = f"""<!-- Capture via Dashboard on {today()} -->
<!-- Source: {safe_inbox_source} -->

{inbox_content}
"""
                suffix = 1
                while True:
                    try:
                        with open(filepath, "x", encoding="utf-8") as f:
                            f.write(content)
                        break
                    except FileExistsError:
                        filename = f"{now_slug()}-dashboard-{title_slug}-{suffix}.md"
                        filepath = os.path.join(RAW_DIR, filename)
                        suffix += 1
                    
                st.success(f"Added note → `0-raw/{filename}`")
                st.balloons()
        st.markdown("</div>", unsafe_allow_html=True)
        
    with col_scout:
        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
        st.markdown("### 🕸️ Web Scout (Add URL)")
        scout_url = st.text_input("Add url to reading list:")
        
        if st.button("➕ Queue URL"):
            if not scout_url.strip() or not scout_url.startswith("http"):
                st.warning("Please enter a valid HTTP/HTTPS URL.")
            else:
                # Append to reading-list.md
                os.makedirs(RAW_DIR, exist_ok=True)
                with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
                    with open(READING_LIST, "a+", encoding="utf-8") as f:
                        f.seek(0)
                        contents = f.read()
                        if scout_url not in contents:
                            if not contents.endswith("\n") and contents:
                                f.write("\n")
                            f.write(f"- [ ] {scout_url}\n")
                            st.success("URL added to reading list!")
                        else:
                            st.info("URL is already in the reading list.")
                        
        st.markdown("### 📋 Current Reading List")
        if os.path.exists(READING_LIST):
            with open(READING_LIST, "r", encoding="utf-8") as f:
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
                st.dataframe(df, use_container_width=True)
            else:
                st.text("Reading list is currently empty.")
        else:
            st.text("No reading list found. Add a URL to create it.")
            
        st.markdown("</div>", unsafe_allow_html=True)
        
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
        q_items = list_inbox_items(INBOX_DIR)
        if not q_items:
            st.success("Quarantined inbox is clean (0 pending captures).")
        else:
            selected_q = st.selectbox("Select Quarantined Capture", [item.filename for item in q_items])
            if selected_q:
                q_detail = inspect_inbox_item(selected_q, inbox_dir=INBOX_DIR)
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
                            )
                            st.success(f"Promoted to `0-raw/{dec['promoted_filename']}` (SHA-256 `{dec['promoted_sha256']}`).")
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
                            )
                            st.info(f"Rejected and archived `{selected_q}`.")
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
        st.caption("Online hosts are disabled by default, require HTTPS + public DNS validation, enforce PII/credential redaction, and undergo identical out-of-model grounding.")
        policies = load_host_policies()
        chosen_host = st.selectbox("Select AI Host Profile", sorted(policies.keys()))
        if chosen_host:
            rule = policies[chosen_host]
            en_val = st.checkbox("Enable Context Sharing with this Host", value=rule.enabled, key=f"en_{chosen_host}")
            scopes_text = st.text_input(
                "Allowed Project Scopes (comma-separated, e.g. `project:second_brain` or `*`)",
                value=", ".join(rule.allowed_projects),
                key=f"sc_{chosen_host}",
            )
            unscoped_val = st.checkbox("Allow Unscoped Atoms", value=rule.allow_unscoped_atoms, key=f"un_{chosen_host}")
            pref_val = st.checkbox("Allow Owner Subjective Preference Atoms", value=rule.allow_preference_atoms, key=f"pr_{chosen_host}")
            model_val = st.text_input("Default Model Name", value=rule.default_model, key=f"md_{chosen_host}")
            if st.button("💾 Save Host Sharing Policy", key=f"save_{chosen_host}"):
                try:
                    new_scopes = tuple(s.strip() for s in scopes_text.split(",") if s.strip())
                    validated_model = validate_model_identifier(
                        model_val.strip() or rule.default_model,
                        provider=rule.provider,
                    )
                    policies[chosen_host] = HostSharingRule(
                        host_id=rule.host_id,
                        provider=rule.provider,
                        transport=rule.transport,
                        endpoint_url=rule.endpoint_url,
                        default_model=validated_model,
                        api_key_env=rule.api_key_env,
                        enabled=en_val,
                        allowed_projects=new_scopes,
                        allow_unscoped_atoms=unscoped_val,
                        allow_preference_atoms=pref_val,
                        redact_pii=True if rule.transport == "online" else rule.redact_pii,
                        fail_on_secret_detection=True if rule.transport == "online" else rule.fail_on_secret_detection,
                    )
                    saved_to = save_host_policies(policies)
                    st.success(f"Updated policy for `{chosen_host}` at `{saved_to}`.")
                except Exception as exc:
                    st.error(f"Could not save policy: {exc}")

    with tab_fitness:
        st.subheader("Memory Fitness & Procedural Skill Maturity")
        report = compute_memory_fitness_report()
        qs = report["query_summary"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Recall Queries", qs["total_recall_events"])
        m2.metric("Answer Turns", qs["total_answer_events"])
        m3.metric("High-Utility Atoms", len(report["high_utility_verified_atoms"]))
        m4.metric("Pending Bottlenecks", len(report["pending_review_bottlenecks"]))

        if report["high_utility_verified_atoms"]:
            st.markdown("#### Verified High-Utility Memories")
            st.dataframe(pd.DataFrame(report["high_utility_verified_atoms"]), use_container_width=True)
        if report["pending_review_bottlenecks"]:
            st.markdown("#### Highest-Priority Pending Atoms to Review")
            st.dataframe(pd.DataFrame(report["pending_review_bottlenecks"]), use_container_width=True)
        if report["procedure_maturity"]:
            st.markdown("#### Procedural Maturity Stages (`procedural_draft -> verified_procedure -> skill_candidate`)")
            st.dataframe(pd.DataFrame(report["procedure_maturity"]), use_container_width=True)

        st.divider()
        st.markdown("#### Log Completed Task Experience (`4-experiences/`)")
        exp_goal = st.text_input("Task Goal", key="exp_goal")
        exp_res = st.text_area("Outcome & Reflection Summary", key="exp_res")
        exp_proc = st.text_input("Optional Candidate Procedure Title", key="exp_proc")
        exp_prop = st.checkbox("Propose Candidate Procedure to Quarantined `0-raw/inbox/`", value=False, key="exp_prop")
        if st.button("📝 Record Immutable Experience", key="btn_save_exp"):
            try:
                saved_exp = save_experience_record(
                    goal=exp_goal,
                    result_summary=exp_res,
                    success=True,
                    candidate_procedure_title=exp_proc or None,
                    propose_to_inbox=exp_prop,
                )
                st.success(f"Saved `4-experiences/{saved_exp['filename']}` (SHA-256 `{saved_exp['sha256']}`).")
            except Exception as exc:
                st.error(f"Could not save experience: {exc}")

    with tab_playbooks:
        st.subheader("Owner-Gated Skill Playbooks (`brain/5-playbooks/`)")
        st.caption(
            "Playbooks are human-readable operational checklists anchored by exact SHA-256 to approved `procedural` atoms. "
            "They are never executed as code and automatically become inactive if any backing atom is edited, demoted, or receives unacknowledged corrections."
        )

        candidates = inspect_skill_candidates()
        if candidates:
            st.markdown("#### Approved Procedural Atoms & Promotion Readiness")
            st.dataframe(pd.DataFrame(candidates), use_container_width=True)
        else:
            st.info("No currently owner-approved `procedural` atoms found in `2-atoms/`.")

        with st.expander("➕ Promote Approved Procedural Atom(s) to Skill Playbook (CAS-Locked)", expanded=False):
            cand_map = {str(c["atom_id"]): str(c["sha256"]) for c in candidates}
            selected_backing = st.multiselect(
                "Backing Approved Procedural Atoms (CAS SHA-256 bound)",
                options=sorted(cand_map.keys()),
                key="pb_backing_atoms",
            )
            pb_id_in = st.text_input("Playbook ID (slug, e.g. `staged-vector-rebuild`)", key="pb_id_in")
            pb_title_in = st.text_input("Playbook Title", key="pb_title_in")
            pb_actor_in = st.text_input("Human Owner Actor", value="local_dashboard_owner", key="pb_actor_in")
            pb_trigger_in = st.text_input("Trigger & Applicability", key="pb_trigger_in")
            pb_pre_in = st.text_area("Preconditions & Inputs (one per line)", height=80, key="pb_pre_in")
            pb_steps_in = st.text_area(
                "Ordered Steps (one per line — each step must be verbatim-grounded in a backing atom's Claim or Evidence)",
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
            pb_early = st.checkbox("Allow Early Owner Promotion (< 3 grounded citations)", value=False, key="pb_early")
            pb_ack_corr = st.checkbox("Acknowledge Existing Backing Atom Corrections", value=False, key="pb_ack_corr")
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
                    )
                    st.success(f"Promoted `5-playbooks/{promoted_res['filename']}` (SHA-256 `{promoted_res['sha256']}`).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Playbook promotion blocked: {exc}")

        st.divider()
        st.markdown("#### Registered Skill Playbooks & Cryptographic Integrity")
        all_playbooks = list_playbooks(active_only=False)
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
            st.dataframe(pd.DataFrame(pb_rows), use_container_width=True)

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
                            )
                            st.info(f"Deprecated `5-playbooks/{dep_res['filename']}` (new SHA-256 `{dep_res['sha256']}`).")
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
                    plan_result = assemble_executive_plan(req, save_briefing=plan_save)
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
    tab_notes_browse, tab_notes_create, tab_notes_import = st.tabs([
        "📚 Browse, Edit & Verify Excerpt",
        "➕ New Working Note",
        "📦 Bulk Markdown / Obsidian Import",
    ])

    with tab_notes_browse:
        note_q = st.text_input("Search Working Notes (FTS5)", value="", key="notes_fts_q")
        note_items, note_total = list_notes(query=note_q.strip(), limit=100, offset=0)
        st.caption(f"Showing {len(note_items)} of {note_total} working note(s).")
        if not note_items:
            st.info("No working notes match the current filter.")
        else:
            note_choices = {f"{n.title} ({n.filename})": n.filename for n in note_items}
            chosen_label = st.selectbox("Select Working Note", list(note_choices.keys()), key="sel_working_note")
            chosen_fn = note_choices[chosen_label]
            full_note = get_note(chosen_fn)
            st.caption(f"SHA-256: `{full_note.sha256}` | Origin: `{full_note.origin}` | Tags: `{', '.join(full_note.tags) or 'none'}`")
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
                        )
                        st.success(f"Saved `{res['filename']}` (SHA-256 `{res['sha256'][:16]}…`).")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Note update blocked: {exc}")
            with nc2:
                if st.button("🗑️ Delete Note (CAS)", key=f"btn_del_nt_{chosen_fn}"):
                    try:
                        delete_note(chosen_fn, expected_sha256=full_note.sha256)
                        st.info(f"Deleted `{chosen_fn}`.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Delete blocked: {exc}")

            with st.expander("✅ Verify Excerpt into Canonical Approved Atom", expanded=False):
                ver_excerpt = st.text_area(
                    "Exact Excerpt from Saved Note (min 12 chars)",
                    placeholder="Paste exact sentence/clause from the saved note above...",
                    height=90,
                    key=f"ver_exc_{chosen_fn}",
                )
                ver_claim = st.text_input("Optional Distilled Claim (defaults to excerpt)", value="", key=f"ver_clm_{chosen_fn}")
                ver_actor = st.text_input("Human Owner Actor", value="local_dashboard_owner", key=f"ver_act_{chosen_fn}")
                if st.button("🔒 Snapshot & Verify Excerpt", key=f"btn_ver_nt_{chosen_fn}"):
                    try:
                        vres = verify_note_excerpt(
                            chosen_fn,
                            ver_excerpt,
                            actor=ver_actor,
                            expected_note_sha256=full_note.sha256,
                            claim=ver_claim.strip() or None,
                        )
                        st.success(
                            f"Promoted approved atom `2-atoms/{vres['atom_filename']}` anchored to immutable snapshot `{vres['snapshot_rel_path']}`."
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
                )
                st.success(f"Created `brain/notes/{created['filename']}` (SHA-256 `{created['sha256'][:16]}…`).")
            except NoteError as exc:
                st.error(f"Could not create note: {exc}")

    with tab_notes_import:
        st.caption("Idempotently import an Obsidian vault or Markdown folder into `brain/notes/` (`owner_import` origin, never auto-approved).")
        imp_folder = st.text_input("Source Folder Path", placeholder=r"C:\Users\you\Documents\ObsidianVault", key="imp_folder_path")
        imp_tags = st.text_input("Optional Tags to Apply (comma-separated)", value="imported", key="imp_folder_tags")
        imp_dry = st.checkbox("Dry Run (preview counts without writing files)", value=True, key="imp_dry_run")
        if st.button("📦 Run Bulk Import", key="btn_bulk_import"):
            if not imp_folder.strip():
                st.warning("Provide a local folder path to import.")
            else:
                try:
                    stats = import_folder(
                        imp_folder.strip(),
                        tags=tuple(t.strip() for t in imp_tags.split(",") if t.strip()),
                        dry_run=imp_dry,
                    )
                    st.json(stats.as_dict())
                except Exception as exc:
                    st.error(f"Bulk import blocked: {exc}")


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
        ledger_state = verify_vault_ledger()
        mc1, mc2, mc3 = st.columns(3)
        mc1.metric("Hash Chain Valid", "YES" if ledger_state["chain_valid"] else "TAMPERED / ERROR")
        mc2.metric("Ledger Entries", ledger_state["entry_count"])
        mc3.metric("Verified Merkle Leaves", ledger_state["leaf_count"])
        st.code(
            f"Live Merkle Root : {ledger_state['live_merkle_root']}\n"
            f"Matches Latest   : {ledger_state['matches_latest_checkpoint']}\n"
            f"Approved Atoms   : {ledger_state['approved_atoms']} | Accepted Playbooks: {ledger_state['accepted_playbooks']}",
            language="text",
        )
        if ledger_state.get("errors"):
            st.error("Ledger verification errors: " + "; ".join(ledger_state["errors"]))

        lc1, lc2 = st.columns(2)
        with lc1:
            ckpt_actor = st.text_input("Owner Actor", value="local_dashboard_owner", key="ldg_actor")
            ckpt_note = st.text_input("Checkpoint Note", placeholder="e.g. Post-review release checkpoint", key="ldg_note")
            if st.button("🔏 Commit Signed Merkle Checkpoint", key="btn_commit_ledger"):
                try:
                    entry = commit_ledger_checkpoint(actor=ckpt_actor, note=ckpt_note)
                    st.success(f"Committed checkpoint #{entry['seq']} (`{entry['entry_sha256'][:16]}…`).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Checkpoint commit failed: {exc}")
        with lc2:
            prove_target = st.text_input("Generate Portable Merkle Proof for Approved Atom", placeholder="e.g. structured-logging.md", key="ldg_prove_atom")
            if st.button("🧾 Prove Atom Membership", key="btn_prove_atom"):
                if not prove_target.strip():
                    st.warning("Enter an approved atom filename.")
                else:
                    try:
                        proof = prove_atom_membership(prove_target.strip())
                        proof["verified_offline"] = verify_merkle_proof(proof)
                        st.json(proof)
                    except Exception as exc:
                        st.error(f"Proof generation blocked: {exc}")

    with tab_radar:
        rc1, rc2 = st.columns(2)
        with rc1:
            radar_proj = st.text_input("Optional Project Scope", placeholder="e.g. project:second_brain", key="radar_proj")
        with rc2:
            radar_inc_pending = st.checkbox("Include Pending Drafts in Contradiction Scan", value=True, key="radar_pending")
        if st.button("📡 Scan Vault for Contradictions & Orphans", key="btn_scan_radar"):
            graph_info = build_knowledge_graph(project_scope=radar_proj.strip() or None, include_pending=False)
            radar_info = detect_contradictions(project_scope=radar_proj.strip() or None, include_pending=radar_inc_pending)
            gc1, gc2, gc3, gc4 = st.columns(4)
            gc1.metric("Approved Graph Nodes", graph_info["node_count"])
            gc2.metric("Graph Edges", graph_info["edge_count"])
            gc3.metric("Contradictions", radar_info["contradiction_count"])
            gc4.metric("High Severity", radar_info["high_severity_count"])
            if radar_info["alerts"]:
                for alert in radar_info["alerts"]:
                    st.code(
                        f"[{alert['severity'].upper()}] {', '.join(alert['reasons'])} (jaccard={alert['jaccard']})\n"
                        f"  Atom A: {alert['atom_a']['filename']} ({alert['atom_a']['review_status']}) -> {alert['atom_a']['claim']}\n"
                        f"  Atom B: {alert['atom_b']['filename']} ({alert['atom_b']['review_status']}) -> {alert['atom_b']['claim']}",
                        language="text",
                    )
            else:
                st.success("Zero claim polarity or numeric contradictions detected in selected scope.")
            if radar_info["orphan_approved_atoms"]:
                st.caption("Orphan approved atoms (no explicit `[[wikilink]]` or `links:` edges): " + ", ".join(radar_info["orphan_approved_atoms"]))

    with tab_timetravel:
        sc1, sc2 = st.columns(2)
        with sc1:
            snap_label = st.text_input("Snapshot Label", placeholder="e.g. pre-refactor", key="tt_label")
            snap_actor = st.text_input("Owner Actor", value="local_dashboard_owner", key="tt_actor")
            if st.button("📸 Capture Vault Snapshot", key="btn_take_snap"):
                try:
                    snap_doc = create_snapshot(label=snap_label, actor=snap_actor)
                    st.success(f"Created snapshot `{snap_doc['snapshot_id']}` ({snap_doc['atom_count']} approved atoms, Merkle `{snap_doc['merkle_root'][:16]}…`).")
                    st.rerun()
                except Exception as exc:
                    st.error(f"Snapshot failed: {exc}")

        snaps = list_snapshots()
        with sc2:
            st.caption(f"Recorded snapshots: {len(snaps)}")
            if snaps:
                st.dataframe(pd.DataFrame(snaps), use_container_width=True)

        if snaps:
            snap_ids = [s["snapshot_id"] for s in snaps]
            dc1, dc2 = st.columns(2)
            with dc1:
                from_snap = st.selectbox("From Snapshot", snap_ids, key="tt_from")
                to_snap = st.selectbox("To Snapshot (or `live`)", ["live", *snap_ids], key="tt_to")
                if st.button("🔍 Semantic Diff", key="btn_diff_snap"):
                    try:
                        st.json(diff_snapshots(from_snap, to_snap))
                    except Exception as exc:
                        st.error(f"Snapshot diff failed: {exc}")
            with dc2:
                pit_snap = st.selectbox("Point-in-Time Snapshot", snap_ids, key="tt_pit_snap")
                pit_query = st.text_input("Historical Recall Query", placeholder="What was approved at this snapshot?", key="tt_pit_q")
                pit_proj = st.text_input("Optional Project Scope", value="", key="tt_pit_proj")
                if st.button("⏪ Recall at Snapshot", key="btn_pit_recall"):
                    try:
                        st.json(recall_at_snapshot(pit_query, pit_snap, project_scope=pit_proj.strip() or None))
                    except Exception as exc:
                        st.error(f"Point-in-time recall failed: {exc}")


# PAGE 5: SYSTEM CONTROL
elif nav_selection == "⚙️ System Control":
    st.title("⚙️ System Control")
    st.subheader("Manage the Engine Core Operations")
    
    c1, c2 = st.columns(2)
    
    with c1:
        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
        st.markdown("### ⚙️ Engine Shift Playbooks")
        st.markdown("Process raw inbox notes, verify consistency, and cluster atomic information.")
        
        if st.button("🔥 Run Refinery (Night Shift)"):
            proc = run_script_process("scripts/ollama_refinery.py")
            stream_logs_ui(proc, "Ollama Refinery Engine running")
            
        if st.button("📚 Run Editor (Morning Shift)"):
            proc = run_script_process("scripts/ollama_editor.py")
            stream_logs_ui(proc, "Ollama Editor clustering & synthesis running")

        if st.button("Run System Doctor"):
            proc = run_script_process("scripts/doctor.py")
            stream_logs_ui(proc, "Second Brain system doctor running")

        if st.button("📊 Run Comparative SOTA Benchmark"):
            proc = run_script_process("scripts/sota_benchmark.py")
            stream_logs_ui(proc, "Comparative SOTA benchmark running")
            
        st.markdown("</div>", unsafe_allow_html=True)
        
    with c2:
        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
        st.markdown("### 📯 Watchdog Real-time Daemon")
        st.markdown("Check status or spawn/manage the real-time folder daemon.")
        
        # We can write run_daemon launcher check
        daemon_status = st.empty()
        daemon_status.info("ℹ️ Launch the daemon via terminal launcher: `run_daemon.ps1`")

        st.markdown("### 🔌 Local IDE & Agent Connector Status")
        if st.checkbox("Show IDE Connector Status"):
            st.dataframe(pd.DataFrame(ide_connector_status()), use_container_width=True)
        
        st.markdown("### 📜 System Instruction Rules")
        if st.checkbox("View `house-rules.md`"):
            rules_content = read_house_rules()
            st.code(rules_content, language="markdown")
        st.markdown("</div>", unsafe_allow_html=True)
        
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
                if not brief_path:
                    st.error("Selected briefing path is invalid.")
                    st.stop()
                with open(brief_path, "r", encoding="utf-8") as f:
                    st.code(f.read(), language="markdown")
