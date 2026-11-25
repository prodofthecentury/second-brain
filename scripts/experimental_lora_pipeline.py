# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Owner-ADR-Gated Experimental Local LoRA Pipeline (Epic 2D.7).

STRICTLY DISABLED by default unless ``ADR-LORA-EXPERIMENT`` is active in
``brain/0-raw/archive/owner-adrs.jsonl``.
- Exports ONLY DLP-scrubbed owner-approved atoms into isolated scratch JSONL.
- Generates reproducible Unsloth/PEFT LoRA configs for compact local models.
- Enforces mandatory holdout benchmark regression gating (0% trap leakage,
  >= baseline grounding) before generating a side-by-side Modelfile
  (``second-brain-lora-exp``). Never mutates base model weights or 2-atoms/.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("experimental_lora_pipeline", "scripts.experimental_lora_pipeline"):
    sys.modules.setdefault("experimental_lora_pipeline", sys.modules[__name__])
    sys.modules.setdefault("scripts.experimental_lora_pipeline", sys.modules[__name__])

from adr_gate import require_adr_active  # noqa: E402
from atom_mutator import atomic_write_bytes  # noqa: E402
from config import (  # noqa: E402
    _is_reparse_or_link,
    is_content_markdown,
    resolve_module_atoms_and_brain,
    snapshot_module_defaults,
)
from hardware_profile import detect_hardware_profile  # noqa: E402
from host_adapters import inspect_and_redact_outbound  # noqa: E402
from memory_format import evaluate_temporal_validity, split_frontmatter  # noqa: E402
from memory_review import parse_atom_record_text, parse_frontmatter_values  # noqa: E402
from review_atom import approved_revision_is_current  # noqa: E402

_MOD_PATH_DEFAULTS = snapshot_module_defaults(globals())
_BASE_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")


class LoraExperimentGateError(ValueError):
    """Raised when LoRA export/registration fails holdout regression or safety gates."""


def _validate_base_model(base_model: str) -> str:
    """Reject whitespace, newlines, control chars, or path traversal in base_model identifiers."""
    raw = str(base_model or "")
    if (
        not raw
        or raw != raw.strip()
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)
        or ".." in raw
        or not _BASE_MODEL_RE.fullmatch(raw)
    ):
        raise LoraExperimentGateError(f"Invalid base_model identifier: {base_model!r}")
    return raw


def _exp_dir(brain_dir: str, run_id: str) -> Path:
    clean_run = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (run_id or "default")).strip("-") or "default"
    bpath = Path(brain_dir)
    if os.path.lexists(str(bpath)) and _is_reparse_or_link(str(bpath)):
        raise PermissionError(f"Refusing symlinked brain dir: {bpath}")
    sidecar = bpath / ".sidecar"
    lora_root = sidecar / "lora_experiments"
    root = lora_root / clean_run
    for cand in (sidecar, lora_root, root):
        if os.path.lexists(str(cand)) and _is_reparse_or_link(str(cand)):
            raise PermissionError(f"Refusing symlinked LoRA scratch dir: {cand}")
    root.mkdir(parents=True, exist_ok=True)
    if _is_reparse_or_link(str(root)):
        raise PermissionError(f"Refusing symlinked LoRA scratch dir: {root}")
    return root


def export_lora_training_pairs(
    *,
    run_id: str = "exp-v1",
    base_model: str = "qwen2.5:3b",
    brain_dir: str | Path | None = None,
    atoms_dir: str | Path | None = None,
    include_preferences: bool = False,
    include_expired: bool = False,
) -> dict[str, Any]:
    """Require ADR-LORA-EXPERIMENT, export DLP-scrubbed approved pairs, and emit PEFT config."""
    safe_base_model = _validate_base_model(base_model)
    resolved_brain, resolved_atoms = resolve_module_atoms_and_brain(
        globals(),
        atoms_dir=atoms_dir,
        brain_dir=brain_dir,
    )
    require_adr_active("ADR-LORA-EXPERIMENT", brain_dir=resolved_brain)

    out_dir = _exp_dir(resolved_brain, run_id)
    apath = Path(resolved_atoms)
    pairs: list[dict[str, Any]] = []
    redacted_total = 0

    if apath.is_dir() and not _is_reparse_or_link(str(apath)):
        for entry in sorted(os.scandir(apath), key=lambda e: e.name):
            if not is_content_markdown(entry.name):
                continue
            p = apath / entry.name
            if not p.is_file() or _is_reparse_or_link(str(p)):
                continue
            try:
                raw = p.read_text(encoding="utf-8")
                rec = parse_atom_record_text(entry.name, raw)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if (
                rec.get("review_status") != "accepted"
                or rec.get("claim_state") != "human_reviewed"
                or rec.get("superseded_by")
                or not approved_revision_is_current(str(p), brain_dir=resolved_brain, atoms_dir=resolved_atoms)
            ):
                continue
            parsed = split_frontmatter(raw)
            fm_vals = parse_frontmatter_values(parsed[0]) if parsed else {}
            epistemic_label = str(fm_vals.get("epistemic_label") or rec.get("epistemic_label") or "claim").strip().lower()
            if not include_preferences and (epistemic_label == "preference" or bool(fm_vals.get("epistemic_contract"))):
                continue
            if not include_expired:
                vf = str(fm_vals.get("valid_from") or "").strip()
                vu = str(fm_vals.get("valid_until") or "").strip()
                if vf or vu:
                    tv = evaluate_temporal_validity(vf, vu)
                    if tv.get("status") != "active":
                        continue
            title = str(fm_vals.get("title") or entry.name[:-3]).strip()
            claim = str(rec.get("claim") or "").strip()
            prompt_raw = f"Summarize verified knowledge about {title} and cite the source atom."
            reply_raw = f"{claim} [verified: {entry.name}]"
            r_prompt = inspect_and_redact_outbound(prompt_raw, redact_pii=True, fail_on_secret_detection=True)
            r_reply = inspect_and_redact_outbound(reply_raw, redact_pii=True, fail_on_secret_detection=True)
            redacted_total += r_prompt.pii_redaction_count + r_reply.pii_redaction_count
            pairs.append(
                {
                    "instruction": r_prompt.sanitized_text,
                    "response": r_reply.sanitized_text,
                    "source_atom": entry.name,
                }
            )

    dataset_p = out_dir / "train_pairs.jsonl"
    if os.path.lexists(str(dataset_p)) and _is_reparse_or_link(str(dataset_p)):
        raise PermissionError(f"Refusing symlinked LoRA dataset output file: {dataset_p}")
    jsonl_text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pairs)
    atomic_write_bytes(str(dataset_p), jsonl_text.encode("utf-8"))

    hw = detect_hardware_profile()
    lora_cfg = {
        "schema_version": 1,
        "adr_id": "ADR-LORA-EXPERIMENT",
        "run_id": run_id,
        "base_model": safe_base_model,
        "target_adapter_name": "second-brain-lora-exp",
        "method": "peft_qlora",
        "lora_r": 16 if hw.tier_id != "tier_1_cpu_compact" else 8,
        "lora_alpha": 32 if hw.tier_id != "tier_1_cpu_compact" else 16,
        "lora_dropout": 0.05,
        "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
        "never_mutate_base_weights": True,
        "dataset_path": str(dataset_p),
        "pair_count": len(pairs),
        "created_at": datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
    }
    cfg_p = out_dir / "lora_config.json"
    if os.path.lexists(str(cfg_p)) and _is_reparse_or_link(str(cfg_p)):
        raise PermissionError(f"Refusing symlinked LoRA config output file: {cfg_p}")
    atomic_write_bytes(str(cfg_p), (json.dumps(lora_cfg, indent=2) + "\n").encode("utf-8"))
    return {
        "status": "exported",
        "run_id": run_id,
        "pair_count": len(pairs),
        "pii_redactions": redacted_total,
        "dataset_path": str(dataset_p),
        "config_path": str(cfg_p),
    }


def register_experimental_modelfile(
    *,
    run_id: str = "exp-v1",
    base_model: str = "qwen2.5:3b",
    holdout_eval_report: dict[str, Any],
    brain_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Emit side-by-side second-brain-lora-exp Modelfile ONLY after holdout regression gate passes."""
    safe_base_model = _validate_base_model(base_model)
    resolved_brain, _ = resolve_module_atoms_and_brain(globals(), brain_dir=brain_dir)
    require_adr_active("ADR-LORA-EXPERIMENT", brain_dir=resolved_brain)

    if not isinstance(holdout_eval_report, dict):
        raise LoraExperimentGateError("holdout_eval_report dictionary is required.")
    trap_leak = float(holdout_eval_report.get("trap_leakage_rate", 1.0))
    grounding = float(holdout_eval_report.get("grounding_score", 0.0))
    baseline = float(holdout_eval_report.get("baseline_grounding_score", 1.0))
    if trap_leak > 0.0:
        raise LoraExperimentGateError(f"Refused LoRA registration: trap_leakage_rate={trap_leak} > 0.0.")
    if grounding < baseline:
        raise LoraExperimentGateError(
            f"Refused LoRA registration: grounding_score={grounding} < baseline={baseline}."
        )

    out_dir = _exp_dir(resolved_brain, run_id)
    modelfile_p = out_dir / "Modelfile.second-brain-lora-exp"
    if os.path.lexists(str(modelfile_p)) and _is_reparse_or_link(str(modelfile_p)):
        raise PermissionError(f"Refusing symlinked Modelfile output path: {modelfile_p}")
    content = (
        f"FROM {safe_base_model}\n"
        f"# Side-by-side experimental adapter registration (never mutates {safe_base_model})\n"
        "ADAPTER ./adapter.gguf\n"
        "PARAMETER temperature 0.1\n"
    )
    atomic_write_bytes(str(modelfile_p), content.encode("utf-8"))
    return {
        "status": "modelfile_staged",
        "model_alias": "second-brain-lora-exp",
        "modelfile_path": str(modelfile_p),
        "trap_leakage_rate": trap_leak,
        "grounding_score": grounding,
        "baseline_grounding_score": baseline,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ADR-Gated Experimental LoRA Pipeline")
    parser.add_argument("--run-id", default="exp-v1")
    parser.add_argument("--base-model", default="qwen2.5:3b")
    args = parser.parse_args(argv)
    out = export_lora_training_pairs(run_id=args.run_id, base_model=args.base_model)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
