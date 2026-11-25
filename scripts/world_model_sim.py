# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""World-Model Impact Simulator (Epic 2D.6).

Combines Living Concept causal edges, local repository AST/import dependency
graphs, and historical ``4-experiences/`` incident records to simulate:
"If I change X, what breaks downstream?"

Every simulated hop is explicitly labeled with its evidence class:
- ``verified_vault_edge``
- ``code_ast_dependency``
- ``inferred_risk``
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
import ast
from collections import deque
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

if __name__ in ("world_model_sim", "scripts.world_model_sim"):
    sys.modules.setdefault("world_model_sim", sys.modules[__name__])
    sys.modules.setdefault("scripts.world_model_sim", sys.modules[__name__])

from scripts.concept_graph import load_concept_graph_snapshot  # noqa: E402
from scripts.config import (  # noqa: E402
    ATOMS_DIR,
    BRAIN_DIR,
    _is_immutable_darwin_root_symlink,
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from scripts.memory_format import split_frontmatter  # noqa: E402
from scripts.memory_review import parse_frontmatter_values, parse_scalar_list  # noqa: E402
from scripts.recall import normalize_project_tag, project_tags_match  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
_IGNORED_REPO_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".sidecar", ".chroma"})


def _validate_repo_dir(
    repo_dir: str | Path,
    *,
    allow_repo_scan: bool,
    allowed_repo_roots: tuple[str, ...] | None,
) -> Path:
    """Validate raw ``repo_dir`` against scan policy, traversal, symlinks, and allowed root confinement."""
    if not allow_repo_scan:
        raise PermissionError("Repository AST scanning is disabled (allow_repo_scan=False).")
    raw_str = str(repo_dir or "").strip()
    if not raw_str or "\x00" in raw_str:
        raise ValueError("Invalid repo_dir.")
    raw_input_path = Path(raw_str)
    if ".." in raw_input_path.parts:
        raise PermissionError(f"Refusing path traversal in repo_dir: {repo_dir}")

    raw_abs = Path(os.path.abspath(os.path.expanduser(raw_str)))
    # Check raw target and every ancestor for symlink/reparse BEFORE calling .resolve()
    cur = raw_abs
    while True:
        if os.path.lexists(str(cur)) and _is_reparse_or_link(str(cur)) and not _is_immutable_darwin_root_symlink(cur):
            raise PermissionError(f"Refusing symlinked or reparse repo_dir component: {cur}")
        parent = cur.parent
        if parent == cur:
            break
        cur = parent

    resolved_repo = raw_abs.resolve()
    if _is_reparse_or_link(str(resolved_repo)):
        raise PermissionError(f"Refusing symlinked repo_dir: {resolved_repo}")

    if allowed_repo_roots is not None:
        resolved_roots: list[Path] = []
        for root_item in allowed_repo_roots:
            r_str = str(root_item or "").strip()
            if not r_str or "\x00" in r_str:
                continue
            resolved_roots.append(Path(os.path.abspath(os.path.expanduser(r_str))).resolve())
        if not resolved_roots or not any(resolved_repo.is_relative_to(r_real) for r_real in resolved_roots):
            raise PermissionError(f"Refusing repo_dir outside allowed_repo_roots: {resolved_repo}")

    return resolved_repo


def _extract_experience_projects(efm: dict[str, Any]) -> list[str]:
    projs: list[str] = []
    for key in ("project", "projects", "context", "contexts", "tags"):
        raw_v = efm.get(key)
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


def _scan_python_repo_dependencies(repo_dir: Path, max_files: int = 250) -> list[dict[str, str]]:
    """Return reverse-dependency edges: imported_module -> dependent_file (code_ast_dependency)."""
    if not repo_dir.is_dir() or _is_reparse_or_link(str(repo_dir)):
        return []
    edges: list[dict[str, str]] = []
    scanned = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_REPO_DIRS and not _is_reparse_or_link(os.path.join(root, d)))
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            fpath = Path(root) / fn
            if _is_reparse_or_link(str(fpath)) or not fpath.is_file():
                continue
            scanned += 1
            if scanned > max_files:
                return edges
            try:
                src = fpath.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(src, filename=str(fpath))
            except (OSError, SyntaxError):
                continue
            rel = fpath.relative_to(repo_dir).as_posix()
            mod_name = fpath.stem
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[-1]
                        if top:
                            edges.append({"src": top, "dst": f"{rel} ({mod_name})", "relation": "imported_by"})
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[-1]
                    if top:
                        edges.append({"src": top, "dst": f"{rel} ({mod_name})", "relation": "imported_by"})
    return edges


def _normalize_node_key(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip().lower()).strip("-")


def simulate_change_impact(
    target: str,
    *,
    max_hops: int = 3,
    project: str | None = None,
    repo_dir: str | Path | None = None,
    allow_repo_scan: bool = True,
    allowed_repo_roots: tuple[str, ...] | None = None,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Simulate downstream blast radius from changing ``target`` across vault, AST, and incidents."""
    clean_target = (target or "").strip()
    if not clean_target:
        raise ValueError("Simulation target must be non-empty.")

    validated_repo: Path | None = None
    if repo_dir is not None:
        validated_repo = _validate_repo_dir(
            repo_dir,
            allow_repo_scan=allow_repo_scan,
            allowed_repo_roots=allowed_repo_roots,
        )

    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    hops_cap = max(1, min(4, int(max_hops)))
    bpath = Path(resolved_brain)

    snapshot = load_concept_graph_snapshot(
        project=project,
        limit=200,
        brain_dir=resolved_brain,
        atoms_dir=resolved_atoms,
    )

    # Adjacency: node_key -> list[(dst_key, edge_info)]
    adj: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    display_names: dict[str, str] = {}

    def _link(src_name: str, dst_name: str, relation: str, provenance: str, evidence: Any) -> None:
        sk = _normalize_node_key(src_name)
        dk = _normalize_node_key(dst_name)
        if not sk or not dk or sk == dk:
            return
        display_names.setdefault(sk, src_name)
        display_names.setdefault(dk, dst_name)
        adj.setdefault(sk, []).append(
            (
                dk,
                {
                    "from": src_name,
                    "to": dst_name,
                    "relation": relation,
                    "provenance": provenance,
                    "evidence": evidence,
                },
            )
        )

    for c in snapshot.get("concepts") or ():
        display_names[str(c["concept_id"])] = str(c["name"])

    for ed in snapshot.get("edges") or ():
        src_c = str(ed["src_concept"])
        dst_c = str(ed["dst_concept"])
        rel = str(ed["relation"])
        ev = list(ed.get("evidence_atoms") or ())
        # Downstream dependents point *to* a dependency ("B depends_on X" means changing X impacts B)
        if rel in {"depends_on", "part_of"}:
            _link(dst_c, src_c, f"depended_on_by:{rel}", "verified_vault_edge", ev)
        else:
            _link(src_c, dst_c, rel, "verified_vault_edge", ev)

    if validated_repo is not None:
        for ast_ed in _scan_python_repo_dependencies(validated_repo):
            _link(ast_ed["src"], ast_ed["dst"], ast_ed["relation"], "code_ast_dependency", ast_ed["dst"])

    # Pull 4-experiences failures touching target
    exp_dir = bpath / "4-experiences"
    target_low = clean_target.lower()
    scope_tag = normalize_project_tag(project) if project else ""
    if exp_dir.is_dir() and not _is_reparse_or_link(str(exp_dir)):
        for entry in sorted(os.scandir(exp_dir), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            ep = exp_dir / entry.name
            if _is_reparse_or_link(str(ep)) or not ep.is_file():
                continue
            try:
                txt = ep.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            eparsed = split_frontmatter(txt)
            efm = parse_frontmatter_values(eparsed[0]) if eparsed else {}
            if scope_tag:
                exp_projs = _extract_experience_projects(efm)
                if not exp_projs or not all(bool(project_tags_match((p,), (scope_tag,))) for p in exp_projs):
                    continue
            succ_raw = str(efm.get("success") or "").strip().lower()
            is_failure = succ_raw in {"false", "0", "no"}
            if is_failure and target_low in txt.lower():
                _link(clean_target, f"incident:{entry.name}", "historical_failure_risk", "inferred_risk", entry.name)

    root_key = _normalize_node_key(clean_target)
    display_names.setdefault(root_key, clean_target)
    # Match any concept key that contains root_key as a token if exact key wasn't populated
    seeds = [root_key]
    for ck in list(adj):
        if ck != root_key and root_key in ck:
            seeds.append(ck)

    visited: set[str] = set(seeds)
    q: deque[tuple[str, int]] = deque((s, 0) for s in seeds)
    impacted_nodes: list[dict[str, Any]] = []
    traversed_edges: list[dict[str, Any]] = []
    prov_counts = {"verified_vault_edge": 0, "code_ast_dependency": 0, "inferred_risk": 0}

    while q:
        cur, dist = q.popleft()
        if dist >= hops_cap:
            continue
        for nxt, meta in adj.get(cur, ()):
            prov = meta["provenance"]
            prov_counts[prov] = prov_counts.get(prov, 0) + 1
            traversed_edges.append({**meta, "hop": dist + 1})
            if nxt not in visited:
                visited.add(nxt)
                impacted_nodes.append(
                    {
                        "node": display_names.get(nxt, nxt),
                        "node_key": nxt,
                        "hop": dist + 1,
                        "provenance": prov,
                        "relation": meta["relation"],
                        "evidence": meta["evidence"],
                    }
                )
                q.append((nxt, dist + 1))

    return {
        "target": clean_target,
        "project": project,
        "max_hops": hops_cap,
        "impacted_count": len(impacted_nodes),
        "impacted_nodes": impacted_nodes,
        "edges": traversed_edges,
        "provenance_summary": prov_counts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain World-Model Impact Simulator")
    parser.add_argument("target")
    parser.add_argument("--max-hops", type=int, default=3)
    parser.add_argument("--project", default=None)
    parser.add_argument("--repo", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    res = simulate_change_impact(args.target, max_hops=args.max_hops, project=args.project, repo_dir=args.repo)
    print(json.dumps(res, indent=2) if args.json else f"target={res['target']} impacted={res['impacted_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
