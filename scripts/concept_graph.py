# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Living Concept Objects, Confidence Timelines & Spreading Activation (Epic 2D.4).

Materializes Living Concept Objects into ``brain/.sidecar/concepts.sqlite3``
(+ optional owner-curated ``brain/concepts/<slug>.md``) across currently approved
atoms, active playbooks, active mental models, and ``4-experiences/`` incidents:
- Linked verified facts & active playbooks
- Historical mistakes & open questions from episodic experience logs
- Deterministic Confidence Timeline derived from ``decision-record.jsonl`` &
  ``atom-corrections.jsonl``
- Typed weighted causal edges (``depends_on``, ``causes``, ``mitigates``,
  ``part_of``, ``contradicts``, ``supersedes``) with ``<= 3``-hop spreading activation.
"""



from __future__ import annotations

if __name__ == "__main__" and not __package__:
    import runpy as _sb_runpy
    import sys as _sb_sys
    from pathlib import Path as _sb_Path

    _sb_repo = _sb_Path(__file__).resolve().parents[1]
    _sb_here = _sb_Path(__file__).resolve().parent
    if _sb_Path(_sb_sys.path[0]).resolve() == _sb_here:
        _sb_sys.path[0] = str(_sb_repo)
    elif str(_sb_repo) not in _sb_sys.path:
        _sb_sys.path.insert(0, str(_sb_repo))
    raise SystemExit(_sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__"))


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

if __name__ in ("concept_graph", "scripts.concept_graph"):
    sys.modules.setdefault("concept_graph", sys.modules[__name__])
    sys.modules.setdefault("scripts.concept_graph", sys.modules[__name__])

from scripts.atom_mutator import latest_decisions_by_atom  # noqa: E402
from scripts.config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from scripts.memory_format import evaluate_temporal_validity, split_frontmatter  # noqa: E402
from scripts.memory_review import parse_atom_record_text, parse_frontmatter_values, parse_scalar_list, section_text  # noqa: E402
from scripts.recall import normalize_project_tag, project_tags_match  # noqa: E402
from scripts.review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())

TYPED_EDGE_RELATIONS = frozenset({"depends_on", "causes", "mitigates", "part_of", "contradicts", "supersedes"})
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]+)?\]\]")
_STOP_CONCEPTS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "when", "where",
        "coding", "general", "semantic", "procedural", "episodic", "owner", "atom",
        "claim", "evidence", "notes", "always", "never", "project", "must", "should",
    }
)


def concepts_db_path(brain_dir: str | Path | None = None) -> Path:
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    return Path(resolved_brain) / ".sidecar" / "concepts.sqlite3"


def _connect(dbp: Path) -> sqlite3.Connection:
    if os.path.lexists(str(dbp.parent)) and _is_reparse_or_link(str(dbp.parent)):
        raise PermissionError(f"Refusing symlinked sidecar dir: {dbp.parent}")
    dbp.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(str(dbp)) and _is_reparse_or_link(str(dbp)):
        raise PermissionError(f"Refusing symlinked concepts db: {dbp}")
    conn = sqlite3.connect(str(dbp), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS concepts (
            concept_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            projects_json TEXT NOT NULL,
            atoms_json TEXT NOT NULL,
            playbooks_json TEXT NOT NULL,
            mistakes_json TEXT NOT NULL,
            open_questions_json TEXT NOT NULL,
            timeline_json TEXT NOT NULL,
            current_confidence REAL NOT NULL DEFAULT 0.5,
            atom_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_edges (
            src_concept TEXT NOT NULL,
            relation TEXT NOT NULL,
            dst_concept TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 1.0,
            evidence_atoms_json TEXT NOT NULL,
            evidence_meta_json TEXT NOT NULL DEFAULT '[]',
            edge_age_days INTEGER NOT NULL DEFAULT 0,
            traversal_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (src_concept, relation, dst_concept)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    ecols = {str(r[1]) for r in conn.execute("PRAGMA table_info(concept_edges)").fetchall()}
    if "evidence_meta_json" not in ecols:
        conn.execute("ALTER TABLE concept_edges ADD COLUMN evidence_meta_json TEXT NOT NULL DEFAULT '[]'")
        conn.execute("DELETE FROM concept_meta WHERE key = 'signature'")
    return conn


def _concepts_signature(bpath: Path, apath: Path) -> str:
    """Compute a fast live freshness signature covering all inputs to concepts.sqlite3."""
    h = hashlib.sha256()
    h.update(b"schema:v2\n")
    h.update(f"today:{datetime.date.today().isoformat()}\n".encode("utf-8"))

    scan_dirs: tuple[tuple[str, Path], ...] = (
        ("2-atoms", apath),
        ("5-playbooks", bpath / "5-playbooks"),
        ("4-experiences", bpath / "4-experiences"),
        ("7-principles", bpath / "7-principles"),
        ("8-mental-models", bpath / "8-mental-models"),
        ("concepts", bpath / "concepts"),
    )
    for label, folder in scan_dirs:
        if folder.is_dir() and not _is_reparse_or_link(str(folder)):
            for entry in sorted(os.scandir(folder), key=lambda e: e.name):
                if not is_content_markdown(entry.name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    h.update(f"{label}/{entry.name}:err\n".encode("utf-8"))
                    continue
                h.update(f"{label}/{entry.name}:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
        else:
            h.update(f"{label}:absent\n".encode("utf-8"))

    archive_dir = bpath / "0-raw" / "archive"
    for fname in (
        "decision-record.jsonl",
        "atom-corrections.jsonl",
        "distillation-decisions.jsonl",
        "playbook-decisions.jsonl",
    ):
        fp = archive_dir / fname
        if os.path.lexists(str(fp)):
            if _is_reparse_or_link(str(fp)):
                h.update(f"archive/{fname}:symlink\n".encode("utf-8"))
            else:
                try:
                    st = os.lstat(str(fp))
                    h.update(f"archive/{fname}:{st.st_mtime_ns}:{st.st_size}\n".encode("utf-8"))
                except OSError:
                    h.update(f"archive/{fname}:err\n".encode("utf-8"))
        else:
            h.update(f"archive/{fname}:missing\n".encode("utf-8"))

    return h.hexdigest()


def _projects_strictly_within(item_projects: list[str] | tuple[str, ...], scope_tag: str) -> bool:
    norm_projs = [
        normalize_project_tag(str(p))
        for p in (item_projects or ())
        if str(p or "").strip().lower().startswith("project:")
    ]
    norm_projs = [p for p in norm_projs if p]
    if not norm_projs:
        return False
    return all(bool(project_tags_match((p,), (scope_tag,))) for p in norm_projs)


def _extract_project_tags_from_fm(fm_vals: dict[str, Any]) -> list[str]:
    projs: list[str] = []
    for key in ("project", "projects", "context", "contexts", "tags"):
        raw_v = fm_vals.get(key)
        if isinstance(raw_v, str):
            vals = parse_scalar_list(raw_v) if raw_v.strip().startswith("[") else [raw_v]
        elif isinstance(raw_v, (list, tuple)):
            vals = list(raw_v)
        else:
            vals = []
        for item in vals:
            s = str(item or "").strip()
            if not s:
                continue
            if key in {"project", "projects"} and not s.lower().startswith("project:"):
                s = f"project:{s}"
            if s.lower().startswith("project:"):
                norm = normalize_project_tag(s)
                if norm and norm not in projs:
                    projs.append(norm)
    return sorted(projs)


def _build_confidence_timeline(atoms_list: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    by_q: dict[str, dict[str, Any]] = {}
    for atom in atoms_list:
        q = _quarter_for_date(str(atom.get("date") or ""))
        slot = by_q.setdefault(q, {"confs": [], "corr": 0})
        try:
            slot["confs"].append(float(atom.get("confidence", 0.7)))
        except (TypeError, ValueError):
            slot["confs"].append(0.7)
        slot["corr"] += int(atom.get("corrections", 0) or 0)
    timeline: list[dict[str, Any]] = []
    cur_conf = 0.5
    for q in sorted(by_q):
        vals = by_q[q]["confs"]
        corr = by_q[q]["corr"]
        avg = sum(vals) / max(1, len(vals))
        adj = max(0.05, min(0.99, avg - 0.1 * corr))
        cur_conf = round(adj, 3)
        timeline.append(
            {
                "period": q,
                "confidence": cur_conf,
                "basis": f"{len(vals)} atoms, {corr} corrections",
            }
        )
    return timeline, float(cur_conf)


def _ensure_concepts_synced(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> tuple[str, str, Path]:
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)
    dbp = concepts_db_path(resolved_brain)
    sig = _concepts_signature(bpath, apath)
    needs_rebuild = True
    with closing(_connect(dbp)) as conn:
        row = conn.execute("SELECT value FROM concept_meta WHERE key = 'signature'").fetchone()
        if row is not None and row["value"] == sig:
            needs_rebuild = False
    if needs_rebuild:
        rebuild_concepts(brain_dir=resolved_brain, atoms_dir=resolved_atoms, force=True)
    return resolved_brain, resolved_atoms, dbp


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return (cleaned or "concept")[:64]


def _quarter_for_date(date_str: str) -> str:
    clean = str(date_str or "").strip()[:10]
    if len(clean) >= 7 and clean[4] == "-":
        try:
            year = clean[:4]
            month = int(clean[5:7])
            q = ((month - 1) // 3) + 1
            return f"{year}-Q{q}"
        except ValueError:
            pass
    return datetime.date.today().strftime("%Y") + f"-Q{((datetime.date.today().month - 1) // 3) + 1}"


def _extract_concept_names_for_atom(rec: dict[str, Any], fm_vals: dict[str, Any]) -> list[str]:
    names: list[str] = []
    tags_raw = fm_vals.get("tags", [])
    tags = parse_scalar_list(tags_raw) if isinstance(tags_raw, str) else list(tags_raw or [])
    for tag in tags:
        clean = str(tag or "").strip()
        if not clean or clean.lower().startswith("project:") or clean.lower() in _STOP_CONCEPTS:
            continue
        names.append(clean)
    for ctx in rec.get("contexts") or ():
        clean = str(ctx or "").strip()
        if not clean or clean.lower().startswith("project:") or clean.lower() in _STOP_CONCEPTS:
            continue
        names.append(clean)
    # Also pick explicit technical entity terms from title / claim (hyphenated or PascalCase / ALLCAPS)
    text_probe = f"{fm_vals.get('title', '')} {rec.get('claim', '')}"
    for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9_-]{2,32}\b", text_probe):
        low = tok.lower()
        if low in _STOP_CONCEPTS:
            continue
        if any(ch.isupper() for ch in tok) or "-" in tok or "_" in tok or any(ch.isdigit() for ch in tok):
            if tok not in names and low not in {n.lower() for n in names}:
                names.append(tok)
    if not names:
        fallback = str(fm_vals.get("title") or rec.get("id") or rec.get("filename", "")[:-3]).strip()
        if fallback:
            names.append(fallback)
    dedup: list[str] = []
    seen: set[str] = set()
    for nm in names:
        sl = _slugify(nm)
        if sl and sl not in seen:
            seen.add(sl)
            dedup.append(nm)
    return dedup[:8]


def _load_correction_counts(brain_dir: str) -> dict[str, list[dict[str, Any]]]:
    corr_p = Path(brain_dir) / "0-raw" / "archive" / "atom-corrections.jsonl"
    out: dict[str, list[dict[str, Any]]] = {}
    if not corr_p.is_file() or _is_reparse_or_link(str(corr_p)):
        return out
    for ln in corr_p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
            aid = str(obj.get("atom_id") or obj.get("filename") or "").strip()
            if aid:
                out.setdefault(aid, []).append(obj)
        except json.JSONDecodeError:
            continue
    return out


def rebuild_concepts(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Materialize all Living Concept Objects and typed edges into concepts.sqlite3."""
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    bpath = Path(resolved_brain)
    apath = Path(resolved_atoms)
    dbp = concepts_db_path(resolved_brain)
    sig = _concepts_signature(bpath, apath)

    if not force:
        with closing(_connect(dbp)) as conn:
            row = conn.execute("SELECT value FROM concept_meta WHERE key = 'signature'").fetchone()
            if row is not None and row["value"] == sig:
                c_cnt = int(conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0])
                e_cnt = int(conn.execute("SELECT COUNT(*) FROM concept_edges").fetchone()[0])
                return {
                    "status": "current",
                    "signature": sig,
                    "concept_count": c_cnt,
                    "edge_count": e_cnt,
                    "db_path": str(dbp),
                }

    dec_path = bpath / "0-raw" / "archive" / "decision-record.jsonl"
    try:
        latest_decisions = latest_decisions_by_atom(str(dec_path))
    except (FileNotFoundError, PermissionError, OSError):
        latest_decisions = {}
    corrections_by_atom = _load_correction_counts(resolved_brain)

    concept_buckets: dict[str, dict[str, Any]] = {}
    atom_to_concepts: dict[str, list[str]] = {}
    atom_records: dict[str, dict[str, Any]] = {}

    def _ensure_concept(cid: str, display: str) -> dict[str, Any]:
        if cid not in concept_buckets:
            concept_buckets[cid] = {
                "concept_id": cid,
                "name": display,
                "projects": set(),
                "atoms": [],
                "playbooks": [],
                "playbooks_seen": set(),
                "mistakes": [],
                "open_questions": [],
            }
        return concept_buckets[cid]

    if apath.is_dir() and not _is_reparse_or_link(str(apath)):
        for entry in sorted(os.scandir(apath), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            fpath = apath / entry.name
            if not fpath.is_file() or _is_reparse_or_link(str(fpath)):
                continue
            try:
                raw_text = fpath.read_text(encoding="utf-8")
                rec = parse_atom_record_text(entry.name, raw_text)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if rec.get("review_status") != "accepted" or not approved_revision_is_current(
                str(fpath), brain_dir=resolved_brain, atoms_dir=resolved_atoms
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

            links = _WIKILINK_RE.findall(raw_text)
            created_str = str(fm_vals.get("last-reviewed") or fm_vals.get("created") or "").strip()
            dec = latest_decisions.get(entry.name) or latest_decisions.get(str(fpath.resolve()))
            if not created_str and isinstance(dec, dict):
                created_str = str(dec.get("timestamp") or dec.get("reviewed_on") or "")[:10]
            try:
                conf_val = float(fm_vals.get("confidence") or 0.7)
            except (TypeError, ValueError):
                conf_val = 0.7

            projs = sorted(
                {
                    normalize_project_tag(str(c))
                    for c in (rec.get("contexts") or [])
                    if str(c).strip().lower().startswith("project:") and normalize_project_tag(str(c))
                }
            )
            corr_list = corrections_by_atom.get(entry.name, [])
            atom_info = {
                "filename": entry.name,
                "title": str(fm_vals.get("title") or rec.get("id") or entry.name[:-3]),
                "claim": str(rec.get("claim") or "").strip(),
                "confidence": conf_val,
                "date": created_str or datetime.date.today().isoformat(),
                "projects": projs,
                "corrections": len(corr_list),
                "links": links,
                "supersedes": list(rec.get("supersedes") or ()),
                "contested_by": list(rec.get("contested_by") or ()),
            }
            atom_records[entry.name] = atom_info
            if rec.get("superseded_by"):
                continue

            c_names = _extract_concept_names_for_atom(rec, fm_vals)
            cids: list[str] = []
            for cname in c_names:
                cid = _slugify(cname)
                cids.append(cid)
                bucket = _ensure_concept(cid, cname)
                bucket["projects"].update(projs)
                bucket["atoms"].append(
                    {
                        "filename": entry.name,
                        "title": atom_info["title"],
                        "claim": atom_info["claim"],
                        "confidence": conf_val,
                        "date": atom_info["date"],
                        "projects": projs,
                        "corrections": len(corr_list),
                    }
                )
            atom_to_concepts[entry.name] = cids

    # Fold active playbooks & 4-experiences failures into matching concepts
    try:
        from scripts.skill_playbooks import list_playbooks

        for pb in list_playbooks(brain_dir=resolved_brain, atoms_dir=resolved_atoms, active_only=True):
            backing_list = [str(b).strip() for b in (getattr(pb, "backing_atoms", ()) or ()) if str(b).strip()]
            pb_projs = sorted(
                {
                    p
                    for b_fn in backing_list
                    for p in (atom_records.get(b_fn, {}).get("projects") or ())
                }
            )
            for backing in backing_list:
                for cid in atom_to_concepts.get(backing, ()):
                    bucket = concept_buckets[cid]
                    if pb.filename not in bucket["playbooks_seen"]:
                        bucket["playbooks_seen"].add(pb.filename)
                        bucket["playbooks"].append(
                            {
                                "playbook_id": pb.filename,
                                "title": pb.title,
                                "projects": pb_projs,
                                "backing_atoms": backing_list,
                            }
                        )
    except Exception:
        pass

    exp_dir = bpath / "4-experiences"
    if exp_dir.is_dir() and not _is_reparse_or_link(str(exp_dir)):
        for entry in sorted(os.scandir(exp_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            ep = exp_dir / entry.name
            if not ep.is_file() or _is_reparse_or_link(str(ep)):
                continue
            try:
                etext = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            eparsed = split_frontmatter(etext)
            efm = parse_frontmatter_values(eparsed[0]) if eparsed else {}
            ebody = "\n".join(eparsed[1]) if eparsed else etext
            succ_raw = str(efm.get("success") or "").strip().lower()
            is_failure = succ_raw in {"false", "0", "no"}
            if not is_failure:
                continue
            goal = str(efm.get("goal") or entry.name).strip()
            exp_projs = _extract_project_tags_from_fm(efm)
            e_low = etext.lower()
            for cid, bucket in concept_buckets.items():
                if bucket["name"].lower() in e_low or cid in e_low:
                    bucket["mistakes"].append(
                        {
                            "experience": entry.name,
                            "goal": goal,
                            "lesson": section_text(ebody, "Lessons") or section_text(ebody, "Reflection") or goal,
                            "projects": exp_projs,
                            "provenance": "unverified_experience",
                            "verified": False,
                        }
                    )

    # Also integrate optional owner-curated brain/concepts/<slug>.md open questions (unverified scratch metadata)
    c_notes_dir = bpath / "concepts"
    if c_notes_dir.is_dir() and not _is_reparse_or_link(str(c_notes_dir)):
        for entry in sorted(os.scandir(c_notes_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            cp = c_notes_dir / entry.name
            if not cp.is_file() or _is_reparse_or_link(str(cp)):
                continue
            try:
                txt = cp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            cparsed = split_frontmatter(txt)
            cfm = parse_frontmatter_values(cparsed[0]) if cparsed else {}
            cbody = "\n".join(cparsed[1]) if cparsed else txt
            note_projs = _extract_project_tags_from_fm(cfm)
            cid = _slugify(entry.name[:-3])
            qs = [ln.lstrip("-* ").strip() for ln in section_text(cbody, "Open Questions").splitlines() if ln.strip()]
            if qs:
                bucket = _ensure_concept(cid, entry.name[:-3])
                for q_text in qs:
                    bucket["open_questions"].append(
                        {
                            "question": q_text,
                            "source": f"concepts/{entry.name}",
                            "projects": note_projs,
                            "provenance": "unverified_concept_note",
                            "verified": False,
                        }
                    )

    # Build typed causal edges from atom links, supersedes, contested_by, and active mental models
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    today_date = datetime.date.today()

    def _add_edge(
        src_c: str,
        rel: str,
        dst_c: str,
        weight: float,
        atom_fn: str,
        date_str: str,
        ev_projects: list[str] | tuple[str, ...] = (),
    ) -> None:
        if not src_c or not dst_c or src_c == dst_c or rel not in TYPED_EDGE_RELATIONS:
            return
        try:
            age = max(0, (today_date - datetime.date.fromisoformat(date_str[:10])).days)
        except ValueError:
            age = 0
        norm_ev_projs = sorted(
            {
                normalize_project_tag(str(p))
                for p in (ev_projects or ())
                if str(p).strip().lower().startswith("project:") and normalize_project_tag(str(p))
            }
        )
        key = (src_c, rel, dst_c)
        ev_entry = {"atom": atom_fn, "projects": norm_ev_projs} if (atom_fn or norm_ev_projs) else None
        if key not in edges:
            edges[key] = {
                "src_concept": src_c,
                "relation": rel,
                "dst_concept": dst_c,
                "weight": float(weight),
                "evidence_atoms": [atom_fn] if atom_fn else [],
                "evidence_meta": [ev_entry] if ev_entry is not None else [],
                "edge_age_days": age,
            }
        else:
            edges[key]["weight"] = round(min(2.0, edges[key]["weight"] + 0.15), 4)
            if atom_fn and atom_fn not in edges[key]["evidence_atoms"]:
                edges[key]["evidence_atoms"].append(atom_fn)
            if ev_entry is not None and ev_entry not in edges[key]["evidence_meta"]:
                edges[key]["evidence_meta"].append(ev_entry)
            edges[key]["edge_age_days"] = min(edges[key]["edge_age_days"], age)

    for atom_fn, info in atom_records.items():
        src_cids = atom_to_concepts.get(atom_fn, [])
        src_projs = info["projects"]
        # Co-occurring concepts in the same approved atom form part_of links
        for i, sc in enumerate(src_cids):
            for dc in src_cids[i + 1 :]:
                _add_edge(sc, "part_of", dc, 0.7, atom_fn, info["date"], src_projs)
                _add_edge(dc, "part_of", sc, 0.7, atom_fn, info["date"], src_projs)
        for lnk in info["links"]:
            target_fn = lnk if lnk.endswith(".md") else f"{lnk}.md"
            tgt_projs = atom_records.get(target_fn, {}).get("projects") or ()
            combined_projs = sorted(set(src_projs) | set(tgt_projs))
            for sc in src_cids:
                for dc in atom_to_concepts.get(target_fn, ()):
                    _add_edge(sc, "depends_on", dc, 0.85, atom_fn, info["date"], combined_projs)
        for sup in info["supersedes"]:
            sup_fn = sup if str(sup).endswith(".md") else f"{sup}.md"
            tgt_projs = atom_records.get(sup_fn, {}).get("projects") or ()
            combined_projs = sorted(set(src_projs) | set(tgt_projs))
            for sc in src_cids:
                for dc in atom_to_concepts.get(sup_fn, ()):
                    _add_edge(sc, "supersedes", dc, 0.9, atom_fn, info["date"], combined_projs)
        for con in info["contested_by"]:
            con_fn = con if str(con).endswith(".md") else f"{con}.md"
            tgt_projs = atom_records.get(con_fn, {}).get("projects") or ()
            combined_projs = sorted(set(src_projs) | set(tgt_projs))
            for sc in src_cids:
                for dc in atom_to_concepts.get(con_fn, ()):
                    _add_edge(sc, "contradicts", dc, 0.95, atom_fn, info["date"], combined_projs)

    # Pull active causal mental models into concept edges
    try:
        from scripts.cognitive_distiller import list_distilled_artifacts

        for mm in list_distilled_artifacts(kind="mental_model", active_only=True, brain_dir=resolved_brain, atoms_dir=resolved_atoms):
            mm_projs = [
                normalize_project_tag(str(p))
                for p in (mm.get("projects") or ())
                if str(p).strip().lower().startswith("project:") and normalize_project_tag(str(p))
            ]
            for ed in mm.get("edges") or ():
                sc = _slugify(str(ed.get("from") or ""))
                dc = _slugify(str(ed.get("to") or ""))
                rel = str(ed.get("relation") or "causes").lower()
                if rel == "enables":
                    rel = "causes"
                if rel in TYPED_EDGE_RELATIONS:
                    ev_atom = str(ed.get("evidence_atom") or "").strip()
                    ev_projs = atom_records.get(ev_atom, {}).get("projects") or mm_projs
                    _ensure_concept(sc, str(ed.get("from") or sc))
                    _ensure_concept(dc, str(ed.get("to") or dc))
                    _add_edge(sc, rel, dc, 1.0, ev_atom, today_date.isoformat(), ev_projs)
    except Exception:
        pass

    now_iso = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
    with closing(_connect(dbp)) as conn, conn:
        prior_traversals = {
            (str(r["src_concept"]), str(r["relation"]), str(r["dst_concept"])): int(r["traversal_count"] or 0)
            for r in conn.execute(
                "SELECT src_concept, relation, dst_concept, traversal_count FROM concept_edges"
            ).fetchall()
        }
        conn.execute("DELETE FROM concepts")
        conn.execute("DELETE FROM concept_edges")
        for cid, b in sorted(concept_buckets.items()):
            timeline, cur_conf = _build_confidence_timeline(b["atoms"])
            conn.execute(
                """
                INSERT INTO concepts (
                    concept_id, name, projects_json, atoms_json, playbooks_json,
                    mistakes_json, open_questions_json, timeline_json,
                    current_confidence, atom_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    b["name"],
                    json.dumps(sorted(b["projects"])),
                    json.dumps(b["atoms"], ensure_ascii=False),
                    json.dumps(b["playbooks"], ensure_ascii=False),
                    json.dumps(b["mistakes"], ensure_ascii=False),
                    json.dumps(b["open_questions"], ensure_ascii=False),
                    json.dumps(timeline, ensure_ascii=False),
                    float(cur_conf),
                    len(b["atoms"]),
                    now_iso,
                ),
            )
        for edge in edges.values():
            prev_tc = prior_traversals.get(
                (edge["src_concept"], edge["relation"], edge["dst_concept"]),
                0,
            )
            conn.execute(
                """
                INSERT INTO concept_edges (
                    src_concept, relation, dst_concept, weight,
                    evidence_atoms_json, evidence_meta_json, edge_age_days, traversal_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge["src_concept"],
                    edge["relation"],
                    edge["dst_concept"],
                    edge["weight"],
                    json.dumps(edge["evidence_atoms"], ensure_ascii=False),
                    json.dumps(edge["evidence_meta"], ensure_ascii=False),
                    int(edge["edge_age_days"]),
                    int(prev_tc),
                ),
            )
        conn.execute(
            "INSERT OR REPLACE INTO concept_meta(key, value) VALUES ('signature', ?)",
            (sig,),
        )

    return {
        "status": "rebuilt",
        "signature": sig,
        "concept_count": len(concept_buckets),
        "edge_count": len(edges),
        "db_path": str(dbp),
    }


def _row_to_concept_dict(row: sqlite3.Row, project: str | None) -> dict[str, Any] | None:
    projs = list(json.loads(row["projects_json"]))
    atoms = list(json.loads(row["atoms_json"]))
    playbooks = list(json.loads(row["playbooks_json"]))
    mistakes = list(json.loads(row["mistakes_json"]))
    open_questions = list(json.loads(row["open_questions_json"]))
    timeline = list(json.loads(row["timeline_json"]))
    cur_conf = float(row["current_confidence"])

    if project:
        scope_tag = normalize_project_tag(project)
        if not scope_tag:
            return None
        scoped_atoms = [a for a in atoms if _projects_strictly_within(a.get("projects") or (), scope_tag)]
        if not scoped_atoms:
            return None
        atoms = scoped_atoms
        projs = sorted(
            {
                normalize_project_tag(str(p))
                for a in scoped_atoms
                for p in (a.get("projects") or ())
                if str(p).strip().lower().startswith("project:") and normalize_project_tag(str(p))
            }
        )
        scoped_playbooks = []
        for pb in playbooks:
            if isinstance(pb, dict) and _projects_strictly_within(pb.get("projects") or (), scope_tag):
                scoped_playbooks.append(pb)
        playbooks = scoped_playbooks

        scoped_mistakes = []
        for mk in mistakes:
            if isinstance(mk, dict) and _projects_strictly_within(mk.get("projects") or (), scope_tag):
                scoped_mistakes.append(mk)
        mistakes = scoped_mistakes

        scoped_questions = []
        for q_item in open_questions:
            if isinstance(q_item, dict) and _projects_strictly_within(q_item.get("projects") or (), scope_tag):
                scoped_questions.append(q_item)
        open_questions = scoped_questions

        timeline, cur_conf = _build_confidence_timeline(scoped_atoms)

    return {
        "concept_id": str(row["concept_id"]),
        "name": str(row["name"]),
        "projects": projs,
        "atom_count": len(atoms),
        "atoms": atoms,
        "playbooks": playbooks,
        "mistakes": mistakes,
        "open_questions": open_questions,
        "confidence_timeline": timeline,
        "current_confidence": float(cur_conf),
        "updated_at": str(row["updated_at"]),
    }


def _filter_edge_row(
    edge_row: sqlite3.Row,
    project: str | None,
    allowed_concept_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    src_c = str(edge_row["src_concept"])
    dst_c = str(edge_row["dst_concept"])
    if allowed_concept_ids is not None and (src_c not in allowed_concept_ids or dst_c not in allowed_concept_ids):
        return None
    ev_atoms = list(json.loads(edge_row["evidence_atoms_json"]))
    if project:
        scope_tag = normalize_project_tag(project)
        if not scope_tag:
            return None
        ev_meta_raw = edge_row["evidence_meta_json"] if "evidence_meta_json" in edge_row.keys() else "[]"
        try:
            ev_meta = list(json.loads(ev_meta_raw or "[]"))
        except Exception:
            ev_meta = []
        permitted_meta = [
            item
            for item in ev_meta
            if isinstance(item, dict) and _projects_strictly_within(item.get("projects") or (), scope_tag)
        ]
        if not permitted_meta:
            return None
        ev_atoms = []
        for item in permitted_meta:
            atom_name = str(item.get("atom") or "").strip()
            if atom_name and atom_name not in ev_atoms:
                ev_atoms.append(atom_name)
    return {
        "src_concept": src_c,
        "relation": str(edge_row["relation"]),
        "dst_concept": dst_c,
        "weight": float(edge_row["weight"]),
        "evidence_atoms": ev_atoms,
        "edge_age_days": int(edge_row["edge_age_days"]),
        "traversal_count": int(edge_row["traversal_count"]),
    }


def load_concept_graph_snapshot(
    *,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    project: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Run at most one freshness sync and return scoped concepts + edges in a single SQLite read."""
    _, _, dbp = _ensure_concepts_synced(brain_dir=brain_dir, atoms_dir=atoms_dir)
    with closing(_connect(dbp)) as conn:
        c_rows = conn.execute("SELECT * FROM concepts ORDER BY atom_count DESC, concept_id ASC").fetchall()
        e_rows = conn.execute("SELECT * FROM concept_edges ORDER BY weight DESC").fetchall()

    all_allowed_ids: set[str] = set()
    concepts: list[dict[str, Any]] = []
    for r in c_rows:
        item = _row_to_concept_dict(r, project)
        if item is not None:
            all_allowed_ids.add(item["concept_id"])
            if len(concepts) < limit:
                concepts.append(item)

    allowed_filter = all_allowed_ids if project else None
    edges: list[dict[str, Any]] = []
    for er in e_rows:
        ed = _filter_edge_row(er, project, allowed_concept_ids=allowed_filter)
        if ed is not None:
            edges.append(ed)

    return {
        "concepts": concepts,
        "edges": edges,
        "concept_count": len(concepts),
        "edge_count": len(edges),
        "project": project,
    }


def list_concepts(
    *,
    project: str | None = None,
    limit: int = 50,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    _, _, dbp = _ensure_concepts_synced(brain_dir=brain_dir, atoms_dir=atoms_dir)
    with closing(_connect(dbp)) as conn:
        rows = conn.execute("SELECT * FROM concepts ORDER BY atom_count DESC, concept_id ASC").fetchall()
    out = []
    for r in rows:
        item = _row_to_concept_dict(r, project)
        if item is not None:
            out.append(item)
            if len(out) >= limit:
                break
    return out


def get_concept(
    concept_id_or_name: str,
    *,
    project: str | None = None,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Retrieve a Living Concept Object with confidence timeline and typed outgoing/incoming edges."""
    _, _, dbp = _ensure_concepts_synced(brain_dir=brain_dir, atoms_dir=atoms_dir)
    slug = _slugify(concept_id_or_name)
    with closing(_connect(dbp)) as conn:
        row = conn.execute(
            "SELECT * FROM concepts WHERE concept_id = ? OR LOWER(name) = ?",
            (slug, str(concept_id_or_name or "").strip().lower()),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Concept not found: {concept_id_or_name}")
        concept = _row_to_concept_dict(row, project)
        if concept is None:
            raise PermissionError(f"Concept '{concept_id_or_name}' has no atoms permitted in project {project!r}.")
        cid = concept["concept_id"]
        allowed_ids: set[str] | None = None
        if project:
            all_rows = conn.execute("SELECT * FROM concepts").fetchall()
            allowed_ids = {
                obj["concept_id"]
                for r in all_rows
                for obj in (_row_to_concept_dict(r, project),)
                if obj is not None
            }
        edges = conn.execute(
            "SELECT * FROM concept_edges WHERE src_concept = ? OR dst_concept = ? ORDER BY weight DESC",
            (cid, cid),
        ).fetchall()
        filtered_edges = []
        for e in edges:
            ed = _filter_edge_row(e, project, allowed_concept_ids=allowed_ids)
            if ed is not None:
                filtered_edges.append(ed)
        concept["edges"] = filtered_edges
    return concept


def spreading_activation(
    seed_concepts: list[str] | tuple[str, ...],
    *,
    max_hops: int = 3,
    decay: float = 0.55,
    limit: int = 12,
    project: str | None = None,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run <=3-hop damped spreading activation over typed concept edges."""
    hops = max(1, min(3, int(max_hops)))
    d_factor = max(0.1, min(0.95, float(decay)))
    _, _, dbp = _ensure_concepts_synced(brain_dir=brain_dir, atoms_dir=atoms_dir)

    scores: dict[str, float] = {}
    hop_distances: dict[str, int] = {}
    visited_edges: list[dict[str, Any]] = []
    traversed_keys: list[tuple[str, str, str]] = []
    frontier: dict[str, float] = {}

    with closing(_connect(dbp)) as conn:
        all_rows = conn.execute("SELECT * FROM concepts").fetchall()
        allowed_concepts: dict[str, dict[str, Any]] = {}
        for r in all_rows:
            obj = _row_to_concept_dict(r, project)
            if obj is not None:
                allowed_concepts[obj["concept_id"]] = obj
        allowed_ids = set(allowed_concepts.keys())

        for seed in seed_concepts or ():
            sc = _slugify(str(seed))
            if sc in allowed_concepts:
                frontier[sc] = 1.0
                scores[sc] = 1.0
                hop_distances[sc] = 0

        for hop in range(1, hops + 1):
            next_frontier: dict[str, float] = {}
            for src_c, act in frontier.items():
                erows = conn.execute(
                    "SELECT * FROM concept_edges WHERE src_concept = ?",
                    (src_c,),
                ).fetchall()
                for er in erows:
                    filtered_ed = _filter_edge_row(er, project, allowed_concept_ids=allowed_ids)
                    if filtered_ed is None:
                        continue
                    dst_c = filtered_ed["dst_concept"]
                    rel = filtered_ed["relation"]
                    propagated = act * float(filtered_ed["weight"]) * d_factor
                    if propagated > scores.get(dst_c, 0.0):
                        scores[dst_c] = round(propagated, 4)
                        hop_distances.setdefault(dst_c, hop)
                        next_frontier[dst_c] = max(next_frontier.get(dst_c, 0.0), propagated)
                    visited_edges.append(
                        {
                            "hop": hop,
                            "from": src_c,
                            "relation": rel,
                            "to": dst_c,
                            "activation": round(propagated, 4),
                        }
                    )
                    traversed_keys.append((src_c, rel, dst_c))
            frontier = next_frontier
            if not frontier:
                break

        if traversed_keys:
            with conn:
                for src_c, rel, dst_c in traversed_keys:
                    conn.execute(
                        """
                        UPDATE concept_edges
                        SET traversal_count = traversal_count + 1
                        WHERE src_concept = ? AND relation = ? AND dst_concept = ?
                        """,
                        (src_c, rel, dst_c),
                    )

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[: max(1, min(30, int(limit)))]
    activated_atoms: list[dict[str, Any]] = []
    seen_atoms: set[str] = set()
    for cid, act in ranked:
        for atom in allowed_concepts[cid]["atoms"]:
            if atom["filename"] not in seen_atoms:
                seen_atoms.add(atom["filename"])
                activated_atoms.append({**atom, "via_concept": cid, "activation": act})

    return {
        "seeds": [_slugify(s) for s in (seed_concepts or ())],
        "max_hops": hops,
        "activated_concepts": [
            {"concept_id": cid, "activation": act, "hop": hop_distances.get(cid, 0)}
            for cid, act in ranked
        ],
        "activated_atoms": activated_atoms[: max(1, min(30, int(limit)))],
        "traversed_edges": visited_edges,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Living Concept Graph & Confidence Timelines")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("rebuild", help="Rebuild Living Concept Graph").add_argument("--json", action="store_true")
    p_g = sub.add_parser("get", help="Inspect a Living Concept Object")
    p_g.add_argument("concept")
    p_g.add_argument("--project", default=None)
    p_g.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "rebuild":
        res = rebuild_concepts(force=True)
        print(json.dumps(res, indent=2) if args.json else f"concepts={res['concept_count']} edges={res['edge_count']}")
        return 0
    res = get_concept(args.concept, project=args.project)
    print(json.dumps(res, indent=2) if args.json else f"{res['concept_id']} conf={res['current_confidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
