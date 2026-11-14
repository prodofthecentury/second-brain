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
    ATOMS_DIR, THREADS_DIR, RAW_DIR, INBOX_DIR, SOURCES_DIR, BRIEFINGS_DIR, SIDECAR_DIR,
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
from scripts.answer_policy import ANSWER_DRAFT_SCHEMA, AnswerRequest, answer_from_memory, build_grounding_prompt, validate_standard_answer
from scripts.atom_mutator import apply_atom_edit, apply_atom_merge
from scripts.review_atom import apply_review_transition, approved_revision_is_current
from scripts.memory_review import parse_atom_record
from scripts.review_atom import review_lock
from scripts.review_details import atom_review_details
from scripts.dashboard_auth import assert_loopback_address, require_dashboard_login

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

def get_ollama_model_names() -> list[str]:
    try:
        response = ollama.list()
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
    ["💬 Chat with Memory", "🕸️ Knowledge Graph", "🗄️ Vault Explorer", "📥 Inbox & Web Scout", "⚙️ System Control"],
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
            with st.spinner("🧠 Querying vector memories..."):
                direct_refs, neighbor_refs = search_memory(
                    user_prompt, 
                    n_results=rag_n_results, 
                    follow_links=rag_follow,
                    projects=project_tuple,
                    retrieval_mode=rag_retrieval_mode,
                    allow_cross_project=rag_cross_project,
                )
                
            all_refs = direct_refs + neighbor_refs

            if all_refs:
                ref_text = build_grounding_prompt(
                    AnswerRequest(query=user_prompt, mode=rag_answer_mode, result_limit=rag_n_results),
                    all_refs,
                )
                st.info(f"📚 Retrieved {len(direct_refs)} direct atoms and {len(neighbor_refs)} linked atoms.")
            else:
                ref_text = ""
                st.warning("⚠ No relevant owner-approved memory found. The assistant will abstain.")
                
            response_container = st.empty()
            full_response = ""
            if not ref_text.strip():
                full_response = answer_from_memory(ref_text, lambda: "")
                response_container.text(full_response)
                st.session_state.chat_history.append({
                    "role": "assistant",
                    "content": full_response,
                    "claims": (),
                })
            else:
                ollama_msgs = [{"role": "system", "content": ref_text}]
                for past_msg in st.session_state.chat_history[-10:]:
                    if past_msg["role"] == "user":
                        ollama_msgs.append({"role": "user", "content": past_msg.get("content", "")})
                    elif past_msg.get("claims"):
                        replay = validate_standard_answer(
                            past_msg.get("draft_json", ""),
                            past_msg.get("retrieved_atoms", []),
                            atoms_dir=ATOMS_DIR,
                            mode=past_msg.get("answer_mode", "standard"),
                        )
                        if replay.answerable:
                            ollama_msgs.append({"role": "assistant", "content": replay.text})

                try:
                    response_stream = ollama.chat(
                        model=st_model, messages=ollama_msgs, format=ANSWER_DRAFT_SCHEMA, stream=True
                    )
                    for chunk in response_stream:
                        content = chunk["message"]["content"]
                        full_response += content
                    model_draft_json = full_response
                    answer = validate_standard_answer(
                        full_response, all_refs, atoms_dir=ATOMS_DIR, mode=rag_answer_mode
                    )
                    full_response = answer.text
                    response_container.text(full_response)
                    st.session_state.chat_history.append({
                        "role": "assistant",
                        "content": full_response if not answer.answerable else "",
                        "claims": answer.claims,
                        "draft_json": model_draft_json if answer.answerable else "",
                        "retrieved_atoms": all_refs if answer.answerable else [],
                        "answer_mode": rag_answer_mode,
                    })
                except Exception as e:
                    st.error(f"Error connecting to Ollama: {e}")


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
            "3-threads (Threads)",
            "sources (Web Pages)",
            "0-raw (Inbox)",
            "0-raw/inbox (Quarantined MCP Inbox)",
        ],
    )
    
    # Map category to folder path
    folder_map = {
        "2-atoms (Atoms)": ATOMS_DIR,
        "3-threads (Threads)": THREADS_DIR,
        "sources (Web Pages)": SOURCES_DIR,
        "0-raw (Inbox)": RAW_DIR,
        "0-raw/inbox (Quarantined MCP Inbox)": INBOX_DIR,
    }
    dir_path = folder_map[vault_tab]
    
    if not os.path.exists(dir_path):
        st.info("This vault category has no files yet.")
    else:
        files = sorted([f for f in os.listdir(dir_path) if f.endswith(".md") or f.endswith(".intake")])
        if dir_path in (ATOMS_DIR, INBOX_DIR):
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
                
                content = f"""<!-- Capture via Dashboard on {today()} -->
<!-- Source: {inbox_source} -->

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
            
        st.markdown("</div>", unsafe_allow_html=True)
        
    with c2:
        st.markdown("<div class='glass-card'>", unsafe_allow_html=True)
        st.markdown("### 📯 Watchdog Real-time Daemon")
        st.markdown("Check status or spawn/manage the real-time folder daemon.")
        
        # We can write run_daemon launcher check
        daemon_status = st.empty()
        daemon_status.info("ℹ️ Launch the daemon via terminal launcher: `run_daemon.ps1`")
        
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
