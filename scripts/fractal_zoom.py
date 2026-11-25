# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Fractal / Hierarchical Zoom Retrieval (Epic 2D.1).

Builds and queries a disposable 6-level hierarchical projection in
``brain/.sidecar/cognitive_hierarchy.sqlite3``:

  L0 Vault -> L1 Domain -> L2 Project -> L3 Concept/Thread -> L4 Approved Atom -> L5 Byte Quote

Only currently owner-approved (``accepted``, non-superseded) atoms enter L4/L5,
and every project-scoped query obeys strict ``project_tags_match`` containment.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("fractal_zoom", "scripts.fractal_zoom"):
    sys.modules.setdefault("fractal_zoom", sys.modules[__name__])
    sys.modules.setdefault("scripts.fractal_zoom", sys.modules[__name__])

from config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    THREADS_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from memory_format import evaluate_temporal_validity, split_frontmatter  # noqa: E402
from memory_review import parse_atom_record_text, parse_frontmatter_values, parse_scalar_list, section_text  # noqa: E402
from recall import normalize_project_tag, project_tags_match  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

ZOOM_LEVELS: tuple[str, ...] = ("L0", "L1", "L2", "L3", "L4", "L5")
LEVEL_NAMES: dict[str, str] = {
    "L0": "vault",
    "L1": "domain",
    "L2": "project",
    "L3": "concept",
    "L4": "atom",
    "L5": "raw_quote",
}
_WORD_RE = re.compile(r"[a-zA-Z0-9_:-]{2,}")


def hierarchy_db_path(brain_dir: str | Path | None = None) -> Path:
    base, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    return Path(base) / ".sidecar" / "cognitive_hierarchy.sqlite3"


def _connect(db_path: Path) -> sqlite3.Connection:
    if os.path.lexists(str(db_path.parent)) and _is_reparse_or_link(str(db_path.parent)):
        raise PermissionError(f"Refusing symlinked sidecar dir: {db_path.parent}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(str(db_path)) and _is_reparse_or_link(str(db_path)):
        raise PermissionError(f"Refusing symlinked hierarchy db: {db_path}")
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zoom_nodes (
            node_id TEXT PRIMARY KEY,
            level TEXT NOT NULL,
            level_rank INTEGER NOT NULL,
            parent_id TEXT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            domain TEXT NOT NULL,
            project_scope TEXT NOT NULL,
            project_tags_json TEXT NOT NULL DEFAULT '[]',
            atom_filename TEXT NOT NULL,
            source_path TEXT NOT NULL,
            byte_start INTEGER NOT NULL DEFAULT 0,
            byte_end INTEGER NOT NULL DEFAULT 0,
            tokens_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS zoom_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(zoom_nodes)").fetchall()}
    if "project_tags_json" not in cols:
        conn.execute("ALTER TABLE zoom_nodes ADD COLUMN project_tags_json TEXT NOT NULL DEFAULT '[]'")
        conn.execute("DELETE FROM zoom_meta WHERE key = 'signature'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zoom_level_proj ON zoom_nodes(level, project_scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zoom_parent ON zoom_nodes(parent_id)")
    return conn


def _tokenize(text: str) -> list[str]:
    seen: list[str] = []
    for tok in _WORD_RE.findall((text or "").lower()):
        if tok not in seen:
            seen.append(tok)
    return seen


def _safe_quote_source_path(bpath: Path, src_rel: str) -> Path | None:
    """Resolve ``src_rel`` strictly under ``brain/0-raw/`` or ``brain/sources/`` without following links."""
    raw_rel = (src_rel or "").strip()
    if (
        not raw_rel
        or raw_rel.startswith(("http://", "https://", "/", "\\"))
        or ":" in raw_rel
        or "\x00" in raw_rel
    ):
        return None
    norm_rel = raw_rel.replace("\\", "/")
    parts = [p for p in norm_rel.split("/") if p]
    if not parts or parts[0] not in {"0-raw", "sources"} or any(p in {".", ".."} for p in parts):
        return None

    cur = bpath
    if _is_reparse_or_link(str(cur)):
        return None
    for part in parts:
        cur = cur / part
        if not os.path.lexists(str(cur)) or _is_reparse_or_link(str(cur)):
            return None
    if not cur.is_file():
        return None
    try:
        bpath_real = bpath.resolve()
        allowed_root_real = (bpath / parts[0]).resolve()
        cand_real = cur.resolve()
        if not cand_real.is_relative_to(bpath_real) or not cand_real.is_relative_to(allowed_root_real):
            return None
    except (OSError, ValueError):
        return None
    return cand_real


def _extract_quote_slice(raw_path: Path, b_start: int, b_end: int, fallback: str) -> str:
    if raw_path.is_file() and not _is_reparse_or_link(str(raw_path)) and b_end > b_start >= 0:
        try:
            data = raw_path.read_bytes()
            if b_end <= len(data):
                snippet = data[b_start:b_end].decode("utf-8", errors="replace").strip()
                if snippet:
                    return snippet
        except OSError:
            pass
    return (fallback or "").strip()


def _atoms_signature(atoms_dir: Path, threads_dir: Path, brain_dir: Path | None = None) -> str:
    h = hashlib.sha256()
    h.update(b"schema:v2\n")
    h.update(f"today:{datetime.date.today().isoformat()}\n".encode("utf-8"))
    for folder in (atoms_dir, threads_dir):
        if folder.is_dir() and not _is_reparse_or_link(str(folder)):
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
            for entry in entries:
                if not is_content_markdown(entry.name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                h.update(f"{folder.name}/{entry.name}:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
    b_root = brain_dir if brain_dir is not None else atoms_dir.parent
    dec_path = b_root / "0-raw" / "archive" / "decision-record.jsonl"
    if os.path.lexists(str(dec_path)):
        if _is_reparse_or_link(str(dec_path)):
            h.update(b"decisions:symlink\n")
        else:
            try:
                dst = os.lstat(str(dec_path))
                h.update(f"decisions:{dst.st_mtime_ns}:{dst.st_size}\n".encode("utf-8"))
            except OSError:
                h.update(b"decisions:err\n")
    else:
        h.update(b"decisions:missing\n")
    return h.hexdigest()


def sync_cognitive_hierarchy(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Rebuild or incrementally refresh the 6-level L0-L5 fractal zoom tree."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)
    tpath = bpath / "3-threads"
    dbp = hierarchy_db_path(resolved_brain)
    sig = _atoms_signature(apath, tpath, brain_dir=bpath)

    with closing(_connect(dbp)) as conn:
        row = conn.execute("SELECT value FROM zoom_meta WHERE key = 'signature'").fetchone()
        if not force and row is not None and row["value"] == sig:
            cnt = int(conn.execute("SELECT COUNT(*) FROM zoom_nodes").fetchone()[0])
            return {"status": "current", "signature": sig, "node_count": cnt, "db_path": str(dbp)}

        nodes: dict[str, dict[str, Any]] = {}
        nodes["L0:vault"] = {
            "node_id": "L0:vault",
            "level": "L0",
            "level_rank": 0,
            "parent_id": "",
            "title": "Second Brain Vault",
            "summary": "Root L0 knowledge landscape across domains, projects, concepts, approved atoms, and raw quotes.",
            "domain": "all",
            "project_scope": "*",
            "project_tags": [],
            "atom_filename": "",
            "source_path": "",
            "byte_start": 0,
            "byte_end": 0,
            "tokens": ["vault", "second_brain", "overview"],
        }

        approved_count = 0
        domains_seen: set[str] = set()
        projects_seen: set[str] = set()

        if apath.is_dir() and not _is_reparse_or_link(str(apath)):
            for entry in sorted(os.scandir(apath), key=lambda e: e.name):
                if not is_content_markdown(entry.name):
                    continue
                atom_p = apath / entry.name
                if _is_reparse_or_link(str(atom_p)) or not atom_p.is_file():
                    continue
                try:
                    raw_text = atom_p.read_text(encoding="utf-8")
                    rec = parse_atom_record_text(entry.name, raw_text)
                except (OSError, UnicodeDecodeError, ValueError):
                    continue
                if (
                    rec.get("review_status") != "accepted"
                    or rec.get("superseded_by")
                    or not approved_revision_is_current(str(atom_p), brain_dir=resolved_brain, atoms_dir=resolved_atoms)
                ):
                    continue

                parsed = split_frontmatter(raw_text)
                fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}

                # Exclude preference atoms and inactive/invalid temporal atoms
                ep_label = str(rec.get("epistemic_label") or fm_vals.get("epistemic_label") or "claim").strip().lower()
                ep_contract = str(rec.get("epistemic_contract") or fm_vals.get("epistemic_contract") or "").strip()
                if ep_label == "preference" or bool(ep_contract):
                    continue
                tv = evaluate_temporal_validity(rec.get("valid_from"), rec.get("valid_until"))
                if tv["status"] == "invalid_window" or not tv["active"]:
                    continue

                approved_count += 1
                title = str(fm_vals.get("title") or rec.get("id") or entry.name[:-3]).strip()
                claim = str(rec.get("claim") or "").strip()
                evidence = str(rec.get("evidence") or "").strip()
                contexts = [str(c).strip() for c in (rec.get("contexts") or []) if str(c).strip()]
                tags_raw = fm_vals.get("tags", [])
                tags = parse_scalar_list(tags_raw) if isinstance(tags_raw, str) else list(tags_raw or [])
                proj_tags = sorted(
                    {
                        normalize_project_tag(c)
                        for c in contexts
                        if c.lower().startswith("project:") and normalize_project_tag(c)
                    }
                )
                project_scope = ",".join(proj_tags) if proj_tags else "global"
                non_proj_ctx = [c.lower() for c in contexts if not c.lower().startswith("project:")]
                domain = non_proj_ctx[0] if non_proj_ctx else "general"

                domains_seen.add(domain)
                projects_seen.add(project_scope)

                l1_id = f"L1:{domain}"
                if l1_id not in nodes:
                    nodes[l1_id] = {
                        "node_id": l1_id,
                        "level": "L1",
                        "level_rank": 1,
                        "parent_id": "L0:vault",
                        "title": f"Domain: {domain}",
                        "summary": f"Knowledge domain '{domain}'",
                        "domain": domain,
                        "project_scope": "*",
                        "project_tags": [],
                        "atom_filename": "",
                        "source_path": "",
                        "byte_start": 0,
                        "byte_end": 0,
                        "tokens": _tokenize(f"domain {domain}"),
                    }

                l2_id = f"L2:{domain}|{project_scope}"
                if l2_id not in nodes:
                    nodes[l2_id] = {
                        "node_id": l2_id,
                        "level": "L2",
                        "level_rank": 2,
                        "parent_id": l1_id,
                        "title": f"Project: {project_scope}",
                        "summary": f"Scoped memories for {project_scope} in domain {domain}",
                        "domain": domain,
                        "project_scope": project_scope,
                        "project_tags": list(proj_tags),
                        "atom_filename": "",
                        "source_path": "",
                        "byte_start": 0,
                        "byte_end": 0,
                        "tokens": _tokenize(f"project {project_scope} {domain}"),
                    }

                concept_slug = str(tags[0] if tags else rec.get("memory_type") or "semantic").strip().lower()
                if concept_slug.startswith("project:"):
                    concept_slug = str(rec.get("memory_type") or "semantic").strip().lower()
                l3_id = f"L3:{domain}|{project_scope}|{concept_slug}"
                if l3_id not in nodes:
                    nodes[l3_id] = {
                        "node_id": l3_id,
                        "level": "L3",
                        "level_rank": 3,
                        "parent_id": l2_id,
                        "title": f"Concept: {concept_slug}",
                        "summary": f"Cluster '{concept_slug}' under {project_scope}",
                        "domain": domain,
                        "project_scope": project_scope,
                        "project_tags": list(proj_tags),
                        "atom_filename": "",
                        "source_path": "",
                        "byte_start": 0,
                        "byte_end": 0,
                        "tokens": _tokenize(f"{concept_slug} {project_scope} {domain}"),
                    }
                else:
                    merged_tok = set(nodes[l3_id]["tokens"]) | set(_tokenize(f"{title} {claim}"))
                    nodes[l3_id]["tokens"] = sorted(merged_tok)[:64]

                src_rel = str(fm_vals.get("evidence_source") or (rec.get("sources") or [""])[0] or "").strip()
                try:
                    b_start = int(fm_vals.get("source_byte_start") or 0)
                    b_end = int(fm_vals.get("source_byte_end") or 0)
                except (TypeError, ValueError):
                    b_start, b_end = 0, 0
                if b_start < 0 or b_end < 0:
                    b_start, b_end = 0, 0

                l4_id = f"L4:{entry.name}"
                l4_tokens = _tokenize(f"{entry.name} {title} {claim} {' '.join(tags)} {' '.join(contexts)}")
                nodes[l4_id] = {
                    "node_id": l4_id,
                    "level": "L4",
                    "level_rank": 4,
                    "parent_id": l3_id,
                    "title": title,
                    "summary": claim or title,
                    "domain": domain,
                    "project_scope": project_scope,
                    "project_tags": list(proj_tags),
                    "atom_filename": entry.name,
                    "source_path": src_rel,
                    "byte_start": b_start,
                    "byte_end": b_end,
                    "tokens": l4_tokens,
                }
                # Enrich ancestor tokens for L0-L2 drill-down routing
                for anc_id in (l2_id, l1_id):
                    nodes[anc_id]["tokens"] = sorted(set(nodes[anc_id]["tokens"]) | set(l4_tokens))[:96]

                quote_text = evidence
                safe_src = _safe_quote_source_path(bpath, src_rel)
                if safe_src is not None:
                    quote_text = _extract_quote_slice(safe_src, b_start, b_end, evidence)
                l5_id = f"L5:{entry.name}"
                nodes[l5_id] = {
                    "node_id": l5_id,
                    "level": "L5",
                    "level_rank": 5,
                    "parent_id": l4_id,
                    "title": f"Quote: {src_rel or entry.name}",
                    "summary": quote_text or claim,
                    "domain": domain,
                    "project_scope": project_scope,
                    "project_tags": list(proj_tags),
                    "atom_filename": entry.name,
                    "source_path": src_rel,
                    "byte_start": b_start,
                    "byte_end": b_end,
                    "tokens": _tokenize(f"{quote_text} {src_rel} {title}"),
                }

        nodes["L0:vault"]["summary"] = (
            f"Vault holds {approved_count} verified atoms across {len(domains_seen)} domain(s) "
            f"and {len(projects_seen)} project scope(s)."
        )
        all_tokens: set[str] = set()
        for nd in nodes.values():
            all_tokens.update(nd["tokens"])
        nodes["L0:vault"]["tokens"] = sorted(all_tokens)[:128]

        with conn:
            conn.execute("DELETE FROM zoom_nodes")
            for nd in nodes.values():
                conn.execute(
                    """
                    INSERT INTO zoom_nodes (
                        node_id, level, level_rank, parent_id, title, summary,
                        domain, project_scope, project_tags_json, atom_filename, source_path,
                        byte_start, byte_end, tokens_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        nd["node_id"],
                        nd["level"],
                        int(nd["level_rank"]),
                        nd["parent_id"],
                        nd["title"],
                        nd["summary"],
                        nd["domain"],
                        nd["project_scope"],
                        json.dumps(nd.get("project_tags") or [], ensure_ascii=False),
                        nd["atom_filename"],
                        nd["source_path"],
                        int(nd["byte_start"]),
                        int(nd["byte_end"]),
                        json.dumps(nd["tokens"], ensure_ascii=False),
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO zoom_meta(key, value) VALUES ('signature', ?)",
                (sig,),
            )

    return {
        "status": "rebuilt",
        "signature": sig,
        "node_count": len(nodes),
        "approved_atoms": approved_count,
        "db_path": str(dbp),
    }


def _row_allowed_for_project(row: sqlite3.Row, project: str | None) -> bool:
    if not project:
        return True
    scope_tag = normalize_project_tag(project)
    if not scope_tag:
        return True
    if row["level"] in {"L0", "L1"} or str(row["project_scope"] or "") == "*":
        return True
    p_tags: list[str] = []
    if "project_tags_json" in row.keys():
        try:
            raw_list = json.loads(row["project_tags_json"] or "[]")
            if isinstance(raw_list, list):
                p_tags = [normalize_project_tag(str(t)) for t in raw_list if str(t).strip().lower().startswith("project:")]
        except Exception:
            p_tags = []
    if not p_tags:
        p_scope = str(row["project_scope"] or "").strip()
        if p_scope.startswith("project:"):
            p_tags = [normalize_project_tag(p) for p in p_scope.split(",") if p.strip().lower().startswith("project:")]
    if not p_tags:
        return False
    return all(bool(project_tags_match((t,), (scope_tag,))) for t in p_tags)


def _score_node(row: sqlite3.Row, q_tokens: set[str]) -> float:
    if not q_tokens:
        return 1.0
    try:
        toks = set(json.loads(row["tokens_json"]))
    except Exception:
        toks = set()
    overlap = len(q_tokens & toks)
    summary_low = str(row["summary"] or "").lower()
    phrase_bonus = sum(1 for qt in q_tokens if qt in summary_low)
    return float(overlap * 2.0 + phrase_bonus * 0.5)


def zoom_retrieve(
    query: str,
    *,
    target_level: str = "L5",
    parent_id: str | None = None,
    project: str | None = None,
    limit: int = 8,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Perform top-down L0 -> L5 progressive narrowing or explicit child drill-down."""
    lvl = (target_level or "L5").strip().upper()
    if lvl not in LEVEL_NAMES:
        raise ValueError(f"Unsupported zoom level {target_level!r}; expected one of {ZOOM_LEVELS}.")
    capped_limit = max(1, min(30, int(limit)))
    sync_cognitive_hierarchy(brain_dir=brain_dir, atoms_dir=atoms_dir)
    dbp = hierarchy_db_path(brain_dir)
    q_tokens = set(_tokenize(query))

    with closing(_connect(dbp)) as conn:
        if parent_id:
            rows = conn.execute("SELECT * FROM zoom_nodes WHERE parent_id = ?", (parent_id,)).fetchall()
            filtered = [r for r in rows if _row_allowed_for_project(r, project)]
            scored = sorted(
                filtered,
                key=lambda r: (-_score_node(r, q_tokens), int(r["level_rank"]), str(r["node_id"])),
            )[:capped_limit]
            return {
                "query": query,
                "mode": "drill_down",
                "parent_id": parent_id,
                "target_level": lvl,
                "project": project,
                "nodes": [
                    {
                        "node_id": str(r["node_id"]),
                        "level": str(r["level"]),
                        "level_name": LEVEL_NAMES.get(str(r["level"]), ""),
                        "parent_id": str(r["parent_id"]),
                        "title": str(r["title"]),
                        "summary": str(r["summary"]),
                        "domain": str(r["domain"]),
                        "project_scope": str(r["project_scope"]),
                        "atom_filename": str(r["atom_filename"]),
                        "source_path": str(r["source_path"]),
                        "byte_start": int(r["byte_start"]),
                        "byte_end": int(r["byte_end"]),
                        "score": round(_score_node(r, q_tokens), 3),
                    }
                    for r in scored
                ],
                "path": [],
            }

        target_rank = ZOOM_LEVELS.index(lvl)
        frontier_parents: list[str] | None = None
        path_steps: list[dict[str, Any]] = []
        final_rows: list[sqlite3.Row] = []

        for step_rank in range(target_rank + 1):
            step_level = ZOOM_LEVELS[step_rank]
            if frontier_parents is None:
                candidates = conn.execute("SELECT * FROM zoom_nodes WHERE level = ?", (step_level,)).fetchall()
            else:
                placeholders = ",".join("?" for _ in frontier_parents)
                candidates = conn.execute(
                    f"SELECT * FROM zoom_nodes WHERE level = ? AND parent_id IN ({placeholders})",
                    [step_level, *frontier_parents],
                ).fetchall()
            allowed = [r for r in candidates if _row_allowed_for_project(r, project)]
            # Always keep L0/L1/L2 branches that contain matching children even if lexical query only hits L4/L5
            ranked = sorted(
                allowed,
                key=lambda r: (-_score_node(r, q_tokens), str(r["node_id"])),
            )
            beam = ranked[: max(capped_limit, 4)] if step_rank < target_rank else ranked[:capped_limit]
            path_steps.append(
                {
                    "level": step_level,
                    "level_name": LEVEL_NAMES[step_level],
                    "selected_ids": [str(r["node_id"]) for r in beam],
                }
            )
            if step_rank == target_rank:
                final_rows = [r for r in beam if not q_tokens or _score_node(r, q_tokens) > 0.0]
                if not final_rows and not q_tokens:
                    final_rows = beam
                break
            frontier_parents = [str(r["node_id"]) for r in beam]
            if not frontier_parents:
                break

        return {
            "query": query,
            "mode": "progressive_zoom",
            "target_level": lvl,
            "project": project,
            "path": path_steps,
            "nodes": [
                {
                    "node_id": str(r["node_id"]),
                    "level": str(r["level"]),
                    "level_name": LEVEL_NAMES.get(str(r["level"]), ""),
                    "parent_id": str(r["parent_id"]),
                    "title": str(r["title"]),
                    "summary": str(r["summary"]),
                    "domain": str(r["domain"]),
                    "project_scope": str(r["project_scope"]),
                    "atom_filename": str(r["atom_filename"]),
                    "source_path": str(r["source_path"]),
                    "byte_start": int(r["byte_start"]),
                    "byte_end": int(r["byte_end"]),
                    "score": round(_score_node(r, q_tokens), 3),
                }
                for r in final_rows
            ],
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Fractal Zoom Retrieval (L0-L5)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="Rebuild/refresh L0-L5 hierarchy index")
    p_sync.add_argument("--force", action="store_true")
    p_sync.add_argument("--json", action="store_true")

    p_z = sub.add_parser("query", help="Run L0-L5 zoom retrieval")
    p_z.add_argument("query")
    p_z.add_argument("--level", default="L5", choices=ZOOM_LEVELS)
    p_z.add_argument("--parent-id", default=None)
    p_z.add_argument("--project", default=None)
    p_z.add_argument("--limit", type=int, default=8)
    p_z.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "sync":
        out = sync_cognitive_hierarchy(force=bool(args.force))
        print(json.dumps(out, indent=2) if args.json else f"status={out['status']} nodes={out['node_count']}")
        return 0
    out = zoom_retrieve(
        args.query,
        target_level=args.level,
        parent_id=args.parent_id,
        project=args.project,
        limit=args.limit,
    )
    print(json.dumps(out, indent=2) if args.json else f"level={out['target_level']} hits={len(out['nodes'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
