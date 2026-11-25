"""Knowledge-graph drawing for the dashboard.

``bind`` receives the dashboard module so vault paths and the wikilink helper
stay the ones the dashboard already configured. Load order is unchanged:
the dashboard binds this module before the sidebar runs.
"""

from __future__ import annotations

import html
import os
import re

import networkx as nx
import plotly.graph_objects as go

from scripts.config import _is_reparse_or_link, is_content_markdown, safe_file_in_dir
from scripts.knowledge_graph import build_knowledge_graph, detect_contradictions

_APP = None


def bind(module) -> None:
    global _APP
    _APP = module


def _require():
    if _APP is None:
        raise RuntimeError("dashboard graph_view.bind() was not called")
    return _APP


_EDGE_PRIORITY = {"contested_by": 4, "superseded_by": 3, "link": 2, "topic_overlap": 1}

def build_network_graph(
    *,
    project_scope: str | None = None,
    include_pending: bool = False,
    include_threads: bool = True,
    include_topic_overlap: bool = True,
    as_of: str | None = None,
    include_expired: bool = True,
):
    app = _require()
    BRAIN_DIR = app.BRAIN_DIR
    ATOMS_DIR = app.ATOMS_DIR
    THREADS_DIR = app.THREADS_DIR
    safe_wikilink_filename = app.safe_wikilink_filename
    G = nx.Graph()
    clean_scope = (project_scope or "").strip() or None
    clean_as_of = str(as_of).strip() if (as_of and str(as_of).strip()) else None
    kg = build_knowledge_graph(
        brain_dir=BRAIN_DIR,
        atoms_dir=ATOMS_DIR,
        project_scope=clean_scope,
        include_pending=include_pending,
        as_of=clean_as_of,
        include_expired=include_expired,
    )
    G.graph["kg_summary"] = kg
    timeline_kg = (
        kg
        if (clean_as_of is None and include_expired)
        else build_knowledge_graph(
            brain_dir=BRAIN_DIR,
            atoms_dir=ATOMS_DIR,
            project_scope=clean_scope,
            include_pending=include_pending,
            as_of=None,
            include_expired=True,
        )
    )
    timeline_dates = sorted({
        d[:10]
        for n in timeline_kg.get("nodes", [])
        for d in (str(n.get("valid_from") or "").strip(), str(n.get("valid_until") or "").strip())
        if d and len(d) >= 10
    })
    G.graph["timeline_dates"] = timeline_dates

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
            valid_from=str(node.get("valid_from") or ""),
            valid_until=str(node.get("valid_until") or ""),
            temporal_status=str(node.get("temporal_status") or "active"),
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
        temporal_lbl = str(ndata.get("temporal_status") or "active")
        valid_from_lbl = str(ndata.get("valid_from") or "")
        valid_until_lbl = str(ndata.get("valid_until") or "")
        claim_snip = (ndata.get("claim") or "")[:120]
        badge = "Verified Approved" if is_verified else ("Synthesis Thread" if ntype == "thread" else f"Unverified ({esc_status})")
        hover_parts = [f"<b>{title}</b>", f"Status: {badge}", f"File: {esc_file}"]
        if ntype == "atom":
            win_lbl = f"{valid_from_lbl or '-inf'} -> {valid_until_lbl or '+inf'}"
            hover_parts.append(f"Temporal: {html.escape(temporal_lbl)} ({html.escape(win_lbl)})")
        if claim_snip:
            hover_parts.append(f"Claim: {claim_snip}")
        node_text.append("<br>".join(hover_parts))

        if ntype == "thread":
            node_color.append("#00f2fe")
            node_size.append(15 if is_3d else 22)
        elif sup_by:
            node_color.append("#6c757d")
            node_size.append(8 if is_3d else 12)
        elif temporal_lbl in {"expired", "not_yet_valid", "invalid_window"}:
            node_color.append("#7aa2f7" if temporal_lbl == "not_yet_valid" else "#9aa5ce")
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


