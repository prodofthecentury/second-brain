# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Meta-Reasoning Router & Adaptive Cognitive Budget (Epic 2D.5).

Classifies query difficulty & reasoning mode (`deduction`, `induction`,
`abduction`, `analogy`, `causal_simulation`, `quantitative_derivation`,
`formal_proof`) and allocates a hardware-adaptive Cognitive Budget
(`local_compact`, `local_gpu`, or `cloud_hybrid`) without assuming heavy GPU
clusters or slow laptop caps.
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
from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

if __name__ in ("meta_router", "scripts.meta_router"):
    sys.modules.setdefault("meta_router", sys.modules[__name__])
    sys.modules.setdefault("scripts.meta_router", sys.modules[__name__])

from scripts.hardware_profile import detect_hardware_profile  # noqa: E402
from scripts.recall import RecallOptions, search_memory  # noqa: E402

REASONING_MODES = (
    "lookup",
    "deduction",
    "induction",
    "abduction",
    "analogy",
    "causal_simulation",
    "quantitative_derivation",
    "formal_proof",
)

_PROOF_RE = re.compile(
    r"\b(?:prove|theorem|lemma|formal(?:ize)?|corollary|invariant|contradiction\s+proof|induction\s+on|lean4?|qed|unsat|z3)\b",
    re.IGNORECASE,
)
_QUANT_RE = re.compile(
    r"(?:\$[^$]+\$|\\frac|\\sum|\\int|\b(?:solve|calculate|compute|evaluate|simplify|integrate|differentiate|probability|variance|derivative|equation|polynomial|ratio|percent)\b|\d+\s*[\+\-\*/\^=]\s*\d+)",
    re.IGNORECASE,
)
_CAUSAL_RE = re.compile(
    r"\b(?:if\s+i\s+change|what\s+breaks|downstream|blast\s+radius|impact\s+of|causes?|depends\s+on|simulate|failure\s+mode|cascade)\b",
    re.IGNORECASE,
)
_ABDUCTION_RE = re.compile(
    r"\b(?:why\s+did|root\s+cause|debug|diagnose|failing|regression|incident|error)\b",
    re.IGNORECASE,
)
_ANALOGY_RE = re.compile(
    r"\b(?:analogous|similar\s+to|compare|versus|vs\.?|trade-?off|like\s+how)\b",
    re.IGNORECASE,
)
_INDUCTION_RE = re.compile(
    r"\b(?:pattern|across\s+projects|principle|trend|synthesize|generalize|recurring)\b",
    re.IGNORECASE,
)
_MULTI_HOP_RE = re.compile(
    r"\b(?:and\s+also|how\s+does\s+.+\s+affect|multi-step|step[- ]by[- ]step|end[- ]to[- ]end|reconcile|debate)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CognitiveBudget:
    complexity: str  # trivial_lookup | standard_synthesis | complex_multi_hop | quantitative_derivation | formal_proof
    reasoning_mode: str
    path: str  # fast_path | deliberative_path
    reasoning_profile: str  # local_compact | local_gpu | cloud_hybrid
    max_hops: int
    n_results: int
    candidate_samples_k: int
    max_refinement_rounds: int
    coverage_threshold: float
    use_critic_pass: bool
    use_symbolic_sandbox: bool


def resolve_reasoning_profile(
    requested: str = "auto",
    *,
    has_cloud_consent: bool = False,
) -> str:
    clean = (requested or "auto").strip().lower()
    if clean in {"local_compact", "local_gpu", "cloud_hybrid"}:
        return clean
    if has_cloud_consent:
        return "cloud_hybrid"
    hw = detect_hardware_profile()
    if hw.tier_id == "tier_3_gpu_accelerated" or hw.total_ram_gib >= 24.0:
        return "local_gpu"
    return "local_compact"


def classify_query(
    query: str,
    *,
    reasoning_profile: str = "auto",
    has_cloud_consent: bool = False,
) -> CognitiveBudget:
    """Classify query difficulty & archetype and compute an adaptive Cognitive Budget."""
    text = (query or "").strip()
    profile = resolve_reasoning_profile(reasoning_profile, has_cloud_consent=has_cloud_consent)

    if _PROOF_RE.search(text):
        mode = "formal_proof"
        complexity = "formal_proof"
    elif _QUANT_RE.search(text):
        mode = "quantitative_derivation"
        complexity = "quantitative_derivation"
    elif _CAUSAL_RE.search(text):
        mode = "causal_simulation"
        complexity = "complex_multi_hop"
    elif _ABDUCTION_RE.search(text):
        mode = "abduction"
        complexity = "complex_multi_hop"
    elif _ANALOGY_RE.search(text):
        mode = "analogy"
        complexity = "complex_multi_hop" if len(text.split()) > 10 else "standard_synthesis"
    elif _INDUCTION_RE.search(text):
        mode = "induction"
        complexity = "complex_multi_hop"
    elif _MULTI_HOP_RE.search(text) or len(text.split()) >= 14 or text.count("?") > 1:
        mode = "deduction"
        complexity = "complex_multi_hop"
    elif len(text.split()) <= 7 and not re.search(r"\b(?:why|explain|design|compare|plan)\b", text, re.IGNORECASE):
        mode = "lookup"
        complexity = "trivial_lookup"
    else:
        mode = "deduction"
        complexity = "standard_synthesis"

    deliberative = complexity in {"complex_multi_hop", "quantitative_derivation", "formal_proof"}
    if profile == "cloud_hybrid":
        k_samples = 8 if deliberative else 1
        n_res = 10 if deliberative else 6
        max_rounds = 3
        max_hops = 3 if deliberative else 1
    elif profile == "local_gpu":
        k_samples = 5 if deliberative else 1
        n_res = 8 if deliberative else 5
        max_rounds = 3
        max_hops = 3 if deliberative else 1
    else:
        k_samples = 3 if deliberative else 1
        n_res = 6 if deliberative else 5
        max_rounds = 2 if deliberative else 1
        max_hops = 2 if deliberative else 1

    return CognitiveBudget(
        complexity=complexity,
        reasoning_mode=mode,
        path="deliberative_path" if deliberative else "fast_path",
        reasoning_profile=profile,
        max_hops=max_hops,
        n_results=n_res,
        candidate_samples_k=k_samples,
        max_refinement_rounds=max_rounds,
        coverage_threshold=0.45,
        use_critic_pass=deliberative,
        use_symbolic_sandbox=(mode in {"quantitative_derivation", "formal_proof"}),
    )


def estimate_evidence_coverage(query: str, records: list[Any]) -> float:
    """Measure non-stopword lexical query coverage against retrieved approved claims (0.0..1.0)."""
    q_tokens = {
        tok.lower()
        for tok in re.findall(r"[a-zA-Z0-9_-]{3,}", query or "")
        if tok.lower() not in {"what", "when", "where", "which", "does", "how", "why", "with", "that", "this", "from", "into", "should", "could"}
    }
    if not q_tokens:
        return 1.0 if records else 0.0
    if not records:
        return 0.0
    corpus = " ".join(str(getattr(r, "content", None) or (r.get("claim", "") if isinstance(r, dict) else "")) for r in records).lower()
    covered = sum(1 for tok in q_tokens if tok in corpus)
    return round(covered / len(q_tokens), 4)


def route_and_retrieve(
    query: str,
    *,
    project: str | None = None,
    reasoning_profile: str = "auto",
    has_cloud_consent: bool = False,
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    lexical_index_path: str | Path | None = None,
    chroma_path: str | Path | None = None,
    retrieve_fn: Callable[[str, int, str | None], tuple[list[Any], list[Any]]] | None = None,
) -> dict[str, Any]:
    """Execute Fast Path (<15ms lookup) or Deliberative Multi-Hop Path with mid-flight coverage re-query."""
    budget = classify_query(query, reasoning_profile=reasoning_profile, has_cloud_consent=has_cloud_consent)

    def _default_retrieve(q: str, n: int, proj: str | None) -> tuple[list[Any], list[Any]]:
        eff_atoms = str(atoms_dir) if atoms_dir is not None else (str(Path(brain_dir) / "2-atoms") if brain_dir else None)
        eff_lex = str(lexical_index_path) if lexical_index_path is not None else (str(Path(brain_dir) / ".sidecar" / "lexical.sqlite3") if brain_dir else None)
        eff_chroma = str(chroma_path) if chroma_path is not None else (str(Path(brain_dir) / ".chroma") if brain_dir else None)
        kwargs: dict[str, Any] = {}
        if eff_atoms is not None:
            kwargs["atoms_dir"] = eff_atoms
        if eff_lex is not None:
            kwargs["index_path"] = eff_lex
        if eff_chroma is not None:
            kwargs["chroma_path"] = eff_chroma
        d, nb = search_memory(
            q,
            RecallOptions(
                n_results=n,
                follow_links=True,
                include_statuses={"accepted"},
                projects=(proj,) if proj else (),
                retrieval_mode="lexical" if eff_chroma and not Path(eff_chroma).exists() else "auto",
            ),
            **kwargs,
        )
        return list(d), list(nb)

    runner = retrieve_fn or _default_retrieve
    direct, neighbors = runner(query, budget.n_results, project)
    combined = [*direct, *neighbors]
    coverage = estimate_evidence_coverage(query, combined)
    requeried = False
    expanded_queries: list[str] = []

    if budget.path == "deliberative_path" and coverage < budget.coverage_threshold:
        requeried = True
        terms = [t for t in re.findall(r"[a-zA-Z0-9_-]{3,}", query) if len(t) >= 4]
        seen_names = {getattr(r, "filename", None) or (r.get("filename") if isinstance(r, dict) else id(r)) for r in combined}
        for term in terms[:3]:
            expanded_queries.append(term)
            d2, n2 = runner(term, budget.n_results, project)
            for item in [*d2, *n2]:
                key = getattr(item, "filename", None) or (item.get("filename") if isinstance(item, dict) else id(item))
                if key not in seen_names:
                    seen_names.add(key)
                    combined.append(item)
        coverage = estimate_evidence_coverage(query, combined)

    return {
        "query": query,
        "budget": asdict(budget),
        "coverage": coverage,
        "mid_flight_requeried": requeried,
        "expanded_queries": expanded_queries,
        "direct_count": len(direct),
        "neighbor_count": len(neighbors),
        "total_results": len(combined),
        "results": combined,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Meta-Reasoning Router")
    parser.add_argument("query")
    parser.add_argument("--profile", default="auto", choices=("auto", "local_compact", "local_gpu", "cloud_hybrid"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    budget = classify_query(args.query, reasoning_profile=args.profile)
    print(json.dumps(asdict(budget), indent=2) if args.json else f"{budget.path} mode={budget.reasoning_mode} profile={budget.reasoning_profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
