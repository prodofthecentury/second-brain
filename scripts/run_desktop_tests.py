# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Behavioral & packaged smoke test runner for the Electron Desktop app (`desktop/`).

1. Verifies zero forbidden raw HTML sinks across `desktop/*.js`, `desktop/lib/*.js`,
   and `desktop/renderer/*.{js,html}`.
2. Executes `npm test` (`node --test tests/`) in `desktop/`.
3. When a packaged desktop binary is present under `dist/` (e.g.
   `dist/win-unpacked/Second Brain.exe` on Windows), executes `--smoke-test`
   with an isolated security/port-discovery state dir and verifies
   `ELECTRON_SMOKE_RESULT` (`apiReady == True` and `health.ok == True`).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_DIR = ROOT / "desktop"
FORBIDDEN_HTML_SINK_RE = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML)\b|document\.write\s*\(")


def check_zero_html_sinks() -> list[str]:
    violations: list[str] = []
    candidates = [
        DESKTOP_DIR / "main.js",
        DESKTOP_DIR / "preload.js",
        *sorted((DESKTOP_DIR / "lib").glob("*.js")),
        *sorted((DESKTOP_DIR / "renderer").glob("*.js")),
        *sorted((DESKTOP_DIR / "renderer").glob("*.html")),
    ]
    for src in candidates:
        if not src.is_file():
            continue
        for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue
            if FORBIDDEN_HTML_SINK_RE.search(line):
                violations.append(f"{src.relative_to(ROOT)}:{lineno}: {stripped}")
    return violations


def find_packaged_binary() -> Path | None:
    if sys.platform == "win32":
        candidate = ROOT / "dist" / "win-unpacked" / "Second Brain.exe"
        return candidate if candidate.is_file() else None
    if sys.platform == "darwin":
        for sub in ("mac-universal", "mac", "mac-arm64", "mac-x64"):
            candidate = ROOT / "dist" / sub / "Second Brain.app" / "Contents" / "MacOS" / "Second Brain"
            if candidate.is_file():
                return candidate
        return None
    for sub in ("linux-unpacked",):
        for exe_name in ("second-brain-desktop", "Second Brain"):
            candidate = ROOT / "dist" / sub / exe_name
            if candidate.is_file():
                return candidate
    return None


def run_packaged_smoke_test(exe_path: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="sb-desktop-smoke-") as tmp_str:
        tmp_dir = Path(tmp_str)
        sec_dir = tmp_dir / "security"
        sec_dir.mkdir(parents=True, exist_ok=True)
        out_path = tmp_dir / "smoke.out"
        err_path = tmp_dir / "smoke.err"

        env = os.environ.copy()
        env["BRAIN_SECURITY_STATE_DIR"] = str(sec_dir)
        env["BRAIN_VAULT_DIR"] = str(tmp_dir / "brain")
        env["ELECTRON_ENABLE_LOGGING"] = "1"

        cmd = [str(exe_path), "--smoke-test"]
        if sys.platform.startswith("linux") and not env.get("DISPLAY"):
            xvfb = shutil.which("xvfb-run")
            if not xvfb:
                return True, "Skipped Linux packaged smoke-test (no DISPLAY or xvfb-run)."
            cmd = [xvfb, "-a", *cmd, "--no-sandbox"]

        with out_path.open("wb") as out_fh, err_path.open("wb") as err_fh:
            proc = subprocess.run(
                cmd,
                cwd=str(tmp_dir),
                env=env,
                stdout=out_fh,
                stderr=err_fh,
                timeout=120,
                check=False,
            )

        stdout_text = out_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = err_path.read_text(encoding="utf-8", errors="replace")
        combined = f"{stdout_text}\n{stderr_text}"

        smoke_payload = None
        for line in combined.splitlines():
            marker_idx = line.find("ELECTRON_SMOKE_RESULT ")
            if marker_idx >= 0:
                raw_json = line[marker_idx + len("ELECTRON_SMOKE_RESULT ") :].strip()
                try:
                    smoke_payload = json.loads(raw_json)
                except json.JSONDecodeError:
                    pass

        if proc.returncode != 0:
            return (
                False,
                f"Packaged smoke-test exited with code {proc.returncode}.\nSTDOUT:\n{stdout_text}\nSTDERR:\n{stderr_text}",
            )
        if not isinstance(smoke_payload, dict):
            return (
                False,
                f"Missing ELECTRON_SMOKE_RESULT JSON in smoke-test output.\nSTDOUT:\n{stdout_text}\nSTDERR:\n{stderr_text}",
            )
        health = smoke_payload.get("health") or {}
        if not smoke_payload.get("apiReady") or not health.get("ok"):
            return (
                False,
                f"Unhealthy ELECTRON_SMOKE_RESULT payload: {json.dumps(smoke_payload)}",
            )
        return True, f"Packaged smoke-test PASS ({exe_path.name}, apiPort={smoke_payload.get('apiPort')})"


def main() -> int:
    violations = check_zero_html_sinks()
    if violations:
        print("FAIL: Forbidden HTML sink(s) detected in Desktop sources:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    npm_bin = shutil.which("npm.cmd" if sys.platform == "win32" else "npm") or shutil.which("npm")
    node_bin = shutil.which("node")
    if npm_bin:
        test_cmd = [npm_bin, "test"]
    elif node_bin:
        test_cmd = [node_bin, "--test", "tests/"]
    else:
        print("FAIL: Neither `npm` nor `node` found on PATH.", file=sys.stderr)
        return 1

    res = subprocess.run(
        test_cmd,
        cwd=str(DESKTOP_DIR),
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if res.returncode != 0:
        return res.returncode

    exe_path = find_packaged_binary()
    if exe_path is not None:
        ok, detail = run_packaged_smoke_test(exe_path)
        print(detail)
        if not ok:
            return 1
    else:
        print("Packaged desktop binary not present in dist/; unit/DOM suites passed.")

    print("Desktop behavioral & XSS sink suite: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
