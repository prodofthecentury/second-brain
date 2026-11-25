# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Behavioral test runner for the VS Code Second Brain extension (`extensions/vscode-second-brain`).

Executes `node --test test/extension.test.js` (mocked VS Code host + live loopback API
+ webview DOM XSS/citation/cancellation harness) and checks zero raw HTML injection sinks.
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


from pathlib import Path
import re
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
EXT_DIR = ROOT / "extensions" / "vscode-second-brain"
FORBIDDEN_HTML_SINK_RE = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML)\b|document\.write\s*\(")


def check_zero_html_sinks() -> list[str]:
    violations: list[str] = []
    for js_file in [EXT_DIR / "extension.js", *sorted((EXT_DIR / "lib").glob("*.js"))]:
        if not js_file.is_file():
            continue
        for lineno, line in enumerate(js_file.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if FORBIDDEN_HTML_SINK_RE.search(line):
                violations.append(f"{js_file.relative_to(ROOT)}:{lineno}: {stripped}")
    return violations


def main() -> int:
    node_bin = shutil.which("node")
    if not node_bin:
        print("FAIL: `node` executable not found on PATH.", file=sys.stderr)
        return 1

    violations = check_zero_html_sinks()
    if violations:
        print("FAIL: Forbidden HTML sink(s) detected in VS Code extension sources:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    cmd = [node_bin, "--test", "test/extension.test.js"]
    res = subprocess.run(
        cmd,
        cwd=str(EXT_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode != 0:
        return res.returncode

    print("VS Code extension behavioral suite & XSS sink audit: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
