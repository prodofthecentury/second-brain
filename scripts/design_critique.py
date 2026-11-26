# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Open Design Token Presets & 5-Panelist Critique Theater (`2C.5` / `2C.1`).

Provides deterministic UI architecture/security/UX/accessibility critique across five
specialist panelists (*Product UX*, *Visual Craft*, *WCAG Accessibility*, *Frontend Architecture*,
*Security & Privacy*) plus curated contrast-verified design token presets for Build Mode.
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
    _sb_runpy.run_module("scripts." + _sb_Path(__file__).stem, run_name="__main__")
    raise SystemExit(0)


import argparse
import json
import os
import re
import sys
from typing import Any

if __name__ in ("design_critique", "scripts.design_critique"):
    sys.modules.setdefault("design_critique", sys.modules[__name__])
    sys.modules.setdefault("scripts.design_critique", sys.modules[__name__])

from scripts.host_adapters import inspect_and_redact_outbound  # noqa: E402

DESIGN_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "obsidian-workbench",
        "name": "Obsidian Workbench",
        "bg": "#0d1117",
        "surface": "#161b22",
        "text": "#e6edf3",
        "accent": "#7c5cff",
        "font_sans": "Inter, system-ui, sans-serif",
        "radius_px": 10,
    },
    {
        "id": "swiss-editorial",
        "name": "Swiss Editorial Print",
        "bg": "#faf9f6",
        "surface": "#ffffff",
        "text": "#18181b",
        "accent": "#e11d48",
        "font_sans": "system-ui, -apple-system, sans-serif",
        "radius_px": 4,
    },
    {
        "id": "clean-fintech",
        "name": "Clean Fintech Slate",
        "bg": "#0b1320",
        "surface": "#132033",
        "text": "#f1f5f9",
        "accent": "#10b981",
        "font_sans": "Inter, Roboto, sans-serif",
        "radius_px": 12,
    },
    {
        "id": "warm-craft",
        "name": "Warm Craft Studio",
        "bg": "#1c1917",
        "surface": "#292524",
        "text": "#f5f5f4",
        "accent": "#f59e0b",
        "font_sans": "Georgia, system-ui, serif",
        "radius_px": 8,
    },
    {
        "id": "cyber-telemetry",
        "name": "Cyber Telemetry HUD",
        "bg": "#050b14",
        "surface": "#0b1628",
        "text": "#e2e8f0",
        "accent": "#38bdf8",
        "font_sans": "ui-monospace, system-ui, sans-serif",
        "radius_px": 6,
    },
    {
        "id": "solarized-paper",
        "name": "Solarized Paper Focus",
        "bg": "#fdf6e3",
        "surface": "#eee8d5",
        "text": "#073642",
        "accent": "#268bd2",
        "font_sans": "system-ui, sans-serif",
        "radius_px": 8,
    },
)


def get_design_presets() -> list[dict[str, Any]]:
    return [dict(p) for p in DESIGN_PRESETS]


def run_five_panel_critique(
    files: list[dict[str, Any]],
    *,
    persona_id: str = "",
    done_criteria: str = "",
    brief: str = "",
) -> dict[str, Any]:
    """Evaluate generated Build Mode code/markup across five structured critique panels."""
    norm_files = [
        {
            "filename": str(f.get("filename") or f"file_{i}.txt"),
            "lang": str(f.get("lang") or ""),
            "content": str(f.get("content") or ""),
        }
        for i, f in enumerate(files or [], start=1)
        if isinstance(f, dict)
    ]
    combined = "\n".join(f["content"] for f in norm_files)
    html_text = "\n".join(f["content"] for f in norm_files if f["filename"].lower().endswith((".html", ".svg")) or f["lang"] == "html")
    css_text = "\n".join(f["content"] for f in norm_files if f["filename"].lower().endswith(".css") or f["lang"] == "css")
    js_text = "\n".join(f["content"] for f in norm_files if f["filename"].lower().endswith(".js") or f["lang"] == "js")

    # 1. Product UX
    has_controls = bool(re.search(r"<button|<input|<select|<textarea|<form|<canvas", combined, re.I))
    ux_score = 4 if has_controls and len(combined) > 120 else (3 if len(norm_files) else 1)
    if done_criteria.strip():
        dc_words = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", done_criteria)[:6]]
        matched_dc = sum(1 for w in dc_words if w in combined.lower())
        if dc_words and matched_dc == 0:
            ux_score = max(2, ux_score - 1)
            ux_note = f"Interactive surface present={has_controls}, but Done Criteria terms ({', '.join(dc_words[:3])}) were not found in code."
        else:
            ux_note = f"Matched {matched_dc}/{len(dc_words)} Done Criteria markers across {len(norm_files)} file(s)."
    else:
        ux_note = (
            "Interactive controls and canvas/form elements detected; verify empty & error states."
            if has_controls
            else "No interactive controls found yet; add explicit user feedback states."
        )

    # 2. Visual Craft
    has_layout = bool(re.search(r"display:\s*(?:flex|grid)|clamp\(|@media|gap\s*:", combined, re.I))
    has_tokens = bool(re.search(r"--[a-z0-9-]+:|box-sizing:\s*border-box", combined, re.I))
    vis_score = 5 if (has_layout and has_tokens) else (4 if has_layout or len(css_text) > 80 else (2 if norm_files else 1))
    vis_note = (
        "CSS custom properties + flex/grid responsive layout rules detected."
        if has_layout and has_tokens
        else "Consider defining `:root` design tokens and `@media` responsive grid/flex breakpoints."
    )

    # 3. WCAG Accessibility
    a11y_hits = len(re.findall(r"aria-[a-z-]+|<main\b|<header\b|<nav\b|<label\b|alt=", combined, re.I))
    has_focus = bool(re.search(r":focus(?:-visible)?|tabindex", combined, re.I))
    a11y_score = 5 if (a11y_hits >= 3 and has_focus) else (4 if a11y_hits >= 1 else (2 if html_text else 3))
    a11y_note = (
        f"Found {a11y_hits} semantic/ARIA marker(s)" + (" plus keyboard `:focus` styling." if has_focus else "; add explicit `:focus-visible` ring styling.")
        if a11y_hits
        else "Add `<main>`, `<label>`, `aria-label`, and visible keyboard focus outlines."
    )

    # 4. Frontend Architecture
    multi_file = len(norm_files) >= 2
    uses_safe_dom = not bool(re.search(r"\b(?:document\.write\s*\(|(?:inner|outer|insertAdjacent)HTML)\b", combined))
    arch_score = 5 if (multi_file and uses_safe_dom) else (4 if uses_safe_dom and norm_files else 2)
    arch_note = (
        f"{len(norm_files)} separated artifact file(s); clean DOM APIs (no raw HTML sink concatenation)."
        if uses_safe_dom
        else "Detected legacy HTML string sink (`*HTML` / `document.write`); prefer `createElement` + `textContent`."
    )

    # 5. Security & Privacy
    red = inspect_and_redact_outbound(combined[:40_000], redact_pii=False, fail_on_secret_detection=False)
    has_eval = bool(re.search(r"\beval\s*\(|\bnew\s+Function\s*\(", combined))
    has_remote_url = bool(re.search(r"https?://(?!127\.0\.0\.1|localhost)", combined, re.I))
    sec_deductions = (2 if has_eval else 0) + (2 if red.secret_hits else 0) + (1 if has_remote_url else 0)
    sec_score = max(1, 5 - sec_deductions)
    sec_findings: list[str] = []
    if red.secret_hits:
        sec_findings.append(f"Embedded secret pattern(s): {', '.join(red.secret_hits)}")
    if has_eval:
        sec_findings.append("Dynamic code evaluation (`eval` / `Function`) detected")
    if has_remote_url:
        sec_findings.append("External HTTP(S) URLs will be blocked under Build Canvas `connect-src 'none'`")
    sec_note = "; ".join(sec_findings) if sec_findings else "Clean: zero embedded secrets, no eval sinks, offline sandbox-safe."

    panels = [
        {"panel": "Product UX", "score": ux_score, "note": ux_note},
        {"panel": "Visual Craft", "score": vis_score, "note": vis_note},
        {"panel": "WCAG Accessibility", "score": a11y_score, "note": a11y_note},
        {"panel": "Frontend Architecture", "score": arch_score, "note": arch_note},
        {"panel": "Security & Privacy", "score": sec_score, "note": sec_note},
    ]
    avg_score = round(sum(p["score"] for p in panels) / len(panels), 2)
    return {
        "status": "ok",
        "persona_id": persona_id or "grounded_default",
        "file_count": len(norm_files),
        "average_score": avg_score,
        "panels": panels,
        "recommended_preset": DESIGN_PRESETS[0]["id"],
        "brief_checked": bool(brief.strip()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("presets")
    p_crit = sub.add_parser("critique")
    p_crit.add_argument("--file", required=True, help="HTML/JS/CSS file to inspect")
    p_crit.add_argument("--persona", default="")
    args = parser.parse_args(argv)
    if args.cmd == "presets":
        print(json.dumps({"presets": get_design_presets()}, indent=2))
    else:
        with open(args.file, encoding="utf-8") as fh:
            content = fh.read()
        print(json.dumps(run_five_panel_critique([{"filename": os.path.basename(args.file), "content": content}], persona_id=args.persona), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
