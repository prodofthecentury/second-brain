"""Clean-wheel namespace, isolated temp-venv CLI/API/Dashboard, VSIX, icon, wheelhouse, and CI tests.

Run:
    uv run --frozen python -m unittest scripts.wheel_packaging_tests -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _find_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _venv_paths(venv_dir: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        py_exe = venv_dir / "Scripts" / "python.exe"
        sb_exe = venv_dir / "Scripts" / "second-brain.exe"
    else:
        py_exe = venv_dir / "bin" / "python"
        sb_exe = venv_dir / "bin" / "second-brain"
    return py_exe, sb_exe


def _terminate_process_tree(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), 15)
                except Exception:
                    proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass


def _start_popen_isolated(cmd: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)  # type: ignore[arg-type]


class WheelPackagingAndReleaseTests(unittest.TestCase):
    _shared_tmp: tempfile.TemporaryDirectory | None = None
    _wheel_path: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._shared_tmp = tempfile.TemporaryDirectory(prefix="sb_wheel_pkg_suite_")
        wheel_out = Path(cls._shared_tmp.name) / "wheel_build"
        wheel_out.mkdir(parents=True, exist_ok=True)
        uv_bin = shutil.which("uv") or "uv"
        subprocess.run(
            [uv_bin, "build", "--wheel", "--out-dir", str(wheel_out)],
            cwd=str(REPO_ROOT),
            check=True,
            capture_output=True,
            text=True,
        )
        wheels = list(wheel_out.glob("second_brain-*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"Expected exactly 1 built wheel, found: {wheels}")
        cls._wheel_path = wheels[0]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._shared_tmp is not None:
            cls._shared_tmp.cleanup()
            cls._shared_tmp = None

    def test_wheel_archive_clean_namespace_and_bundled_assets(self) -> None:
        with zipfile.ZipFile(self._wheel_path, "r") as zf:
            names = set(zf.namelist())
            entry_points_files = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
            self.assertEqual(len(entry_points_files), 1)
            entry_points_txt = zf.read(entry_points_files[0]).decode("utf-8")

        for expected in (
            "second_brain/__init__.py",
            "second_brain/__main__.py",
            "second_brain/cli.py",
            "second_brain/scripts/cli.py",
            "second_brain/scripts/config.py",
            "second_brain/scripts/memory_api.py",
            "second_brain/dashboard/app.py",
            "second_brain/mobile/inbox-drop/index.html",
            "second_brain/mobile/inbox-drop/drop-crypto.js",
            "second_brain/_streamlit/config.toml",
        ):
            self.assertIn(expected, names, f"Missing expected wheel entry: {expected}")

        self.assertTrue(
            any(n.startswith("second_brain/plugins/") for n in names),
            "Wheel must bundle plugins under second_brain/plugins/",
        )

        polluted = [
            n
            for n in names
            if n.startswith(("scripts/", "dashboard/", "plugins/", "mobile/", ".streamlit/"))
        ]
        self.assertEqual(
            polluted,
            [],
            f"Wheel must NOT pollute top-level site-packages namespace; found: {polluted[:10]}",
        )
        leaked_tests = sorted(
            n
            for n in names
            if n.endswith(("_tests.py", "_canary.py"))
            or n.endswith(("/test_all.py", "/smoke_test.py", "/run_desktop_tests.py", "/run_vscode_tests.py"))
            or "/node_modules/" in n
        )
        self.assertEqual(
            leaked_tests,
            [],
            f"Production wheel must NOT bundle test/canary suites or node_modules; found: {leaked_tests}",
        )
        self.assertIn("second_brain/scripts/doc_fixtures.py", names)
        self.assertIn("second_brain/scripts/publish_registries.py", names)
        with zipfile.ZipFile(self._wheel_path, "r") as zf2:
            pub_src = zf2.read("second_brain/scripts/publish_registries.py").decode("utf-8")
        self.assertIn("import re\n", pub_src.replace("\r\n", "\n"))
        self.assertIn("second-brain = second_brain.cli:main", entry_points_txt)

    def test_isolated_venv_wheel_doctor_serve_and_headless_dashboard(self) -> None:
        uv_bin = shutil.which("uv") or "uv"
        with tempfile.TemporaryDirectory(prefix="sb_clean_venv_") as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            self.assertNotIn(REPO_ROOT.resolve(), (tmp, *tmp.parents))

            venv_dir = tmp / "venv"
            empty_cwd = tmp / "empty_cwd"
            vault_dir = tmp / "vault"
            sec_dir = tmp / "sec"
            fake_home = tmp / "fake_home"
            fake_localappdata = tmp / "fake_localappdata"
            fake_xdg_data = tmp / "fake_xdg_data"
            fake_xdg_state = tmp / "fake_xdg_state"
            for d in (empty_cwd, sec_dir, fake_home, fake_localappdata, fake_xdg_data, fake_xdg_state):
                d.mkdir(parents=True, exist_ok=True)

            py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            subprocess.run(
                [uv_bin, "venv", str(venv_dir), "--python", py_ver],
                cwd=str(empty_cwd),
                check=True,
                capture_output=True,
                text=True,
            )
            py_exe, sb_exe = _venv_paths(venv_dir)
            self.assertTrue(py_exe.is_file(), f"Missing venv python: {py_exe}")

            # Install minimal core wheel ONLY first (no dashboard/online/mcp extras)
            subprocess.run(
                [uv_bin, "pip", "install", "--python", str(py_exe), str(self._wheel_path)],
                cwd=str(empty_cwd),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(sb_exe.is_file(), f"Missing console script entrypoint: {sb_exe}")

            # 1. Verify site-packages in <tmp>/venv has NO top-level scripts, dashboard, or plugins dirs
            sp_probe = subprocess.run(
                [
                    str(py_exe),
                    "-c",
                    (
                        "import importlib.util, json, os, sysconfig; "
                        "sps = sorted({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')}); "
                        "bad = [name for sp in sps for name in ('scripts', 'dashboard', 'plugins') "
                        "if os.path.exists(os.path.join(sp, name))]; "
                        "has_sb = any(os.path.isdir(os.path.join(sp, 'second_brain')) for sp in sps); "
                        "heavy = {m: importlib.util.find_spec(m) is not None for m in ('chromadb', 'streamlit', 'ollama', 'mcp')}; "
                        "print(json.dumps({'bad': bad, 'has_sb': has_sb, 'heavy': heavy, 'sps': sps}))"
                    ),
                ],
                cwd=str(empty_cwd),
                check=True,
                capture_output=True,
                text=True,
            )
            sp_info = json.loads(sp_probe.stdout.strip())
            self.assertTrue(sp_info["has_sb"])
            self.assertEqual(sp_info["bad"], [])
            self.assertEqual(
                sp_info["heavy"],
                {"chromadb": False, "streamlit": False, "ollama": False, "mcp": False},
            )

            base_env = dict(os.environ)
            for k in (
                "VIRTUAL_ENV",
                "PYTHONPATH",
                "UV_PROJECT_ENVIRONMENT",
                "BRAIN_VAULT_DIR",
                "BRAIN_MEMORY_API_TOKEN",
                "BRAIN_OWNER_TOKEN",
            ):
                base_env.pop(k, None)
            base_env.update({
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": str(fake_home),
                "USERPROFILE": str(fake_home),
                "LOCALAPPDATA": str(fake_localappdata),
                "XDG_DATA_HOME": str(fake_xdg_data),
                "XDG_STATE_HOME": str(fake_xdg_state),
            })

            # Verify installed-wheel default vault resolution (when BRAIN_VAULT_DIR is unset)
            default_vault_probe = subprocess.run(
                [
                    str(py_exe),
                    "-c",
                    (
                        "from second_brain.cli import bootstrap_runtime; bootstrap_runtime(); "
                        "import config; print(config.BRAIN_DIR)"
                    ),
                ],
                cwd=str(empty_cwd),
                env=base_env,
                check=True,
                capture_output=True,
                text=True,
            )
            resolved_default_vault = Path(default_vault_probe.stdout.strip()).resolve()
            self.assertTrue(str(resolved_default_vault).startswith(str(tmp)))
            self.assertNotIn(REPO_ROOT.resolve(), (resolved_default_vault, *resolved_default_vault.parents))

            # Configure isolated vault + security paths for CLI / API / Dashboard
            api_token_file = sec_dir / "memory-api-token"
            owner_token_file = sec_dir / "owner-token"
            api_port_file = sec_dir / "api-port"
            dash_auth_file = sec_dir / "dash-auth.json"

            isolated_env = dict(base_env)
            isolated_env.update({
                "BRAIN_VAULT_DIR": str(vault_dir),
                "BRAIN_SECURITY_STATE_DIR": str(sec_dir),
                "BRAIN_MEMORY_API_TOKEN_FILE": str(api_token_file),
                "BRAIN_OWNER_TOKEN_FILE": str(owner_token_file),
                "BRAIN_API_PORT_FILE": str(api_port_file),
                "BRAIN_DASHBOARD_AUTH_FILE": str(dash_auth_file),
            })

            # 2. Initialize isolated vault and run <venv>/second-brain doctor
            install_res = subprocess.run(
                [str(sb_exe), "install"],
                cwd=str(empty_cwd),
                env=isolated_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(install_res.returncode, 0, f"install failed:\n{install_res.stdout}\n{install_res.stderr}")

            doctor_res = subprocess.run(
                [str(sb_exe), "doctor"],
                cwd=str(empty_cwd),
                env=isolated_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(doctor_res.returncode, 0, f"doctor failed:\n{doctor_res.stdout}\n{doctor_res.stderr}")
            self.assertIn(f"Vault: {vault_dir}", doctor_res.stdout)
            self.assertNotIn(str(REPO_ROOT), doctor_res.stdout)

            # 3. Run <venv>/second-brain serve --generate-token + <venv>/second-brain serve --port 0
            tok_res = subprocess.run(
                [str(sb_exe), "serve", "--generate-token"],
                cwd=str(empty_cwd),
                env=isolated_env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(tok_res.returncode, 0, f"serve --generate-token failed:\n{tok_res.stdout}\n{tok_res.stderr}")
            self.assertTrue(api_token_file.is_file(), f"Expected token at {api_token_file}; stdout={tok_res.stdout}")
            bearer_token = api_token_file.read_text(encoding="utf-8").strip()
            self.assertGreaterEqual(len(bearer_token), 24)

            api_proc: subprocess.Popen | None = None
            api_lines: list[str] = []
            try:
                api_proc = _start_popen_isolated(
                    [str(sb_exe), "serve", "--port", "0"],
                    cwd=empty_cwd,
                    env=isolated_env,
                )

                def _drain_api() -> None:
                    assert api_proc is not None and api_proc.stdout is not None
                    for line in api_proc.stdout:
                        api_lines.append(line.rstrip("\r\n"))

                reader = threading.Thread(target=_drain_api, daemon=True)
                reader.start()

                bound_port: int | None = None
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    if api_port_file.is_file():
                        try:
                            port_payload = json.loads(api_port_file.read_text(encoding="utf-8"))
                            if isinstance(port_payload, dict) and int(port_payload.get("port", 0)) > 0:
                                bound_port = int(port_payload["port"])
                                break
                        except (OSError, ValueError, json.JSONDecodeError):
                            pass
                    for line in list(api_lines):
                        m = re.search(r"\bport=(\d+)\b", line)
                        if m:
                            bound_port = int(m.group(1))
                            break
                    if bound_port:
                        break
                    if api_proc.poll() is not None:
                        break
                    time.sleep(0.1)

                self.assertIsNotNone(
                    bound_port,
                    f"Memory API did not report bound port. Output:\n" + "\n".join(api_lines),
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                req = urllib.request.Request(
                    f"http://127.0.0.1:{bound_port}/v1/health",
                    headers={"Authorization": f"Bearer {bearer_token}"},
                    method="GET",
                )
                with opener.open(req, timeout=5.0) as resp:
                    self.assertEqual(resp.status, 200)
                    health_json = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(health_json.get("status"), "ok")
            finally:
                _terminate_process_tree(api_proc)

            # 4. Install [dashboard] extra and verify headless `second-brain dashboard --port <free_port>`
            subprocess.run(
                [uv_bin, "pip", "install", "--python", str(py_exe), f"{self._wheel_path}[dashboard]"],
                cwd=str(empty_cwd),
                check=True,
                capture_output=True,
                text=True,
            )

            # Pre-seed scrypt password record at BRAIN_DASHBOARD_AUTH_FILE so non-interactive launch proceeds
            subprocess.run(
                [
                    str(py_exe),
                    "-c",
                    (
                        "from second_brain.cli import bootstrap_runtime; bootstrap_runtime(); "
                        "from dashboard_auth import build_password_record, save_password_record; "
                        "save_password_record(build_password_record('correct-horse-battery-staple'))"
                    ),
                ],
                cwd=str(empty_cwd),
                env=isolated_env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(dash_auth_file.is_file())

            dash_port = _find_free_loopback_port()
            dash_proc: subprocess.Popen | None = None
            dash_lines: list[str] = []
            try:
                dash_proc = _start_popen_isolated(
                    [str(sb_exe), "dashboard", "--port", str(dash_port)],
                    cwd=empty_cwd,
                    env=isolated_env,
                )

                def _drain_dash() -> None:
                    assert dash_proc is not None and dash_proc.stdout is not None
                    for line in dash_proc.stdout:
                        dash_lines.append(line.rstrip("\r\n"))

                dash_reader = threading.Thread(target=_drain_dash, daemon=True)
                dash_reader.start()

                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                health_ok = False
                deadline = time.monotonic() + 35.0
                while time.monotonic() < deadline:
                    if dash_proc.poll() is not None:
                        break
                    try:
                        with opener.open(f"http://127.0.0.1:{dash_port}/_stcore/health", timeout=2.0) as resp:
                            if resp.status == 200:
                                health_ok = True
                                break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.25)

                self.assertTrue(
                    health_ok,
                    f"Headless Streamlit dashboard did not respond 200 OK on port {dash_port}. Output:\n"
                    + "\n".join(dash_lines),
                )
            finally:
                _terminate_process_tree(dash_proc)

    def test_icons_vsix_electron_config_wheelhouse_and_ci_yaml(self) -> None:
        from build_vsix import build_vsix
        from build_wheelhouse import build_wheelhouse

        # 1. Verify generated icons on disk
        ico_path = REPO_ROOT / "desktop" / "assets" / "icon.ico"
        icns_path = REPO_ROOT / "desktop" / "assets" / "icon.icns"
        vscode_icon = REPO_ROOT / "extensions" / "vscode-second-brain" / "media" / "icon.png"

        self.assertTrue(ico_path.is_file())
        ico_bytes = ico_path.read_bytes()
        reserved, ico_type, ico_count = struct.unpack_from("<HHH", ico_bytes, 0)
        self.assertEqual((reserved, ico_type, ico_count), (0, 1, 7))
        with Image.open(ico_path) as ico_img:
            self.assertEqual(
                sorted(ico_img.info.get("sizes", [])),
                [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
            )

        self.assertTrue(icns_path.is_file())
        self.assertEqual(icns_path.read_bytes()[:4], b"icns")

        self.assertTrue(vscode_icon.is_file())
        with Image.open(vscode_icon) as vs_img:
            self.assertEqual(vs_img.size, (128, 128))

        # 2. Verify desktop/package.json build config & LF-clean package_desktop.sh
        desktop_pkg = json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        build_cfg = desktop_pkg["build"]
        self.assertEqual(build_cfg["win"]["icon"], "assets/icon.ico")
        self.assertEqual(build_cfg["mac"]["icon"], "assets/icon.icns")
        self.assertEqual(build_cfg["linux"]["icon"], "assets/icon.png")
        self.assertNotIn("signAndEditExecutable", build_cfg["win"])
        self.assertIn("lib/**/*", build_cfg["files"])
        extra_tos = {entry["to"] for entry in build_cfg["extraResources"]}
        self.assertIn("backend/second_brain", extra_tos)
        scripts_res = next(entry for entry in build_cfg["extraResources"] if entry["to"] == "backend/scripts")
        self.assertIn("!**/*_tests.py", scripts_res.get("filter", []))
        self.assertIn("!test_all.py", scripts_res.get("filter", []))
        self.assertTrue((REPO_ROOT / "desktop" / "build" / "sign.js").is_file())
        self.assertNotIn(b"\r", (REPO_ROOT / "scripts" / "package_desktop.sh").read_bytes())

        # 3. Verify VSIX builder & OPC [Content_Types].xml completeness
        with tempfile.TemporaryDirectory(prefix="sb_vsix_wheelhouse_") as raw_tmp:
            tmp = Path(raw_tmp)
            vsix_path = build_vsix(out_dir=tmp / "vsix")
            self.assertTrue(vsix_path.is_file())

            with zipfile.ZipFile(vsix_path, "r") as zf:
                vsix_entries = zf.namelist()
                ct_xml = ET.fromstring(zf.read("[Content_Types].xml"))
                manifest_xml = ET.fromstring(zf.read("extension.vsixmanifest"))

            ns_ct = {"ct": "http://schemas.openxmlformats.org/package/2006/content-types"}
            declared_exts = {
                el.attrib["Extension"].lower()
                for el in ct_xml.findall("ct:Default", ns_ct)
                if "Extension" in el.attrib
            }
            declared_overrides = {
                el.attrib["PartName"].lstrip("/")
                for el in ct_xml.findall("ct:Override", ns_ct)
                if "PartName" in el.attrib
            }

            for entry in vsix_entries:
                if entry == "[Content_Types].xml" or entry.endswith("/"):
                    continue
                self.assertFalse(entry.startswith("extension/node_modules/"), f"Forbidden node_modules in VSIX: {entry}")
                self.assertFalse(entry.startswith("extension/test/"), f"Forbidden test/ in VSIX: {entry}")
                ext = Path(entry).suffix.lstrip(".").lower()
                if ext:
                    self.assertIn(ext, declared_exts, f"Missing <Default Extension='{ext}'/> for {entry}")
                else:
                    self.assertIn(entry, declared_overrides, f"Missing <Override/> for extensionless {entry}")

            self.assertIn("extension/media/icon.png", vsix_entries)
            ns_vsx = {"v": "http://schemas.microsoft.com/developer/vsx-schema/2011"}
            icon_el = manifest_xml.find("v:Metadata/v:Icon", ns_vsx)
            self.assertIsNotNone(icon_el)
            assert icon_el is not None
            self.assertEqual(icon_el.text, "extension/media/icon.png")

            # 4. Verify offline wheelhouse installs cleanly with --offline --no-index
            wh_dir = tmp / "wheelhouse"
            wheels = build_wheelhouse(out_dir=wh_dir)
            self.assertGreaterEqual(len(wheels), 8)
            self.assertTrue(any(w.name.startswith("second_brain-") for w in wheels))

            offline_venv = tmp / "offline_venv"
            uv_bin = shutil.which("uv") or "uv"
            py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            subprocess.run(
                [uv_bin, "venv", str(offline_venv), "--python", py_ver],
                check=True,
                capture_output=True,
                text=True,
            )
            off_py, _ = _venv_paths(offline_venv)
            subprocess.run(
                [
                    uv_bin,
                    "pip",
                    "install",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    str(wh_dir),
                    "--python",
                    str(off_py),
                    "second-brain",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        # 5. Verify .github/workflows/ci.yml parses cleanly via yaml.safe_load
        ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        self.assertTrue(ci_path.is_file())
        ci_doc = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
        self.assertIsInstance(ci_doc, dict)
        job = ci_doc["jobs"]["build-and-test"]
        matrix_os = set(job["strategy"]["matrix"]["os"])
        self.assertEqual(matrix_os, {"windows-latest", "macos-latest", "ubuntu-latest"})
        all_runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
        self.assertIn("wheel_packaging_tests", all_runs)
        self.assertIn("build_vsix.py", all_runs)
        self.assertIn("electron-builder --dir", all_runs)
        self.assertIn("npm test", all_runs)

    def test_plugin_skill_prove_leaf_kind_lf_normalization_and_no_bak_pollution(self) -> None:
        import hashlib
        import ide_connector

        ag_root = REPO_ROOT / "plugins" / "antigravity" / "second-brain"
        cc_root = REPO_ROOT / "plugins" / "claude-code" / "second-brain"
        ag_skill = ag_root / "skills" / "second-brain-memory" / "SKILL.md"
        cc_skill = cc_root / "skills" / "second-brain-memory" / "SKILL.md"

        ag_skill_bytes = ag_skill.read_bytes()
        cc_skill_bytes = cc_skill.read_bytes()
        self.assertEqual(ag_skill_bytes, cc_skill_bytes, "Antigravity and Claude Code SKILL.md must stay identical")

        skill_txt = ag_skill_bytes.decode("utf-8")
        self.assertIn(
            "`kind` (`auto` \\| `atom` \\| `playbook` \\| `skill`)",
            skill_txt,
            "SKILL.md prove_leaf documentation must include `skill` kind",
        )
        self.assertIn("executable skill", skill_txt.lower())

        for plugin_root in (ag_root, cc_root):
            for p in sorted(plugin_root.rglob("*")):
                if p.is_file():
                    self.assertNotIn(
                        b"\r",
                        p.read_bytes(),
                        f"Plugin file must be strictly LF-normalized: {p.relative_to(REPO_ROOT)}",
                    )

        gitattributes = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("plugins/** text eol=lf", gitattributes)

        with tempfile.TemporaryDirectory(prefix="sb_ag_plugin_sync_") as td:
            fake_home = Path(td) / "home"
            plugin_dir = fake_home / ".gemini" / "config" / "plugins" / "second-brain"
            plugin_dir.mkdir(parents=True, exist_ok=True)
            # Pre-seed legacy .bak-* clutter inside plugin_dir
            (plugin_dir / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"second-brain": {"command": "old"}}}),
                encoding="utf-8",
            )
            (plugin_dir / "mcp_config.json.bak-20261116-004648").write_text("{}", encoding="utf-8")
            (plugin_dir / "mcp_config.json.bak-20261121-154053").write_text("{}", encoding="utf-8")

            res1 = ide_connector.connect("antigravity", home=fake_home)
            self.assertEqual(res1["status"], "connected")
            # Backup of changed config exists OUTSIDE plugin_dir
            self.assertTrue(res1["backup"] and Path(res1["backup"]).is_file())
            self.assertNotEqual(Path(res1["backup"]).parent.resolve(), plugin_dir.resolve())

            # Re-running connect when unchanged does not create redundant backups or .bak-* in plugin_dir
            res2 = ide_connector.connect("antigravity", home=fake_home)
            self.assertEqual(res2["status"], "connected")
            self.assertIsNone(res2["backup"])

            leftover_baks = list(plugin_dir.glob("*.bak*"))
            self.assertEqual(leftover_baks, [], f"Plugin dir must contain zero .bak* files: {leftover_baks}")

            for rel in ("plugin.json", "README.md", "rules/AGENTS.md", "skills/second-brain-memory/SKILL.md"):
                repo_sha = hashlib.sha256((ag_root / rel).read_bytes()).hexdigest()
                live_sha = hashlib.sha256((plugin_dir / rel).read_bytes()).hexdigest()
                self.assertEqual(repo_sha, live_sha, f"Byte-for-byte drift on {rel}")

            ide_connector.disconnect("antigravity", home=fake_home)
            self.assertFalse(plugin_dir.exists(), "disconnect('antigravity') must cleanly remove empty plugin_dir")

    def test_wheelhouse_no_duplicate_versions_stale_pruning_and_macos_universal_wheels(self) -> None:
        import platform as py_platform
        from unittest import mock
        import build_wheelhouse as bw

        with tempfile.TemporaryDirectory(prefix="sb_wh_univ_") as td:
            wh_dir = Path(td) / "wheelhouse"
            wh_dir.mkdir(parents=True, exist_ok=True)
            # Pre-seed stale/duplicate wheels that must be pruned on rebuild
            (wh_dir / "soupsieve-2.10-py3-none-any.whl").write_bytes(b"stale-soupsieve")
            (wh_dir / "obsolete_pkg-0.1.0-py3-none-any.whl").write_bytes(b"stale-pkg")
            (wh_dir / ".gitignore").write_text("*\n", encoding="utf-8")

            wheels = bw.build_wheelhouse(out_dir=wh_dir)
            on_disk = sorted(p.name for p in wh_dir.glob("*.whl"))
            self.assertEqual(sorted(w.name for w in wheels), on_disk)
            self.assertTrue((wh_dir / ".gitignore").is_file())
            self.assertNotIn("soupsieve-2.10-py3-none-any.whl", on_disk)
            self.assertNotIn("obsolete_pkg-0.1.0-py3-none-any.whl", on_disk)

            # One version per package. macOS host builds keep both arm64 and
            # x86_64 wheels of that version; those are not duplicate versions.
            versions_by_dist: dict[str, set[str]] = {}
            files_by_dist_platform: dict[tuple[str, str], list[str]] = {}
            for fname in on_disk:
                stem = fname[:-4] if fname.endswith(".whl") else fname
                parts = stem.split("-")
                dist = parts[0].lower()
                version = parts[1] if len(parts) > 1 else ""
                platform_tag = parts[-1] if len(parts) >= 5 else stem
                versions_by_dist.setdefault(dist, set()).add(version)
                files_by_dist_platform.setdefault((dist, platform_tag), []).append(fname)
            version_dups = {k: sorted(v) for k, v in versions_by_dist.items() if len(v) > 1}
            platform_dups = {f"{k[0]}:{k[1]}": v for k, v in files_by_dist_platform.items() if len(v) > 1}
            self.assertEqual(version_dups, {}, f"Duplicate wheel versions in host wheelhouse: {version_dups}")
            self.assertEqual(platform_dups, {}, f"Duplicate platform tags in host wheelhouse: {platform_dups}")

            # Verify real uv.lock resolves BOTH arm64 and x86_64 wheels for every core dependency on macOS universal
            mac_plan = bw.resolve_wheelhouse_plan(
                repo_root=REPO_ROOT,
                target_platform="darwin-universal",
                py_version=(3, 11, 14),
            )
            by_pkg: dict[str, list[str]] = {}
            for item in mac_plan:
                by_pkg.setdefault(item["package"], []).append(item["filename"])

            self.assertEqual(len(by_pkg.get("soupsieve", [])), 1, f"Expected single soupsieve on macOS: {by_pkg.get('soupsieve')}")
            for native_pkg in ("cffi", "cryptography", "pydantic-core", "pyyaml"):
                pkg_wheels = by_pkg.get(native_pkg, [])
                self.assertTrue(pkg_wheels, f"Missing {native_pkg} in darwin-universal plan")
                covers_arm64 = any("universal2" in w or "arm64" in w for w in pkg_wheels)
                covers_x64 = any("universal2" in w or "x86_64" in w for w in pkg_wheels)
                self.assertTrue(covers_arm64, f"{native_pkg} missing arm64/universal2 wheel in {pkg_wheels}")
                self.assertTrue(covers_x64, f"{native_pkg} missing x86_64/universal2 wheel in {pkg_wheels}")

            # Verify default 'auto' on an arm64 Darwin host automatically builds universal (arm64 + x86_64) wheels!
            with (
                mock.patch.object(bw.sys, "platform", "darwin"),
                mock.patch.object(py_platform, "machine", return_value="arm64"),
            ):
                resolved_slices = bw.resolve_target_slices("auto")
            self.assertEqual(resolved_slices, [("darwin", "arm64"), ("darwin", "x86_64")])

            mac_wh_dir = Path(td) / "mac_wheelhouse"

            def _stub_fetch(url: str, expected_sha256: str, dest_path: Path) -> Path:
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(f"stub-wheel:{url}:{expected_sha256}".encode("utf-8"))
                return dest_path

            mac_wheels = bw.build_wheelhouse(
                out_dir=mac_wh_dir,
                target_platform="darwin-universal",
                wheel_fetcher=_stub_fetch,
            )
            mac_names = [w.name for w in mac_wheels]
            self.assertTrue(any(n.startswith("cffi-") and "arm64" in n for n in mac_names))
            self.assertTrue(any(n.startswith("cffi-") and "x86_64" in n for n in mac_names))
            self.assertTrue(any(n.startswith("pydantic_core-") and "arm64" in n for n in mac_names))
            self.assertTrue(any(n.startswith("pydantic_core-") and "x86_64" in n for n in mac_names))
            self.assertTrue(
                any(n.startswith("cryptography-") and ("universal2" in n or "x86_64" in n) for n in mac_names),
                f"Expected x86_64-compatible cryptography wheel in {mac_names}",
            )

    def test_packaging_scripts_preserve_mobile_vault_client_and_complete_sha256sums(self) -> None:
        pkg_ps1 = (REPO_ROOT / "scripts" / "package_desktop.ps1").read_text(encoding="utf-8")
        pkg_sh_bytes = (REPO_ROOT / "scripts" / "package_desktop.sh").read_bytes()
        self.assertNotIn(b"\r", pkg_sh_bytes, "package_desktop.sh must be LF-only")
        pkg_sh = pkg_sh_bytes.decode("utf-8")

        # 1. package_desktop.ps1 must NOT delete second-brain-mobile-vault-client.zip or .gitignore
        self.assertIn("second-brain-mobile-vault-client.zip", pkg_ps1)
        self.assertIn(".gitignore", pkg_ps1)
        # SkipSign / SHA256SUMS helper in package_desktop.ps1 must also cover .xpi and .tar.gz
        self.assertIn('".xpi"', pkg_ps1)
        self.assertIn('".tar.gz"', pkg_ps1)

        # 2. package_desktop.sh must build sdist (.tar.gz) + mobile bundles and include *.xpi and *.tar.gz in SHA256SUMS.txt
        self.assertIn("build_distribution_manifests.py --pwa-only", pkg_sh)
        self.assertNotIn("uv build --wheel", pkg_sh)
        self.assertIn("*.xpi", pkg_sh)
        self.assertIn("*.tar.gz", pkg_sh)

        # 3. _collect_homebrew_resources must resolve soupsieve 3.0.4 (not 2.10) and cryptography <49
        from build_distribution_manifests import _collect_homebrew_resources, build_package_manager_manifests
        import hashlib

        brew_res = dict((name, (url, sha)) for name, url, sha in _collect_homebrew_resources())
        self.assertIn("soupsieve", brew_res)
        self.assertIn("3.0.4", brew_res["soupsieve"][0])
        self.assertNotIn("2.10", brew_res["soupsieve"][0])
        self.assertIn("cryptography", brew_res)
        self.assertIn("48.0.1", brew_res["cryptography"][0])

        with tempfile.TemporaryDirectory(prefix="sb_mf_winget_zip_") as td:
            d = Path(td)
            (d / ".gitignore").write_text("*\n", encoding="utf-8")
            exe_b = b"MZ_EXE_100"
            tar_b = b"TAR_GZ_100"
            dmg_b = b"DMG_UNIV_100"
            (d / "Second-Brain-Setup-1.0.0-x64.exe").write_bytes(exe_b)
            (d / "second_brain-1.0.0.tar.gz").write_bytes(tar_b)
            (d / "Second-Brain-1.0.0-universal.dmg").write_bytes(dmg_b)
            (d / "SHA256SUMS.txt").write_text(
                f"{hashlib.sha256(exe_b).hexdigest()}  Second-Brain-Setup-1.0.0-x64.exe\n"
                f"{hashlib.sha256(tar_b).hexdigest()}  second_brain-1.0.0.tar.gz\n"
                f"{hashlib.sha256(dmg_b).hexdigest()}  Second-Brain-1.0.0-universal.dmg\n",
                encoding="utf-8",
            )
            mf = build_package_manager_manifests(
                out_dir=d,
                dist_dir=d,
                version="1.0.0",
                repo_slug="prodofthecentury/second-brain",
                require_checksums=True,
            )
            self.assertTrue(Path(mf["winget_pkgs_zip"]).is_file())
            self.assertTrue(Path(mf["homebrew_formula"]).is_file())
            self.assertTrue(Path(mf["homebrew_cask"]).is_file())

    def test_ci_and_release_workflows_homebrew_formula_winget_and_packaged_smoke(self) -> None:
        ci_txt = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        rel_txt = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        ci_doc = yaml.safe_load(ci_txt)
        rel_doc = yaml.safe_load(rel_txt)
        self.assertIsInstance(ci_doc, dict)
        self.assertIsInstance(rel_doc, dict)

        # ci.yml must run doctor, scale_benchmark_1500, and packaged run_desktop_tests.py --require-packaged
        self.assertIn("doctor", ci_txt)
        self.assertIn("scale_benchmark_1500", ci_txt)
        self.assertIn("run_desktop_tests.py --require-packaged", ci_txt)

        # release.yml must NOT run `rm -rf manifests`, MUST pass `--formula` to publish-homebrew-tap,
        # MUST preserve/upload winget-pkgs, and MUST exclude .gitignore / default.gitignore
        self.assertNotIn("rm -rf manifests", rel_txt)
        self.assertIn("--formula", rel_txt)
        self.assertIn("publish-winget-pkgs", rel_txt)
        self.assertIn("winget-pkgs", rel_txt)
        self.assertIn("run_desktop_tests.py --require-packaged", rel_txt)

    def test_publish_registries_homebrew_formula_winget_pr_github_release_and_verify_assets(self) -> None:
        import hashlib
        import httpx
        import publish_registries as pr

        with tempfile.TemporaryDirectory(prefix="sb_pub_reg_") as td:
            tmp = Path(td)
            cask_file = tmp / "second-brain.rb"
            formula_file = tmp / "Formula-second-brain.rb"
            readme_file = tmp / "README.md"
            cask_file.write_text('cask "second-brain" do\nend\n', encoding="utf-8")
            formula_file.write_text("class SecondBrain < Formula\nend\n", encoding="utf-8")
            readme_file.write_text("# Tap\n", encoding="utf-8")

            pushed_paths: list[str] = []

            def tap_handler(req: httpx.Request) -> httpx.Response:
                if req.method == "GET":
                    return httpx.Response(404, json={"message": "Not Found"})
                if req.method == "PUT":
                    pushed_paths.append(req.url.path)
                    return httpx.Response(201, json={"content": {"path": req.url.path}})
                return httpx.Response(400)

            tap_res = pr.publish_homebrew_tap_via_github_api(
                tap_repo="prodofthecentury/homebrew-second-brain",
                cask_path=cask_file,
                formula_path=formula_file,
                readme_path=readme_file,
                token="ghp_mock_token_for_unit_test_only",
                version="1.0.0",
                transport=httpx.MockTransport(tap_handler),
            )
            self.assertEqual(
                tap_res["updated_files"],
                ["Casks/second-brain.rb", "Formula/second-brain.rb", "README.md"],
            )
            self.assertEqual(len(pushed_paths), 3)

            # Formula-only tap update (without cask) must also succeed
            formula_only_res = pr.publish_homebrew_tap_via_github_api(
                tap_repo="prodofthecentury/homebrew-second-brain",
                cask_path=None,
                formula_path=formula_file,
                token="ghp_mock_token_for_unit_test_only",
                version="1.0.0",
                transport=httpx.MockTransport(tap_handler),
            )
            self.assertEqual(formula_only_res["updated_files"], ["Formula/second-brain.rb"])

            # WinGet multi-file PR submission via MockTransport
            import yaml as _yaml

            wg_ver_dir = tmp / "winget-pkgs" / "manifests" / "s" / "SecondBrain" / "SecondBrain" / "1.0.0"
            wg_ver_dir.mkdir(parents=True, exist_ok=True)
            (wg_ver_dir / "SecondBrain.SecondBrain.yaml").write_text(
                "# yaml-language-server: $schema=https://aka.ms/winget-manifest.version.1.6.0.schema.json\n"
                + _yaml.safe_dump(
                    {
                        "PackageIdentifier": "SecondBrain.SecondBrain",
                        "PackageVersion": "1.0.0",
                        "DefaultLocale": "en-US",
                        "ManifestType": "version",
                        "ManifestVersion": "1.6.0",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (wg_ver_dir / "SecondBrain.SecondBrain.installer.yaml").write_text(
                "# yaml-language-server: $schema=https://aka.ms/winget-manifest.installer.1.6.0.schema.json\n"
                + _yaml.safe_dump(
                    {
                        "PackageIdentifier": "SecondBrain.SecondBrain",
                        "PackageVersion": "1.0.0",
                        "Installers": [
                            {
                                "Architecture": "x64",
                                "InstallerType": "nullsoft",
                                "InstallerUrl": "https://github.com/prodofthecentury/second-brain/releases/download/v1.0.0/SecondBrain.exe",
                                "InstallerSha256": "A" * 64,
                            }
                        ],
                        "ManifestType": "installer",
                        "ManifestVersion": "1.6.0",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (wg_ver_dir / "SecondBrain.SecondBrain.locale.en-US.yaml").write_text(
                "# yaml-language-server: $schema=https://aka.ms/winget-manifest.defaultLocale.1.6.0.schema.json\n"
                + _yaml.safe_dump(
                    {
                        "PackageIdentifier": "SecondBrain.SecondBrain",
                        "PackageVersion": "1.0.0",
                        "PackageLocale": "en-US",
                        "Publisher": "Second Brain",
                        "PackageName": "Second Brain",
                        "License": "MIT",
                        "ShortDescription": "Local-first verifiable AI memory platform.",
                        "ManifestType": "defaultLocale",
                        "ManifestVersion": "1.6.0",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            wg_calls: list[tuple[str, str]] = []

            def wg_handler(req: httpx.Request) -> httpx.Response:
                wg_calls.append((req.method, req.url.path))
                p = req.url.path
                if req.method == "GET" and p == "/user":
                    return httpx.Response(200, json={"login": "sb-bot"})
                if req.method == "POST" and p.endswith("/forks"):
                    return httpx.Response(202, json={"full_name": "sb-bot/winget-pkgs", "default_branch": "master"})
                if req.method == "GET" and p == "/repos/microsoft/winget-pkgs":
                    return httpx.Response(200, json={"default_branch": "master"})
                if req.method == "GET" and "/git/ref/heads/master" in p:
                    return httpx.Response(200, json={"object": {"sha": "a" * 40}})
                if req.method == "GET" and "/git/ref/heads/second-brain-1.0.0" in p:
                    return httpx.Response(404, json={"message": "Not Found"})
                if req.method == "POST" and p.endswith("/git/refs"):
                    return httpx.Response(201, json={"ref": "refs/heads/second-brain-1.0.0"})
                if req.method == "GET" and "/contents/manifests/" in p:
                    return httpx.Response(404, json={"message": "Not Found"})
                if req.method == "PUT" and "/contents/manifests/" in p:
                    return httpx.Response(201, json={"content": {"path": p}})
                if req.method == "POST" and p == "/repos/microsoft/winget-pkgs/pulls":
                    return httpx.Response(201, json={"html_url": "https://github.com/microsoft/winget-pkgs/pull/12345", "number": 12345})
                return httpx.Response(400, json={"path": p})

            wg_res = pr.publish_winget_pkgs_pr_via_github_api(
                manifest_dir=tmp / "winget-pkgs",
                version="1.0.0",
                token="ghp_mock_winget_token_only",
                transport=httpx.MockTransport(wg_handler),
            )
            self.assertEqual(wg_res["pr_number"], 12345)
            self.assertEqual(len(wg_res["committed_files"]), 3)

            # verify_release_assets & publish_github_release_assets (rejects stale wheel and deletes default.gitignore)
            dist_mock = tmp / "dist_mock"
            dist_mock.mkdir(parents=True, exist_ok=True)
            (dist_mock / ".gitignore").write_text("*\n", encoding="utf-8")
            shutil.copy2(self._wheel_path, dist_mock / self._wheel_path.name)
            whl_sha = hashlib.sha256((dist_mock / self._wheel_path.name).read_bytes()).hexdigest()
            (dist_mock / "SHA256SUMS.txt").write_text(f"{whl_sha}  {self._wheel_path.name}\n", encoding="utf-8")

            verified = pr.verify_release_assets(dist_mock, repo_root=REPO_ROOT, skip_git_timestamp_check=True)
            self.assertIn(self._wheel_path.name, verified["assets"])
            self.assertNotIn(".gitignore", verified["assets"])

            deleted_asset_ids: list[int] = []
            uploaded_names: list[str] = []

            def rel_handler(req: httpx.Request) -> httpx.Response:
                p = req.url.path
                if req.method == "GET" and p.endswith("/releases/tags/v1.0.0"):
                    return httpx.Response(
                        200,
                        json={
                            "id": 777,
                            "upload_url": "https://uploads.github.com/repos/prodofthecentury/second-brain/releases/777/assets{?name,label}",
                            "assets": [
                                {"id": 901, "name": "default.gitignore"},
                                {"id": 902, "name": self._wheel_path.name},
                            ],
                        },
                    )
                if req.method == "DELETE" and "/releases/assets/" in p:
                    deleted_asset_ids.append(int(p.rsplit("/", 1)[-1]))
                    return httpx.Response(204)
                if req.method == "POST" and req.url.host == "uploads.github.com":
                    uploaded_names.append(str(req.url.params.get("name") or ""))
                    return httpx.Response(201, json={"name": req.url.params.get("name")})
                return httpx.Response(400, json={"path": p})

            rel_res = pr.publish_github_release_assets(
                repo_slug="prodofthecentury/second-brain",
                tag="v1.0.0",
                dist_dir=dist_mock,
                repo_root=REPO_ROOT,
                token="ghp_mock_release_token_only",
                skip_git_timestamp_check=True,
                transport=httpx.MockTransport(rel_handler),
            )
            self.assertEqual(sorted(deleted_asset_ids), [901, 902])
            self.assertEqual(sorted(uploaded_names), sorted([self._wheel_path.name, "SHA256SUMS.txt"]))
            self.assertEqual(rel_res["deleted_bogus_assets"], ["default.gitignore"])

    def test_track1d_security_audit_and_code_review_regressions(self) -> None:
        import ast
        import hashlib
        from unittest import mock
        import build_distribution_manifests as bdm
        import build_wheelhouse as bw
        import evaluation_dataset as eval_ds
        import ide_connector
        import memory_api
        import publish_registries as pr
        import run_desktop_tests as rdt
        import _hatch_build as hb

        # 1. Wheel excludes scripts/*.ps1/*.sh, _hatch_build.py, mobile fastlane/, gradle-wrapper.jar & stale Capacitor public/
        with zipfile.ZipFile(self._wheel_path, "r") as zf:
            whl_names = set(zf.namelist())
        self.assertNotIn("second_brain/scripts/_hatch_build.py", whl_names)
        self.assertFalse(any(n.startswith("second_brain/scripts/") and not n.endswith(".py") for n in whl_names))
        self.assertFalse(any("/fastlane/" in n for n in whl_names))
        self.assertFalse(any(n.endswith("/gradle-wrapper.jar") for n in whl_names))
        self.assertFalse(any("assets/public/" in n or "App/App/public/" in n for n in whl_names))

        for secret_name in (".env", ".env.prod", "local.properties", "google-services.json", "mcp_config.json", "cert.pfx", "k.pem", "x.bak-123"):
            self.assertTrue(hb._is_forbidden_secret_or_temp(secret_name), f"Expected secret denylist hit: {secret_name}")

        # 2. desktop/package.json retains _hatch_build.py in backend/scripts (for pyproject.toml uv sync fallback)
        #    while filtering plugins secrets and mobile tests/fastlane/secrets
        desktop_pkg = json.loads((REPO_ROOT / "desktop" / "package.json").read_text(encoding="utf-8"))
        res_by_to = {entry["to"]: entry.get("filter", []) for entry in desktop_pkg["build"]["extraResources"]}
        self.assertNotIn("!_hatch_build.py", res_by_to["backend/scripts"])
        self.assertIn("!**/*_tests.py", res_by_to["backend/scripts"])
        self.assertIn("!**/mcp_config.json", res_by_to["backend/plugins"])
        self.assertIn("!**/*.bak*", res_by_to["backend/plugins"])
        self.assertIn("!**/*.pfx", res_by_to["backend/plugins"])
        for excl in ("!**/fastlane/**", "!**/test/**", "!**/androidTest/**", "!**/gradle-wrapper.jar", "!**/*.jks", "!**/.env*", "!**/*.pem", "!**/*.sbvault"):
            self.assertIn(excl, res_by_to["backend/mobile"])

        # macOS root compatibility symlink (/var -> private/var) allowed on darwin; arbitrary symlinks still rejected
        import config as cfg
        import inbox_drop
        import mobile_vault_client as mvc

        with (
            mock.patch.object(bw.sys, "platform", "darwin"),
            mock.patch.object(bw.os, "readlink", return_value="private/var"),
        ):
            self.assertTrue(bw._is_immutable_darwin_root_symlink(PurePosixPath("/var")))
            self.assertFalse(bw._is_immutable_darwin_root_symlink(PurePosixPath("/Users/evil")))
        with (
            mock.patch.object(cfg.sys, "platform", "darwin"),
            mock.patch.object(cfg.os, "readlink", return_value="private/tmp"),
        ):
            self.assertTrue(ide_connector._is_immutable_darwin_root_symlink(PurePosixPath("/tmp")))
            self.assertFalse(ide_connector._is_immutable_darwin_root_symlink(PurePosixPath("/tmp/evil")))
        # Keychain fallback tests report linux while still walking the real /var symlink.
        with (
            mock.patch.object(cfg.sys, "platform", "linux"),
            mock.patch.object(cfg.os, "readlink", return_value="private/var"),
        ):
            self.assertTrue(cfg._is_immutable_darwin_root_symlink("/var"))
            self.assertFalse(cfg._is_immutable_darwin_root_symlink("/var/folders"))

        # End-to-end macOS /var/folders/x traversal allowed for build_distribution_manifests, inbox_drop, mobile_vault_client
        with (
            mock.patch.object(cfg.sys, "platform", "darwin"),
            mock.patch.object(cfg.os, "readlink", return_value="private/var"),
            mock.patch.object(bdm.os.path, "lexists", side_effect=lambda p: str(p) in {"/var", "/var/folders", "/var/folders/x"}),
            mock.patch.object(bdm, "_is_reparse_or_link", side_effect=lambda p: str(p) == "/var"),
        ):
            bdm._assert_no_symlink_ancestors(PurePosixPath("/var/folders/x"))
        with (
            mock.patch.object(cfg.sys, "platform", "darwin"),
            mock.patch.object(cfg.os, "readlink", return_value="private/var"),
            mock.patch.object(bdm.os.path, "lexists", side_effect=lambda p: str(p) in {"/var", "/var/folders", "/var/folders/x"}),
            mock.patch.object(bdm, "_is_reparse_or_link", side_effect=lambda p: str(p) in {"/var", "/var/folders"}),
        ):
            with self.assertRaises(PermissionError):
                bdm._assert_no_symlink_ancestors(PurePosixPath("/var/folders/x"))
        with (
            mock.patch.object(cfg.sys, "platform", "darwin"),
            mock.patch.object(cfg.os, "readlink", return_value="private/var"),
            mock.patch.object(inbox_drop.os.path, "lexists", side_effect=lambda p: str(p) in {"/var", "/var/folders", "/var/folders/x"}),
            mock.patch.object(inbox_drop, "_is_reparse_or_link", side_effect=lambda p: str(p) == "/var"),
        ):
            inbox_drop._assert_no_symlinks(PurePosixPath("/var/folders/x"))
        with (
            mock.patch.object(cfg.sys, "platform", "darwin"),
            mock.patch.object(cfg.os, "readlink", return_value="private/var"),
            mock.patch.object(mvc, "Path", PurePosixPath),
            mock.patch.object(mvc.os.path, "abspath", side_effect=lambda s: s),
            mock.patch.object(mvc.os.path, "lexists", side_effect=lambda p: str(p) in {"/var", "/var/folders", "/var/folders/x"}),
            mock.patch.object(mvc, "_is_reparse_or_link", side_effect=lambda p: str(p) == "/var"),
        ):
            mvc._assert_no_symlinks(PurePosixPath("/var/folders/x"))
        with (
            mock.patch.object(cfg.sys, "platform", "darwin"),
            mock.patch.object(cfg.os, "readlink", return_value="private/var"),
            mock.patch.object(mvc, "Path", PurePosixPath),
            mock.patch.object(mvc.os.path, "abspath", side_effect=lambda s: s),
            mock.patch.object(mvc.os.path, "lexists", side_effect=lambda p: str(p) in {"/var", "/var/folders", "/var/folders/x"}),
            mock.patch.object(mvc, "_is_reparse_or_link", side_effect=lambda p: str(p) in {"/var", "/var/folders"}),
        ):
            with self.assertRaises(mvc.MobileVaultSecurityError):
                mvc._assert_no_symlinks(PurePosixPath("/var/folders/x"))

        # desktop/main.js writes .sb-bootstrap-complete marker so partial step-1 venv never bricks subsequent launches
        main_js_txt = (REPO_ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        self.assertIn(".sb-bootstrap-complete", main_js_txt)

        # 3. build_wheelhouse security & compatibility tie-breakers
        with self.assertRaises(PermissionError):
            bw._validate_wheel_filename("../../escape-1.0.0-py3-none-any.whl")
        with self.assertRaises(PermissionError):
            bw._validate_wheel_url("https://evil.example/pkg-1.0.0-py3-none-any.whl")
        with self.assertRaises(PermissionError):
            bw._validate_wheel_url("http://files.pythonhosted.org/pkg-1.0.0-py3-none-any.whl")
        with self.assertRaises(PermissionError):
            bw._validate_wheel_url("https://files.pythonhosted.org:8443/pkg-1.0.0-py3-none-any.whl")
        with self.assertRaises(PermissionError):
            bw._validate_wheel_url("https://user:pass@files.pythonhosted.org/pkg-1.0.0-py3-none-any.whl")

        # implementation_version marker evaluates without error
        self.assertTrue(
            bw.evaluate_pep508_marker(
                "implementation_version >= '3.11.0'",
                target_os="linux",
                target_arch="x86_64",
                py_version=(3, 11, 14),
            )
        )
        # Linux manylinux tie-breaker picks widest glibc compatibility (not manylinux_2_34)
        linux_plan = bw.resolve_wheelhouse_plan(
            repo_root=REPO_ROOT,
            target_platform="linux-x86_64",
            py_version=(3, 11, 14),
        )
        crypto_linux = [item["filename"] for item in linux_plan if item["package"] == "cryptography"]
        self.assertEqual(len(crypto_linux), 1)
        self.assertNotIn("manylinux_2_34", crypto_linux[0], f"Should prefer wider manylinux over 2_34: {crypto_linux}")
        # cp311-abi3 beats cp39-abi3 numerically on Python 3.12+
        self.assertGreater(
            bw._wheel_candidate_sort_key(40, "cp311-abi3-manylinux_2_28_x86_64"),
            bw._wheel_candidate_sort_key(40, "cp39-abi3-manylinux_2_28_x86_64"),
        )

        # Cache RECORD tamper + escape rejection
        with tempfile.TemporaryDirectory(prefix="sb_wh_sec_") as td:
            tmp = Path(td)
            fake_cache = tmp / "uv_cache"
            arch = fake_cache / "archive-v0" / "abc"
            di = arch / "demo-1.0.0.dist-info"
            di.mkdir(parents=True, exist_ok=True)
            (arch / "demo.py").write_bytes(b"print('tampered')\n")
            (di / "RECORD").write_text("demo.py,sha256=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,10\ndemo-1.0.0.dist-info/RECORD,,\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                bw._repack_uv_archive_to_wheel(arch, tmp / "demo-1.0.0-py3-none-any.whl", uv_cache_root=fake_cache)
            outside = tmp / "outside_arch"
            outside.mkdir()
            with self.assertRaises(PermissionError):
                bw._repack_uv_archive_to_wheel(outside, tmp / "demo-1.0.0-py3-none-any.whl", uv_cache_root=fake_cache)

            # Cold UV_CACHE_DIR test of exact Desktop offline wheelhouse bootstrap steps!
            cold_wh = tmp / "cold_wh"
            bw.build_wheelhouse(out_dir=cold_wh)
            cold_venv = tmp / "cold_venv"
            cold_py, _ = _venv_paths(cold_venv)
            uv_bin = shutil.which("uv") or "uv"
            cold_env = {**os.environ, "UV_CACHE_DIR": str(tmp / "empty_uv_cache")}
            subprocess.run(
                [uv_bin, "venv", str(cold_venv), "--python", sys.executable, "--clear", "--offline"],
                env=cold_env,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    uv_bin,
                    "pip",
                    "install",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    str(cold_wh),
                    "--python",
                    str(cold_py),
                    "second-brain",
                ],
                env=cold_env,
                check=True,
                capture_output=True,
                text=True,
            )
            probe = subprocess.run(
                [str(cold_py), "-c", "import second_brain, cryptography; print(second_brain.__version__)"],
                env=cold_env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.stdout.strip(), "1.0.0")

        # 4. Exact parameter parity between SKILL.md and scripts/mcp_server.py inputSchema across every TOOL_NAMES entry
        mcp_src = (REPO_ROOT / "scripts" / "mcp_server.py").read_text(encoding="utf-8")
        mcp_tree = ast.parse(mcp_src)
        mcp_schemas: dict[str, set[str]] = {}
        for node in ast.walk(mcp_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Tool":
                tname = None
                props: set[str] = set()
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        tname = kw.value.value
                    elif kw.arg == "inputSchema" and isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if isinstance(k, ast.Constant) and k.value == "properties" and isinstance(v, ast.Dict):
                                for pk in v.keys:
                                    if isinstance(pk, ast.Constant) and isinstance(pk.value, str):
                                        props.add(pk.value)
                if tname:
                    mcp_schemas[tname] = props
        self.assertEqual(len(mcp_schemas), 35)
        skill_lines = (
            REPO_ROOT / "plugins" / "antigravity" / "second-brain" / "skills" / "second-brain-memory" / "SKILL.md"
        ).read_text(encoding="utf-8").splitlines()
        doc_tools: set[str] = set()
        for ln in skill_lines:
            if not ln.startswith("| `"):
                continue
            cols = [c.strip() for c in re.split(r"(?<!\\)\|", ln)[1:-1]]
            if len(cols) < 4:
                continue
            tname = cols[0].strip("`")
            self.assertIn(tname, mcp_schemas, f"Unknown MCP tool in SKILL.md: {tname}")
            doc_tools.add(tname)
            params_col = cols[2]
            if params_col == "_(none)_":
                self.assertEqual(mcp_schemas[tname], set(), f"{tname} has parameters in mcp_server.py but SKILL.md says none")
            else:
                # Every parameter name before '(' or ',' must exist in mcp_schemas[tname]
                cleaned = re.sub(r"\([^)]*\)", "", params_col)
                for tok in re.findall(r"`([a-z][a-z0-9_]*)`", cleaned):
                    self.assertIn(tok, mcp_schemas[tname], f"Drifted parameter {tok!r} for tool {tname!r} in SKILL.md")
        self.assertEqual(doc_tools, set(mcp_schemas))

        # Claude Code .mcp.json uses bootstrap_runtime() on wheel installs & chain_ok(c.parent) for POSIX venv symlinks
        cc_mcp = (REPO_ROOT / "plugins" / "claude-code" / "second-brain" / ".mcp.json").read_text(encoding="utf-8")
        self.assertIn("bootstrap_runtime", cc_mcp)
        self.assertIn("chain_ok(c.parent)", cc_mcp)

        # 5. ide_connector disconnect redacts MCP token + prunes backups <= 3; custom host rejects empty/relative paths
        with tempfile.TemporaryDirectory(prefix="sb_ide_sec_") as td:
            fhome = Path(td) / "home"
            for _ in range(5):
                ide_connector.connect("antigravity", home=fhome)
                ide_connector.disconnect("antigravity", home=fhome)
            bdir = fhome / ".gemini" / "config" / ".second-brain-backups"
            baks = sorted(bdir.glob("antigravity-mcp_config.json.bak-*"))
            self.assertLessEqual(len(baks), 3)
            self.assertTrue(baks)
            self.assertIn("<redacted-on-disconnect>", baks[-1].read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                ide_connector.register_custom_mcp_host("my-custom", label="Bad", config_path="", state_dir=Path(td) / "st")
            with self.assertRaises(ValueError):
                ide_connector.register_custom_mcp_host("my-custom", label="Bad", config_path="rel/mcp.json", state_dir=Path(td) / "st")

        # 6. publish_registries strict hygiene & SSRF hardening
        with self.assertRaises(pr.RegistryPublishError):
            pr._validate_repo_slug("./.")
        with tempfile.TemporaryDirectory(prefix="sb_pr_sec_") as td:
            d = Path(td)
            whl = d / self._wheel_path.name
            shutil.copy2(self._wheel_path, whl)
            wsha = hashlib.sha256(whl.read_bytes()).hexdigest()
            (d / "SHA256SUMS.txt").write_text(f"{wsha}  {whl.name}\n", encoding="utf-8")
            (d / "stray_secret.txt").write_text("oops", encoding="utf-8")
            with self.assertRaises(pr.RegistryPublishError):
                pr.verify_release_assets(d, repo_root=REPO_ROOT, skip_git_timestamp_check=True)
            (d / "stray_secret.txt").unlink()
            # Reject all-zero digest & duplicate entries
            (d / "SHA256SUMS.txt").write_text(f"{'0'*64}  {whl.name}\n", encoding="utf-8")
            with self.assertRaises(pr.RegistryPublishError):
                pr.verify_release_assets(d, repo_root=REPO_ROOT, skip_git_timestamp_check=True)
            (d / "SHA256SUMS.txt").write_text(f"{wsha}  {whl.name}\n{wsha}  {whl.name}\n", encoding="utf-8")
            with self.assertRaises(pr.RegistryPublishError):
                pr.verify_release_assets(d, repo_root=REPO_ROOT, skip_git_timestamp_check=True)
            # publish_pypi rejects cleartext non-loopback http://
            with mock.patch.dict(os.environ, {"PYPI_API_TOKEN": "pypi-mock"}, clear=False):
                with self.assertRaises(pr.RegistryPublishError):
                    pr.publish_pypi(d, dry_run=True, repository_url="http://upload.pypi.org/legacy/")

        # 7. evaluation_dataset code-only guard, MemoryLoopbackHTTPServer backlog, run_desktop_tests Linux --require-packaged, default repo slug & Homebrew formula deps
        import inspect
        self.assertNotIn("check_vault_files", inspect.signature(eval_ds.load_dataset).parameters)
        self.assertGreaterEqual(memory_api.MemoryLoopbackHTTPServer.request_queue_size, 128)
        with tempfile.TemporaryDirectory(prefix="sb_eval_ci_") as td:
            troot = Path(td)
            empty_vault = troot / "empty_brain"
            (empty_vault / "2-atoms").mkdir(parents=True)
            self.assertEqual(eval_ds.main(["--allow-code-only", "--brain-dir", str(empty_vault)]), 0)
            # Vault with decision-record.jsonl refuses code-only downgrade even if 2-atoms is empty
            live_marker_vault = troot / "live_brain"
            (live_marker_vault / "2-atoms").mkdir(parents=True)
            (live_marker_vault / "0-raw" / "archive").mkdir(parents=True)
            (live_marker_vault / "0-raw" / "archive" / "decision-record.jsonl").write_text("{}\n", encoding="utf-8")
            self.assertEqual(eval_ds.main(["--allow-code-only", "--brain-dir", str(live_marker_vault)]), 1)

        with (
            mock.patch.object(rdt.sys, "platform", "linux"),
            mock.patch.dict(os.environ, {"DISPLAY": ""}, clear=False),
            mock.patch.object(rdt.shutil, "which", return_value=None),
        ):
            ok_req, _msg_req = rdt.run_packaged_smoke_test(Path("/nonexistent/bin"), require_packaged=True)
            self.assertFalse(ok_req)

        with mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": ""}, clear=False):
            self.assertEqual(bdm._resolve_default_repo_slug(), "prodofthecentury/second-brain")
        formula_sample = bdm._render_homebrew_formula(
            slug="prodofthecentury/second-brain",
            version="1.0.0",
            base_download="https://github.com/prodofthecentury/second-brain/releases/download/v1.0.0",
            artifact_name="second_brain-1.0.0.tar.gz",
            artifact_sha="a" * 64,
        )
        self.assertNotIn('depends_on "hatchling"', formula_sample)
        for dep_line in (
            'depends_on "maturin" => :build',
            'depends_on "pkgconf" => :build',
            'depends_on "rust" => :build',
            'depends_on "openssl@3"',
            'depends_on "libffi"',
            'resource "hatchling" do',
            'resource "flit-core" do',
        ):
            self.assertIn(dep_line, formula_sample)

        # 8. CI & Release workflow security & parse-validity invariants
        ci_txt = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        rel_txt = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        rel_doc = yaml.safe_load(rel_txt)
        for job_id, job_cfg in (rel_doc.get("jobs") or {}).items():
            job_if = str(job_cfg.get("if") or "")
            self.assertNotIn("secrets.", job_if, f"GitHub Actions forbids secrets.* in job-level if: on {job_id}")
        self.assertIn("github.event_name != 'pull_request'", ci_txt)
        self.assertNotIn("npm install --no-package-lock", ci_txt)
        self.assertIn("--exclude desktop_ui_tests vscode_extension_tests wheel_packaging_tests", ci_txt)
        self.assertIn("SB_RELEASE_LINUX_RUNNER", rel_txt)
        self.assertIn("!inputs.draft && inputs.publish_registries", rel_txt)
        self.assertNotIn("-PfxPassword", rel_txt)
        self.assertNotIn('--pat "$VSCE_PAT"', rel_txt)
        self.assertNotIn('-p "$OVSX_PAT"', rel_txt)
        self.assertIn("dist/latest.yml", rel_txt)
        self.assertIn("dist/latest-mac.yml", rel_txt)
        self.assertIn("dist/latest-linux.yml", rel_txt)
        self.assertIn("winget-pkgs/manifests/s/SecondBrain/SecondBrain/", rel_txt)
        self.assertIn("LC_ALL=C sort -z", rel_txt)


if __name__ == "__main__":
    unittest.main()
