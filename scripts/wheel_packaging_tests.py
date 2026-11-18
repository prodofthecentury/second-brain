"""Clean-wheel namespace, isolated temp-venv CLI/API/Dashboard, VSIX, icon, wheelhouse, and CI tests.

Run:
    uv run --frozen python -m unittest scripts.wheel_packaging_tests -v
"""

from __future__ import annotations

import json
import os
from pathlib import Path
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
            if n.startswith(("scripts/", "dashboard/", "plugins/", ".streamlit/"))
        ]
        self.assertEqual(
            polluted,
            [],
            f"Wheel must NOT pollute top-level site-packages namespace; found: {polluted[:10]}",
        )
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


if __name__ == "__main__":
    unittest.main()
