# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = []
# ///
"""Contradiction & Drift Radar + Temporal Knowledge Graph.

Constructs a deterministic, explainable graph across atoms (explicit links,
contested_by declarations, superseded_by lineage, shared-topic edges) and runs a
zero-model Contradiction & Drift Radar to surface:
  * Polarity conflicts (with scoped double-negation resolution)
  * Curated technical antonym conflicts (e.g. sync vs async, mutable vs immutable)
  * Unit-normalized parameter conflicts (normalizing durations & byte sizes)
  * Explicit contested_by conflicts
  * Source anchor / CAS approval drift & orphan approved atoms

Read-only advisory radar: never mutates or auto-approves vault memory.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atom_mutator import approved_revision_is_current, latest_decisions_by_atom  # noqa: E402
from audit_sources import _source_snapshot  # noqa: E402
from config import ATOMS_DIR, BRAIN_DIR, _is_reparse_or_link, is_content_markdown  # noqa: E402
from evidence import ANCHOR_FIELDS, EvidenceValidationError, find_evidence_anchor  # noqa: E402
from memory_format import split_frontmatter  # noqa: E402
from memory_review import parse_atom_record, parse_frontmatter_values, parse_scalar_list  # noqa: E402
from recall import normalize_project_tag, project_tags_match, wikilinks  # noqa: E402

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
        "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "was", "were",
        "will", "with", "should", "must", "can", "could", "would", "all", "any",
        "not", "no", "never", "do", "dont", "don't", "avoid", "forbid", "deprecated",
        "use", "used", "using", "only", "always", "every",
    }
)
_DOUBLE_NEGATION_RE = re.compile(
    r"\b(?:(?:never|must\s+not|do\s+not|don't|cannot|can't|refuse\s+to)\s+(?:\w+\s+){0,2}"
    r"(?:disable|disabled|bypass|skip|omit|ignore|drop|turn\s+off|allow\s+unencrypted)"
    r"|not\s+(?:invalid|insecure|incomplete|incompatible|inaccurate|unsafe|unauthorized|unauthenticated|unencrypted|unverified))\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?:never|must\s+not|do\s+not|don't|cannot|can't|avoid|forbid|forbidden|"
    r"disable|disabled|deprecated|no\s+longer|prohibited)\b",
    re.IGNORECASE,
)
_NEGATED_WORD_RE = re.compile(
    r"\b(?:not|never|must\s+not|do\s+not|don't|cannot|can't|avoid)\s+([a-z0-9_-]+)\b",
    re.IGNORECASE,
)
_NON_QUANTITY_LITERAL_RE = re.compile(
    r"(?ix)"
    r"\b\d{4}-\d{2}-\d{2}\b"
    r"|\bv?\d+\.\d+\.\d+(?:-[a-z0-9.]+)?\b"
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"
    r"|\b(?:sha|aes|rsa|ed|utf|http|rfc|iso|tls|ipv|erc|eip|x509|pkcs)[-\s]?\d+(?:\.\d+)?\b"
)
_CLAUSE_SPLIT_RE = re.compile(
    r"(?:;|\.\s+|,\s+(?:but|whereas|while)\s+|\s+however\b\s*,?\s*)",
    re.IGNORECASE,
)
_PORT_RE = re.compile(r"\bports?\s+(\d+)\b", re.IGNORECASE)
_QUANTITY_RE = re.compile(
    r"(?ix)\b(\d+(?:\.\d+)?)\s*(?:-|\s)?\s*"
    r"(milliseconds?|ms|seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d|weeks?|w|"
    r"gigabytes?|gib|gb|megabytes?|mib|mb|kilobytes?|kib|kb|bytes?|b|"
    r"bits?|bit|percent|%)\b"
    r"|\b(\d+(?:\.\d+)?)\b"
)
_TOKEN_RE = re.compile(r"[a-z0-9_]{3,}")

_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("enable", "disable"),
    ("enabled", "disabled"),
    ("allow", "deny"),
    ("allowed", "denied"),
    ("allowed", "forbidden"),
    ("sync", "async"),
    ("synchronous", "asynchronous"),
    ("encrypt", "plaintext"),
    ("encrypted", "unencrypted"),
    ("encrypted", "plaintext"),
    ("encryption", "plaintext"),
    ("mutable", "immutable"),
    ("stateful", "stateless"),
    ("online", "offline"),
    ("public", "private"),
    ("blocking", "nonblocking"),
    ("required", "optional"),
    ("strict", "permissive"),
)

_DURATION_FACTORS: dict[str, float] = {
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0,
    "w": 604800.0, "week": 604800.0, "weeks": 604800.0,
}
_BYTE_FACTORS: dict[str, float] = {
    "b": 1.0, "byte": 1.0, "bytes": 1.0,
    "kb": 1024.0, "kib": 1024.0, "kilobyte": 1024.0, "kilobytes": 1024.0,
    "mb": 1048576.0, "mib": 1048576.0, "megabyte": 1048576.0, "megabytes": 1048576.0,
    "gb": 1073741824.0, "gib": 1073741824.0, "gigabyte": 1073741824.0, "gigabytes": 1073741824.0,
}


def _topic_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS and not t.isdigit()}


def _is_negative_claim(text: str) -> bool:
    scrubbed = _DOUBLE_NEGATION_RE.sub(" affirm_keep ", text or "")
    return bool(_NEGATION_RE.search(scrubbed))


def _unnegated_predicates(text: str) -> set[str]:
    scrubbed = _DOUBLE_NEGATION_RE.sub(" ", text or "")
    scrubbed = _NEGATED_WORD_RE.sub(" ", scrubbed)
    return set(re.findall(r"[a-z0-9_]+", scrubbed.lower()))


def _split_clauses(text: str) -> list[str]:
    parts = [seg.strip() for seg in _CLAUSE_SPLIT_RE.split(text or "") if seg and seg.strip()]
    return parts if parts else ([text.strip()] if (text or "").strip() else [])


def _aligned_clause_pairs(text_a: str, text_b: str) -> list[tuple[str, str]]:
    clauses_a = [(c, _topic_tokens(c)) for c in _split_clauses(text_a)]
    clauses_b = [(c, _topic_tokens(c)) for c in _split_clauses(text_b)]
    if not clauses_a or not clauses_b:
        return []
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[int, int]] = set()
    for i, (ca, ta) in enumerate(clauses_a):
        best_j = -1
        best_key = (0.0, 0)
        for j, (_cb, tb) in enumerate(clauses_b):
            sh = len(ta & tb)
            if sh < 2:
                continue
            un = len(ta | tb)
            key = (sh / un if un else 0.0, sh)
            if key > best_key:
                best_key = key
                best_j = j
        if best_j >= 0:
            seen.add((i, best_j))
            pairs.append((ca, clauses_b[best_j][0]))
    for j, (cb, tb) in enumerate(clauses_b):
        best_i = -1
        best_key = (0.0, 0)
        for i, (_ca, ta) in enumerate(clauses_a):
            sh = len(ta & tb)
            if sh < 2:
                continue
            un = len(ta | tb)
            key = (sh / un if un else 0.0, sh)
            if key > best_key:
                best_key = key
                best_i = i
        if best_i >= 0 and (best_i, j) not in seen:
            seen.add((best_i, j))
            pairs.append((clauses_a[best_i][0], cb))
    return pairs


def _has_clause_polarity_conflict(text_a: str, text_b: str) -> bool:
    for ca, cb in _aligned_clause_pairs(text_a, text_b):
        if _is_negative_claim(ca) != _is_negative_claim(cb):
            return True
    return False


def _has_antonym_conflict(text_a: str, text_b: str) -> bool:
    pairs = _aligned_clause_pairs(text_a, text_b)
    if not pairs:
        pairs = [(text_a, text_b)]
    for ca, cb in pairs:
        words_a = _unnegated_predicates(ca)
        words_b = _unnegated_predicates(cb)
        for left, right in _ANTONYM_PAIRS:
            if (left in words_a and right not in words_a and right in words_b and left not in words_b) or (
                right in words_a and left not in words_a and left in words_b and right not in words_b
            ):
                return True
    return False


def _extract_quantities(text: str) -> dict[str, set[float]]:
    scrubbed = _NON_QUANTITY_LITERAL_RE.sub(" ", text or "")
    dims: dict[str, set[float]] = {}
    for p_match in _PORT_RE.finditer(scrubbed):
        dims.setdefault("port", set()).add(round(float(p_match.group(1)), 1))
    scrubbed_no_ports = _PORT_RE.sub(" ", scrubbed)
    for match in _QUANTITY_RE.finditer(scrubbed_no_ports):
        val_unit, raw_unit, val_scalar = match.group(1), match.group(2), match.group(3)
        if val_unit is not None and raw_unit is not None:
            num = float(val_unit)
            u = raw_unit.lower()
            if u in _DURATION_FACTORS:
                dims.setdefault("duration_s", set()).add(round(num * _DURATION_FACTORS[u], 6))
            elif u in _BYTE_FACTORS:
                dims.setdefault("bytes", set()).add(round(num * _BYTE_FACTORS[u], 3))
            elif u in {"bit", "bits"}:
                dims.setdefault("bits", set()).add(round(num, 3))
            elif u in {"%", "percent"}:
                dims.setdefault("percent", set()).add(round(num, 4))
        elif val_scalar is not None:
            dims.setdefault("scalar", set()).add(round(float(val_scalar), 6))
    return dims


def _extract_contextual_quantities(text: str) -> list[tuple[float, float, str, frozenset[str]]]:
    """Extract (raw_value, normalized_value, canonical_unit, local_context_tokens) per clause."""
    items: list[tuple[float, float, str, frozenset[str]]] = []
    unit_words = frozenset(set(_DURATION_FACTORS.keys()) | set(_BYTE_FACTORS.keys()) | {"bit", "bits", "percent", "port", "ports"})
    for clause in _split_clauses(text):
        scrubbed = _NON_QUANTITY_LITERAL_RE.sub(" ", clause)
        ctx = frozenset(_topic_tokens(scrubbed) - unit_words)
        for p_match in _PORT_RE.finditer(scrubbed):
            pv = float(p_match.group(1))
            items.append((pv, pv, "port", ctx))
        scrubbed_no_ports = _PORT_RE.sub(" ", scrubbed)
        for match in _QUANTITY_RE.finditer(scrubbed_no_ports):
            val_unit, raw_unit, val_scalar = match.group(1), match.group(2), match.group(3)
            if val_unit is not None and raw_unit is not None:
                raw_v = float(val_unit)
                u = raw_unit.lower()
                if u in _DURATION_FACTORS:
                    items.append((raw_v, round(raw_v * _DURATION_FACTORS[u], 6), "duration_s", ctx))
                elif u in _BYTE_FACTORS:
                    items.append((raw_v, round(raw_v * _BYTE_FACTORS[u], 3), "bytes", ctx))
                elif u in {"bit", "bits"}:
                    items.append((raw_v, round(raw_v, 3), "bits", ctx))
                elif u in {"%", "percent"}:
                    items.append((raw_v, round(raw_v, 4), "percent", ctx))
            elif val_scalar is not None:
                sv = round(float(val_scalar), 6)
                items.append((sv, sv, "scalar", ctx))
    return items


def _extract_numbers(text: str) -> set[str]:
    out: set[str] = set()
    for dim, vals in _extract_quantities(text).items():
        for v in vals:
            formatted = f"{int(v)}" if float(v).is_integer() else f"{v:g}"
            out.add(formatted if dim == "scalar" else f"{formatted}:{dim}")
    return out


def _has_numeric_conflict(
    q_a: list[tuple[float, float, str, frozenset[str]]],
    q_b: list[tuple[float, float, str, frozenset[str]]],
) -> bool:
    if not q_a or not q_b:
        return False
    for raw_a, norm_a, unit_a, ctx_a in q_a:
        for raw_b, norm_b, unit_b, ctx_b in q_b:
            shared_ctx = ctx_a & ctx_b
            if not shared_ctx:
                continue
            if unit_a == unit_b:
                if abs(norm_a - norm_b) > 1e-6 * max(1.0, abs(norm_a), abs(norm_b)):
                    equal_in_b = any(
                        ub2 == unit_a and (ctx_a & cb2) and abs(nb2 - norm_a) <= 1e-6 * max(1.0, abs(norm_a), abs(nb2))
                        for _rb2, nb2, ub2, cb2 in q_b
                    )
                    equal_in_a = any(
                        ua2 == unit_b and (ca2 & ctx_b) and abs(na2 - norm_b) <= 1e-6 * max(1.0, abs(norm_b), abs(na2))
                        for _ra2, na2, ua2, ca2 in q_a
                    )
                    if not (equal_in_b or equal_in_a):
                        return True
            else:
                if (
                    abs(raw_a - raw_b) <= 1e-9
                    and len(shared_ctx) >= 2
                    and "port" not in (unit_a, unit_b)
                ):
                    return True
    return False


def _matches_scope(contexts: list[str], project_scope: str | None) -> bool:
    if not project_scope or not project_scope.strip():
        return True
    target = normalize_project_tag(project_scope)
    if not target:
        return True
    proj_tags = [str(c).strip() for c in contexts if str(c).strip().lower().startswith("project:")]
    return bool(proj_tags) and all(bool(project_tags_match((c,), (target,))) for c in proj_tags)


def _parse_ref_list(raw_val: Any) -> list[str]:
    if isinstance(raw_val, list):
        return [str(x).strip() for x in raw_val if str(x).strip()]
    return parse_scalar_list(str(raw_val or ""))


def _inspect_drift_alert(
    path: str,
    raw: str,
    fm_vals: dict[str, Any],
    *,
    brain_root: Path,
    is_approved_current: bool,
) -> dict[str, Any] | None:
    review_status = str(fm_vals.get("review_status", "pending")).strip()
    claim_state = str(fm_vals.get("claim_state", "candidate")).strip()
    if review_status != "accepted" and claim_state != "human_reviewed":
        return None

    reasons: list[str] = []
    if not is_approved_current:
        reasons.append("missing_or_stale_cas_approval")
    evidence_source = str(fm_vals.get("evidence_source") or "").strip()
    anchor_error: str | None = None
    anchors = {key: fm_vals.get(key) for key in ANCHOR_FIELDS}
    if not all(value is not None and str(value).strip() for value in anchors.values()):
        reasons.append("missing_evidence_anchor")
    else:
        src_status, src_sha, src_err = _source_snapshot(brain_root, anchors)
        if src_status != "present":
            reasons.append(f"source_{src_status}")
            anchor_error = src_err
        elif src_sha != str(anchors.get("evidence_sha256") or ""):
            reasons.append("source_sha256_mismatch")
            anchor_error = "Anchored source file SHA-256 on disk no longer matches evidence_sha256."
        elif not is_approved_current:
            try:
                current_anchor = find_evidence_anchor(raw, str(brain_root))
                if not all(str(current_anchor.get(k)) == str(anchors.get(k)) for k in ANCHOR_FIELDS):
                    reasons.append("source_anchor_mismatch")
            except (EvidenceValidationError, OSError, ValueError) as exc:
                reasons.append("source_anchor_unverifiable")
                anchor_error = str(exc)

    if not reasons:
        return None

    return {
        "filename": os.path.basename(path),
        "review_status": review_status,
        "claim_state": claim_state,
        "drift_reasons": reasons,
        "evidence_source": evidence_source,
        "anchor_error": anchor_error,
        "recommended_action": "demote_or_reapprove",
    }


def build_knowledge_graph(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    project_scope: str | None = None,
    include_pending: bool = False,
    include_preferences: bool = True,
) -> dict[str, Any]:
    """Build deterministic nodes and edges across vault atoms."""
    nodes: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    latest_decisions: dict[str, dict[str, Any]] = {}
    dlog = os.path.join(brain_dir, "0-raw", "archive", "decision-record.jsonl")
    if os.path.isfile(dlog) and not _is_reparse_or_link(dlog):
        try:
            latest_decisions = latest_decisions_by_atom(dlog)
        except (OSError, ValueError, TypeError):
            latest_decisions = {}

    if os.path.isdir(atoms_dir) and not _is_reparse_or_link(atoms_dir):
        for entry in sorted(os.scandir(atoms_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            path = os.path.join(atoms_dir, entry.name)
            if _is_reparse_or_link(path) or not os.path.isfile(path):
                continue
            try:
                raw = Path(path).read_text(encoding="utf-8")
                rec = parse_atom_record(path)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            fm_parsed = split_frontmatter(raw)
            fm_vals = parse_frontmatter_values(fm_parsed[0]) if fm_parsed else {}
            status = rec.get("review_status")
            claim_state = str(fm_vals.get("claim_state") or rec.get("claim_state") or "").strip().lower()
            latest_action = str((latest_decisions.get(entry.name) or {}).get("action") or "").strip().lower()
            if (
                status in ("rejected", "quarantined")
                or claim_state in ("rejected", "quarantined")
                or latest_action in ("reject", "quarantine")
            ):
                continue
            sup_list = list(rec.get("superseded_by") or [])
            is_approved_current = bool(
                status == "accepted"
                and not sup_list
                and approved_revision_is_current(path, brain_dir=brain_dir, atoms_dir=atoms_dir)
            )
            if not include_pending and not is_approved_current:
                continue
            contexts = list(rec.get("contexts") or [])
            if not _matches_scope(contexts, project_scope):
                continue
            epistemic_label_raw = str(fm_vals.get("epistemic_label") or rec.get("epistemic_label") or "claim").strip() or "claim"
            is_pref = epistemic_label_raw == "preference" or bool(
                fm_vals.get("epistemic_contract") or rec.get("epistemic_contract")
            )
            if is_pref and not include_preferences:
                continue
            epistemic_label = "preference" if is_pref else epistemic_label_raw
            fm_links = _parse_ref_list(fm_vals.get("links", ""))
            links_dedup: list[str] = []
            seen_links: set[str] = set()
            for raw_target in [*fm_links, *wikilinks(raw)]:
                cleaned = str(raw_target or "").strip()
                if cleaned.startswith("[[") and cleaned.endswith("]]"):
                    cleaned = cleaned[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
                if cleaned and cleaned not in seen_links:
                    seen_links.add(cleaned)
                    links_dedup.append(cleaned)
            supersedes_list = _parse_ref_list(fm_vals.get("supersedes", ""))
            contested_by_list = _parse_ref_list(fm_vals.get("contested_by", ""))
            superseded_by = sup_list[0] if sup_list else ""
            claim_text = rec.get("claim") or ""
            atom_id = rec.get("id") or Path(entry.name).stem
            effective_status = (
                "unverified_accepted"
                if (status == "accepted" and not sup_list and not is_approved_current)
                else str(status or "")
            )
            node = {
                "atom_id": atom_id,
                "filename": rec.get("filename", entry.name),
                "review_status": rec.get("review_status", ""),
                "effective_status": effective_status,
                "approved_current": is_approved_current,
                "memory_type": rec.get("memory_type", ""),
                "epistemic_label": epistemic_label,
                "contexts": contexts,
                "links": links_dedup,
                "supersedes": supersedes_list,
                "contested_by": contested_by_list,
                "superseded_by": superseded_by,
                "claim": claim_text,
                "negative_polarity": _is_negative_claim(claim_text),
                "numbers": sorted(_extract_numbers(claim_text)),
                "topic_tokens": sorted(_topic_tokens(claim_text)),
            }
            nodes.append(node)
            by_id[atom_id] = node
            by_id[node["filename"]] = node

    def _resolve_target(target_ref: str) -> dict[str, Any] | None:
        clean = target_ref.strip()
        if clean.startswith("[[") and clean.endswith("]]"):
            clean = clean[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
        if clean in by_id:
            return by_id[clean]
        if not clean.lower().endswith(".md") and f"{clean}.md" in by_id:
            return by_id[f"{clean}.md"]
        if clean.lower().endswith(".md") and Path(clean).stem in by_id:
            return by_id[Path(clean).stem]
        return None

    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for node in nodes:
        for target in node["links"]:
            resolved = _resolve_target(target)
            if resolved and resolved["filename"] != node["filename"]:
                ek = (node["filename"], resolved["filename"], "link")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": node["filename"], "to": resolved["filename"], "kind": "link"})
        for contested_ref in node.get("contested_by") or ():
            resolved = _resolve_target(contested_ref)
            if resolved and resolved["filename"] != node["filename"]:
                ek = (node["filename"], resolved["filename"], "contested_by")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": node["filename"], "to": resolved["filename"], "kind": "contested_by"})
        if node["superseded_by"]:
            resolved = _resolve_target(node["superseded_by"])
            if resolved and resolved["filename"] != node["filename"]:
                ek = (node["filename"], resolved["filename"], "superseded_by")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": node["filename"], "to": resolved["filename"], "kind": "superseded_by"})
        for old_ref in node.get("supersedes") or ():
            resolved = _resolve_target(old_ref)
            if resolved and resolved["filename"] != node["filename"]:
                ek = (resolved["filename"], node["filename"], "superseded_by")
                if ek not in seen_edges:
                    seen_edges.add(ek)
                    edges.append({"from": resolved["filename"], "to": node["filename"], "kind": "superseded_by"})

    # Compute topic-similarity edges via inverted token index (evaluating only pairs sharing >= 2 topic tokens)
    inv_index: dict[str, list[int]] = defaultdict(list)
    node_tok_sets: list[set[str]] = []
    for idx, node in enumerate(nodes):
        tset = set(node["topic_tokens"])
        node_tok_sets.append(tset)
        for tok in tset:
            inv_index[tok].append(idx)

    for i, toks_a in enumerate(node_tok_sets):
        if len(toks_a) < 2:
            continue
        candidate_counts: Counter[int] = Counter()
        for tok in toks_a:
            for j in inv_index[tok]:
                if j > i:
                    candidate_counts[j] += 1
        for j in sorted(k for k, cnt in candidate_counts.items() if cnt >= 2):
            toks_b = node_tok_sets[j]
            shared = toks_a & toks_b
            union = toks_a | toks_b
            jaccard = len(shared) / len(union) if union else 0.0
            if jaccard >= 0.35 or len(shared) >= 3:
                edges.append(
                    {
                        "from": nodes[i]["filename"],
                        "to": nodes[j]["filename"],
                        "kind": "topic_overlap",
                        "jaccard": round(jaccard, 3),
                        "shared_terms": sorted(shared)[:8],
                    }
                )

    linked_files = {
        e["from"] for e in edges if e["kind"] in {"link", "contested_by"}
    } | {
        e["to"] for e in edges if e["kind"] in {"link", "contested_by"}
    }
    orphans = [n["filename"] for n in nodes if n.get("approved_current") and n["filename"] not in linked_files]
    return {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "orphan_approved_atoms": orphans,
        "nodes": nodes,
        "edges": edges,
    }


def detect_contradictions(
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    project_scope: str | None = None,
    include_pending: bool = True,
    include_preferences: bool = False,
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Detect polarity, antonym, unit-normalized numeric, contested_by, and source-drift issues."""
    if graph is None:
        graph = build_knowledge_graph(
            brain_dir=brain_dir,
            atoms_dir=atoms_dir,
            project_scope=project_scope,
            include_pending=include_pending,
            include_preferences=include_preferences,
        )
    active = [
        n
        for n in graph["nodes"]
        if not n["superseded_by"] and (include_preferences or n.get("epistemic_label", "claim") != "preference")
    ]
    active_idx_by_file = {n["filename"]: idx for idx, n in enumerate(active)}
    contested_pairs: set[tuple[str, str]] = set()
    explicit_pair_indices: dict[int, set[int]] = defaultdict(set)
    for edge in graph["edges"]:
        if edge["kind"] == "contested_by":
            pair = tuple(sorted((edge["from"], edge["to"])))
            contested_pairs.add((pair[0], pair[1]))
            ia = active_idx_by_file.get(edge["from"])
            ib = active_idx_by_file.get(edge["to"])
            if ia is not None and ib is not None and ia != ib:
                lo, hi = (ia, ib) if ia < ib else (ib, ia)
                explicit_pair_indices[lo].add(hi)

    inv_index: dict[str, list[int]] = defaultdict(list)
    active_tok_sets: list[set[str]] = []
    quantities_by_file: dict[str, list[tuple[float, float, str, frozenset[str]]]] = {}
    for idx, n in enumerate(active):
        tset = set(n["topic_tokens"])
        active_tok_sets.append(tset)
        quantities_by_file[n["filename"]] = _extract_contextual_quantities(n["claim"])
        for tok in tset:
            inv_index[tok].append(idx)

    alerts: list[dict[str, Any]] = []

    for i, a in enumerate(active):
        toks_a = active_tok_sets[i]
        candidate_counts: Counter[int] = Counter()
        if len(toks_a) >= 2:
            for tok in toks_a:
                for j in inv_index[tok]:
                    if j > i:
                        candidate_counts[j] += 1
        peer_indices = sorted({k for k, cnt in candidate_counts.items() if cnt >= 2} | explicit_pair_indices.get(i, set()))

        for j in peer_indices:
            b = active[j]
            toks_b = active_tok_sets[j]
            shared = sorted(toks_a & toks_b)
            union = toks_a | toks_b
            jaccard = len(shared) / len(union) if union else 0.0
            pair_key = tuple(sorted((a["filename"], b["filename"])))
            explicitly_contested = (pair_key[0], pair_key[1]) in contested_pairs

            topic_match = (len(toks_a) >= 2 and len(toks_b) >= 2) and (jaccard >= 0.30 or len(shared) >= 3)
            if not topic_match and not explicitly_contested:
                continue

            reasons: list[str] = []
            if explicitly_contested:
                reasons.append("contested_by_declared")
            if topic_match:
                if _has_clause_polarity_conflict(a["claim"], b["claim"]):
                    reasons.append("polarity_conflict")
                if _has_antonym_conflict(a["claim"], b["claim"]):
                    reasons.append("antonym_conflict")
                if _has_numeric_conflict(quantities_by_file[a["filename"]], quantities_by_file[b["filename"]]):
                    reasons.append("numeric_value_conflict")

            if not reasons:
                continue

            both_accepted = bool(a.get("approved_current") and b.get("approved_current"))
            alerts.append(
                {
                    "severity": "high" if both_accepted else "medium",
                    "reasons": reasons,
                    "jaccard": round(jaccard, 3),
                    "shared_terms": shared[:10],
                    "atom_a": {
                        "filename": a["filename"],
                        "review_status": a["review_status"],
                        "approved_current": bool(a.get("approved_current")),
                        "claim": a["claim"],
                    },
                    "atom_b": {
                        "filename": b["filename"],
                        "review_status": b["review_status"],
                        "approved_current": bool(b.get("approved_current")),
                        "claim": b["claim"],
                    },
                    "recommended_action": "owner_review_merge_or_demote",
                }
            )

    alerts.sort(key=lambda item: (0 if item["severity"] == "high" else 1, -item["jaccard"]))

    drift_alerts: list[dict[str, Any]] = []
    if os.path.isdir(atoms_dir) and not _is_reparse_or_link(atoms_dir):
        brain_root = Path(brain_dir).resolve()
        for node in active:
            apath = os.path.join(atoms_dir, node["filename"])
            if _is_reparse_or_link(apath) or not os.path.isfile(apath):
                continue
            try:
                raw_text = Path(apath).read_text(encoding="utf-8")
                fm_parsed = split_frontmatter(raw_text)
                fm_vals = parse_frontmatter_values(fm_parsed[0]) if fm_parsed else {}
                drift = _inspect_drift_alert(
                    apath,
                    raw_text,
                    fm_vals,
                    brain_root=brain_root,
                    is_approved_current=bool(node.get("approved_current")),
                )
                if drift is not None:
                    drift_alerts.append(drift)
            except (OSError, UnicodeDecodeError, ValueError):
                continue

    return {
        "contradiction_count": len(alerts),
        "high_severity_count": sum(1 for al in alerts if al["severity"] == "high"),
        "drift_count": len(drift_alerts),
        "orphan_approved_count": len(graph["orphan_approved_atoms"]),
        "alerts": alerts,
        "drift_alerts": drift_alerts,
        "orphan_approved_atoms": graph["orphan_approved_atoms"],
    }


def graph_neighbors(
    atom_filename: str,
    *,
    brain_dir: str = BRAIN_DIR,
    atoms_dir: str = ATOMS_DIR,
    include_pending: bool = False,
    include_preferences: bool = True,
) -> dict[str, Any]:
    """Return 1-hop neighbors and contradictions involving a specific atom."""
    clean_name = (atom_filename or "").strip()
    graph = build_knowledge_graph(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        include_pending=include_pending,
        include_preferences=include_preferences,
    )
    radar = detect_contradictions(
        brain_dir=brain_dir,
        atoms_dir=atoms_dir,
        include_pending=include_pending,
        include_preferences=include_preferences,
        graph=graph,
    )
    node = next(
        (
            n
            for n in graph["nodes"]
            if n["filename"] == clean_name
            or n["atom_id"] == clean_name
            or (not clean_name.endswith(".md") and n["filename"] == f"{clean_name}.md")
        ),
        None,
    )
    if node is None:
        raise FileNotFoundError(f"Atom not found in graph: {atom_filename}")
    fname = node["filename"]
    neighbors = [e for e in graph["edges"] if e["from"] == fname or e["to"] == fname]
    related_alerts = [
        al for al in radar["alerts"] if al["atom_a"]["filename"] == fname or al["atom_b"]["filename"] == fname
    ]
    return {
        "atom": fname,
        "edge_count": len(neighbors),
        "edges": neighbors,
        "contradictions": related_alerts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Contradiction & Drift Radar + Knowledge Graph")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("contradictions", help="Scan for claim polarity, antonym, numeric & drift issues")
    p_c.add_argument("--project", default=None)
    p_c.add_argument("--accepted-only", action="store_true")
    p_c.add_argument("--include-preferences", action="store_true")
    p_c.add_argument("--json", action="store_true")

    p_s = sub.add_parser("summary", help="Show knowledge graph node/edge/orphan summary")
    p_s.add_argument("--project", default=None)
    p_s.add_argument("--json", action="store_true")

    p_n = sub.add_parser("neighbors", help="Show 1-hop graph neighbors and contradictions for an atom")
    p_n.add_argument("filename")
    p_n.add_argument("--include-pending", action="store_true")
    p_n.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "contradictions":
        res = detect_contradictions(
            project_scope=args.project,
            include_pending=not args.accepted_only,
            include_preferences=bool(args.include_preferences),
        )
        print(
            json.dumps(res, indent=2)
            if args.json
            else f"contradictions={res['contradiction_count']} high={res['high_severity_count']} drift={res['drift_count']}"
        )
        return 0
    if args.cmd == "summary":
        res = build_knowledge_graph(project_scope=args.project, include_pending=False)
        out = {k: v for k, v in res.items() if k not in {"nodes", "edges"}}
        print(
            json.dumps(out, indent=2)
            if args.json
            else f"nodes={out['node_count']} edges={out['edge_count']} orphans={len(out['orphan_approved_atoms'])}"
        )
        return 0
    if args.cmd == "neighbors":
        res = graph_neighbors(args.filename, include_pending=bool(args.include_pending))
        print(
            json.dumps(res, indent=2)
            if args.json
            else f"atom={res['atom']} edges={res['edge_count']} contradictions={len(res['contradictions'])}"
        )
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
