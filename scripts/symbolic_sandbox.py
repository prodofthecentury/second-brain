# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Isolated Zero-Network, AST-Jailed Python Symbolic & Numeric Sandbox (Epic 2E.2).

Executes quantitative PAL / PoT / ToRA code blocks safely:
- AST validation BEFORE spawning subprocess: allows only pure math/symbolic modules
  (math, cmath, fractions, decimal, statistics, itertools, collections, re, json,
  optional sympy/z3).
- Hard blocks filesystem I/O, network sockets, subprocess, os, sys, ctypes,
  eval/exec/compile, and dunder reflection escape chains (`__subclasses__`, etc.).
- Runs in a separate subprocess with strict wall-clock timeout and bounded stdout.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("symbolic_sandbox", "scripts.symbolic_sandbox"):
    sys.modules.setdefault("symbolic_sandbox", sys.modules[__name__])
    sys.modules.setdefault("scripts.symbolic_sandbox", sys.modules[__name__])

ALLOWED_SANDBOX_MODULES = frozenset(
    {
        "math",
        "cmath",
        "fractions",
        "decimal",
        "statistics",
        "itertools",
        "functools",
        "collections",
        "re",
        "json",
        "sympy",
        "z3",
    }
)

# Safe internal modules allowed through sys.meta_path only for transitive stdlib/sympy imports
# (never directly importable by user code through _user_import).
_META_PATH_ALLOWED_MODULES = ALLOWED_SANDBOX_MODULES | frozenset(
    {
        "mpmath",
        "numbers",
        "operator",
        "_operator",
        "abc",
        "warnings",
        "enum",
        "bisect",
        "_bisect",
        "copyreg",
        "encodings",
        "unicodedata",
        "string",
        "heapq",
        "keyword",
        "reprlib",
        "random",
        "_random",
        "_sha512",
        "_sre",
        "_decimal",
        "_json",
        "_statistics",
        "collections",
        "weakref",
        "copy",
        "typing",
    }
)

FORBIDDEN_BUILTINS_OR_CALLS = frozenset(
    {
        "open",
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "input",
        "breakpoint",
        "exit",
        "quit",
        "help",
        "memoryview",
        "object",
        "type",
    }
)

FORBIDDEN_ATTRS = FORBIDDEN_BUILTINS_OR_CALLS | frozenset(
    {
        # Generator / coroutine / async-generator / traceback / frame introspection
        "gi_frame",
        "gi_code",
        "gi_yieldfrom",
        "gi_running",
        "cr_frame",
        "cr_code",
        "cr_await",
        "cr_running",
        "ag_frame",
        "ag_code",
        "ag_await",
        "ag_running",
        "tb_frame",
        "tb_next",
        "tb_lasti",
        "tb_lineno",
        "f_back",
        "f_globals",
        "f_locals",
        "f_builtins",
        "f_code",
        "f_trace",
        "co_code",
        "co_consts",
        "co_names",
        "co_freevars",
        # Operator / stdlib internal attribute traversal & frame/closure escapes
        "operator",
        "attrgetter",
        "methodcaller",
        "itemgetter",
        "enum",
        "bltns",
        "copyreg",
        "decoder",
        "encoder",
        "scanner",
        "meta_path",
        "cell_contents",
        "closure",
        "code",
        "frame",
        "current_frames",
        "getframe",
        # Format-string attribute traversal
        "format",
        "format_map",
        # Module / runtime / builtin escape handles
        "sys",
        "modules",
        "builtins",
        "os",
        "io",
        "subprocess",
        "socket",
        "ctypes",
        "importlib",
        "pathlib",
        "shutil",
        "tempfile",
        "inspect",
        "traceback",
        "gc",
        "types",
        "codecs",
        "threading",
        "multiprocessing",
        "signal",
        "resource",
        "webbrowser",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "winreg",
        "msvcrt",
        "pickle",
        "marshal",
        "runpy",
        "pkgutil",
        "zipimport",
        "linecache",
        "sysconfig",
        "site",
        "nt",
        "posix",
        # Dynamic introspection / reflection builtins & dunder-stripped names
        "import_module",
        "load_module",
        "find_spec",
        "mro",
        "subclasses",
        "bases",
        "class",
        "dict",
        "init",
        "new",
        "reduce",
        "reduce_ex",
        "getattribute",
        "import",
        # Process / filesystem / IO methods
        "FileIO",
        "Popen",
        "system",
        "popen",
        "startfile",
        "spawn",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "remove",
        "unlink",
        "rmdir",
        "mkdir",
        "makedirs",
        "rename",
        "replace",
        "chmod",
        "chdir",
        "symlink",
        "link",
        "truncate",
        "write_bytes",
        "write_text",
        "read_bytes",
        "read_text",
    }
)

_SAFE_BUILTIN_NAMES = (
    "None",
    "False",
    "True",
    "Ellipsis",
    "NotImplemented",
    "abs",
    "all",
    "any",
    "ascii",
    "bin",
    "bool",
    "bytearray",
    "bytes",
    "callable",
    "chr",
    "complex",
    "dict",
    "divmod",
    "enumerate",
    "filter",
    "float",
    "frozenset",
    "hash",
    "hex",
    "id",
    "int",
    "isinstance",
    "issubclass",
    "iter",
    "len",
    "list",
    "map",
    "max",
    "min",
    "next",
    "oct",
    "ord",
    "pow",
    "print",
    "range",
    "repr",
    "reversed",
    "round",
    "set",
    "slice",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "__build_class__",
    "BaseException",
    "Exception",
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "EOFError",
    "FloatingPointError",
    "ImportError",
    "IndexError",
    "KeyError",
    "LookupError",
    "MemoryError",
    "ModuleNotFoundError",
    "NameError",
    "NotImplementedError",
    "OverflowError",
    "PermissionError",
    "RecursionError",
    "RuntimeError",
    "StopIteration",
    "SyntaxError",
    "TimeoutError",
    "TypeError",
    "UnboundLocalError",
    "ValueError",
    "ZeroDivisionError",
)

MAX_CODE_CHARS = 16_000
MAX_STDOUT_CHARS = 8_000
MAX_PIPE_CAPTURE_BYTES = MAX_STDOUT_CHARS * 4
_SANDBOX_MEMORY_LIMIT_BYTES = 512 * 1024 * 1024


class SandboxSecurityError(PermissionError):
    """Raised when candidate Python code attempts a forbidden import, call, or reflection escape."""


def _is_forbidden_alias_name(name: str | None) -> bool:
    if not name:
        return False
    return (
        name in FORBIDDEN_BUILTINS_OR_CALLS
        or name in FORBIDDEN_ATTRS
        or (name.startswith("__") and name.endswith("__"))
    )


def validate_sandbox_ast(code: str) -> ast.Module:
    """Strictly validate candidate Python code AST before subprocess execution."""
    raw = str(code or "")
    if not raw.strip():
        raise ValueError("Sandbox code must be non-empty.")
    if len(raw) > MAX_CODE_CHARS:
        raise SandboxSecurityError(f"Sandbox code exceeds maximum length ({MAX_CODE_CHARS} chars).")
    try:
        tree = ast.parse(raw, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"Syntax error in sandbox code: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                raw_name = (alias.name or "").strip()
                parts = raw_name.split(".")
                top = parts[0]
                if not top or top not in ALLOWED_SANDBOX_MODULES:
                    raise SandboxSecurityError(f"Disallowed module import in sandbox: {alias.name!r}")
                for sub in parts[1:]:
                    if (
                        not sub
                        or sub.startswith("_")
                        or sub in FORBIDDEN_ATTRS
                        or sub in FORBIDDEN_BUILTINS_OR_CALLS
                    ):
                        raise SandboxSecurityError(f"Disallowed submodule import in sandbox: {alias.name!r}")
                if _is_forbidden_alias_name(alias.asname):
                    raise SandboxSecurityError(f"Disallowed import alias in sandbox: {alias.asname!r}")
        elif isinstance(node, ast.ImportFrom):
            raw_mod = (node.module or "").strip()
            parts = raw_mod.split(".")
            top = parts[0]
            if node.level != 0 or not top or top not in ALLOWED_SANDBOX_MODULES:
                raise SandboxSecurityError(f"Disallowed relative or external import in sandbox: {node.module!r}")
            for sub in parts[1:]:
                if (
                    not sub
                    or sub.startswith("_")
                    or sub in FORBIDDEN_ATTRS
                    or sub in FORBIDDEN_BUILTINS_OR_CALLS
                ):
                    raise SandboxSecurityError(f"Disallowed submodule import in sandbox: {node.module!r}")
            for alias in node.names:
                imp_name = (alias.name or "").strip()
                if (
                    not imp_name
                    or imp_name == "*"
                    or imp_name.startswith("_")
                    or imp_name in FORBIDDEN_ATTRS
                    or imp_name in FORBIDDEN_BUILTINS_OR_CALLS
                ):
                    raise SandboxSecurityError(
                        f"Disallowed symbol import in sandbox: from {node.module} import {alias.name!r}"
                    )
                if _is_forbidden_alias_name(alias.asname):
                    raise SandboxSecurityError(f"Disallowed import alias in sandbox: {alias.asname!r}")
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_BUILTINS_OR_CALLS or (node.id.startswith("__") and node.id.endswith("__")):
                if node.id != "__name__":
                    raise SandboxSecurityError(f"Disallowed identifier or builtin in sandbox: {node.id!r}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in FORBIDDEN_ATTRS:
                raise SandboxSecurityError(f"Disallowed reflection/IO attribute access in sandbox: {node.attr!r}")
    return tree


def _requested_optional_modules(tree: ast.Module) -> list[str]:
    optional = {"sympy", "z3"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                raw_name = (alias.name or "").strip()
                top = raw_name.split(".", 1)[0]
                if top in optional:
                    found.add(top)
                    if raw_name != top:
                        found.add(raw_name)
        elif isinstance(node, ast.ImportFrom):
            raw_mod = (node.module or "").strip()
            top = raw_mod.split(".", 1)[0]
            if top in optional:
                found.add(top)
                if raw_mod != top:
                    found.add(raw_mod)
    return sorted(found)


def _build_clean_subprocess_env(tmp_dir: str) -> dict[str, str]:
    """Build a minimal secret-free environment stripping all tokens/keys (API keys, BRAIN_*, AWS_*, etc.)."""
    env: dict[str, str] = {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TEMP": tmp_dir,
        "TMP": tmp_dir,
    }
    for key in ("PATH", "SYSTEMROOT"):
        val = os.environ.get(key)
        if val:
            env[key] = val
    return env


def _make_posix_preexec(timeout_seconds: float):
    def _preexec() -> None:
        try:
            os.setsid()
        except Exception:
            pass
        try:
            import resource as _res

            if hasattr(_res, "RLIMIT_CORE"):
                _res.setrlimit(_res.RLIMIT_CORE, (0, 0))
            if hasattr(_res, "RLIMIT_CPU"):
                cpu_s = max(2, int(math.ceil(timeout_seconds)) + 1)
                _res.setrlimit(_res.RLIMIT_CPU, (cpu_s, cpu_s))
            if hasattr(_res, "RLIMIT_AS"):
                _res.setrlimit(_res.RLIMIT_AS, (_SANDBOX_MEMORY_LIMIT_BYTES, _SANDBOX_MEMORY_LIMIT_BYTES))
        except Exception:
            pass

    return _preexec


class _Win32SandboxJob:
    """Fail-safe Win32 Job Object enforcing active-process cap, memory limit, and kill-on-close."""

    def __init__(self, *, memory_limit_bytes: int = _SANDBOX_MEMORY_LIMIT_BYTES) -> None:
        self._handle: int = 0
        self._memory_limit = int(memory_limit_bytes)

    def attach_and_resume(self, proc: subprocess.Popen[bytes]) -> bool:
        if os.name != "nt":
            return False
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.restype = ctypes.c_long

        proc_handle = int(getattr(proc, "_handle", 0) or 0)
        if not proc_handle:
            return False

        attached = False
        try:
            h_job = kernel32.CreateJobObjectW(None, None)
            if h_job:
                self._handle = int(h_job)
                base_exe = getattr(sys, "_base_executable", None) or sys.executable
                uses_trampoline = os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(
                    os.path.abspath(base_exe)
                )
                ext = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                # ACTIVE_PROCESS(0x8) | PROCESS_MEMORY(0x100) | JOB_MEMORY(0x200) | DIE_ON_UNHANDLED_EXCEPTION(0x400) | KILL_ON_JOB_CLOSE(0x2000)
                ext.BasicLimitInformation.LimitFlags = 0x0008 | 0x0100 | 0x0200 | 0x0400 | 0x2000
                ext.BasicLimitInformation.ActiveProcessLimit = 2 if uses_trampoline else 1
                ext.ProcessMemoryLimit = self._memory_limit
                ext.JobMemoryLimit = self._memory_limit
                if kernel32.SetInformationJobObject(
                    h_job, 9, ctypes.byref(ext), ctypes.sizeof(ext)
                ) and kernel32.AssignProcessToJobObject(h_job, wintypes.HANDLE(proc_handle)):
                    attached = True
                else:
                    self.close()
        except Exception:
            self.close()
        finally:
            # Always resume the suspended child process (fail-safe on restricted Windows hosts)
            ntdll.NtResumeProcess(wintypes.HANDLE(proc_handle))
        return attached

    def close(self) -> None:
        if os.name == "nt" and self._handle:
            try:
                import ctypes
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel32.CloseHandle.restype = wintypes.BOOL
                kernel32.CloseHandle(wintypes.HANDLE(self._handle))
            except Exception:
                pass
            finally:
                self._handle = 0


def _terminate_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            proc.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _read_pipe_capped(stream: Any, limit_bytes: int, sink: bytearray) -> None:
    """Drain a subprocess pipe while storing at most `limit_bytes` in RAM."""
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            remaining = limit_bytes - len(sink)
            if remaining > 0:
                sink.extend(chunk[:remaining])
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _build_runner_harness(code: str, preimport_optional: list[str]) -> str:
    """Build a bootstrap-isolated runner leaving ZERO harness internals in any frame or __main__."""
    deny_exact_lit = (
        "{"
        + ", ".join(
            repr(evt)
            for evt in (
                "os.system",
                "os.popen",
                "os.startfile",
                "os.posix_spawn",
                "os.kill",
                "os.remove",
                "os.unlink",
                "os.rename",
                "os.replace",
                "os.chmod",
                "os.mkdir",
                "os.rmdir",
                "os.symlink",
                "os.link",
                "os.truncate",
                "os.chdir",
                "os.listdir",
                "os.scandir",
                "gc.get_objects",
                "gc.get_referrers",
                "pickle.find_class",
                "sys._current_frames",
                "sys._getframe",
            )
        )
        + "}"
    )
    deny_prefixes_lit = repr(
        (
            "socket.",
            "subprocess.",
            "ctypes.",
            "winreg.",
            "_winapi.",
            "webbrowser.",
            "urllib.",
            "http.",
            "ftplib.",
            "smtplib.",
            "os.spawn",
            "os.exec",
            "os.fork",
            "sqlite3.",
            "shutil.",
            "mmap.",
        )
    )
    return (
        "def _sb_bootstrap():\n"
        "    import atexit as _atexit\n"
        "    import bisect as _bisect\n"
        "    import builtins as _b\n"
        "    import cmath as _cmath\n"
        "    import collections as _collections\n"
        "    import collections.abc as _collections_abc\n"
        "    import decimal as _decimal\n"
        "    import fractions as _fractions\n"
        "    import functools as _functools\n"
        "    import heapq as _heapq\n"
        "    import io as _io\n"
        "    import itertools as _itertools\n"
        "    import json as _json\n"
        "    import math as _math\n"
        "    import numbers as _numbers\n"
        "    import operator as _operator\n"
        "    import os as _os\n"
        "    import re as _re\n"
        "    import statistics as _statistics\n"
        "    import sys as _sys\n"
        "    _re.compile(r'\\d+')\n"
        f"    _PREIMPORT_OPT = {preimport_optional!r}\n"
        f"    _USER_ALLOWED = frozenset({sorted(ALLOWED_SANDBOX_MODULES)!r})\n"
        f"    _META_ALLOWED = frozenset({sorted(_META_PATH_ALLOWED_MODULES)!r})\n"
        f"    _FORBIDDEN_NAMES = frozenset({sorted(FORBIDDEN_ATTRS | FORBIDDEN_BUILTINS_OR_CALLS)!r})\n"
        f"    _SAFE_BUILTIN_NAMES = {_SAFE_BUILTIN_NAMES!r}\n"
        f"    _USER_CODE = {code!r}\n"
        f"    _MAX_BUF = {MAX_STDOUT_CHARS + 1024!r}\n"
        "    _orig_import = _b.__import__\n"
        "    for _opt in _PREIMPORT_OPT:\n"
        "        try:\n"
        "            _orig_import(_opt)\n"
        "        except Exception:\n"
        "            pass\n"
        "    _raw_exec = _b.exec\n"
        "    _raw_compile = _b.compile\n"
        "    _dumps = _json.dumps\n"
        "    _orig_stdout = _sys.stdout\n"
        "    _orig_stderr = _sys.stderr\n"
        "    _orig_str = _b.str\n"
        "    _runner_dir = _os.path.normcase(_os.path.realpath(_os.path.dirname(_os.path.abspath(__file__))))\n"
        "    _allowed_read_roots = []\n"
        "    for _cand in (\n"
        "        getattr(_sys, 'prefix', ''),\n"
        "        getattr(_sys, 'base_prefix', ''),\n"
        "        getattr(_sys, 'exec_prefix', ''),\n"
        "        getattr(_sys, 'base_exec_prefix', ''),\n"
        "        *list(getattr(_sys, 'path', ())),\n"
        "    ):\n"
        "        if not _cand or not isinstance(_cand, str):\n"
        "            continue\n"
        "        try:\n"
        "            _rp = _os.path.realpath(_cand)\n"
        "            _np = _os.path.normcase(_rp)\n"
        "        except Exception:\n"
        "            continue\n"
        "        if not _np or _np == _runner_dir:\n"
        "            continue\n"
        "        try:\n"
        "            if _os.path.normcase(_os.path.commonpath([_rp, _runner_dir])) == _np:\n"
        "                continue\n"
        "        except Exception:\n"
        "            pass\n"
        "        if _np not in _allowed_read_roots:\n"
        "            _allowed_read_roots.append(_np)\n"
        "    _write_flags = (\n"
        "        getattr(_os, 'O_WRONLY', 1)\n"
        "        | getattr(_os, 'O_RDWR', 2)\n"
        "        | getattr(_os, 'O_CREAT', 0)\n"
        "        | getattr(_os, 'O_TRUNC', 0)\n"
        "        | getattr(_os, 'O_APPEND', 0)\n"
        "    )\n"
        "    _hook_src = (\n"
        "        'def _audit_hook(_event, _args):\\n'\n"
        f"        '    if _event in ' + {deny_exact_lit!r} + ':\\n'\n"
        "        '        raise PermissionError(f\"Blocked runtime event {_event!r} in symbolic sandbox\")\\n'\n"
        f"        '    for _pfx in ' + {deny_prefixes_lit!r} + ':\\n'\n"
        "        '        if _event.startswith(_pfx):\\n'\n"
        "        '            raise PermissionError(f\"Blocked runtime event {_event!r} in symbolic sandbox\")\\n'\n"
        "        '    if _event == \"open\":\\n'\n"
        "        '        _p = _args[0] if len(_args) > 0 else None\\n'\n"
        "        '        _m = str(_args[1] or \"\") if len(_args) > 1 else \"\"\\n'\n"
        "        '        _fl = int(_args[2] or 0) if len(_args) > 2 and isinstance(_args[2], int) else 0\\n'\n"
        "        '        for _c in (\"w\", \"a\", \"x\", \"+\"):\\n'\n"
        "        '            if _c in _m:\\n'\n"
        "        '                raise PermissionError(\"Write/append/create file open is blocked in symbolic sandbox\")\\n'\n"
        "        '        if bool(_fl & ' + repr(_write_flags) + '):\\n'\n"
        "        '            raise PermissionError(\"Write/append/create file open is blocked in symbolic sandbox\")\\n'\n"
        "        '        if isinstance(_p, int):\\n'\n"
        "        '            raise PermissionError(\"Filesystem read outside stdlib/venv is blocked in symbolic sandbox\")\\n'\n"
        "        '        try:\\n'\n"
        "        '            _ps = _fsdecode(_p)\\n'\n"
        "        '        except Exception:\\n'\n"
        "        '            raise PermissionError(\"Filesystem read outside stdlib/venv is blocked in symbolic sandbox\")\\n'\n"
        "        '        if not _ps or \"\\\\x00\" in _ps:\\n'\n"
        "        '            raise PermissionError(\"Filesystem read outside stdlib/venv is blocked in symbolic sandbox\")\\n'\n"
        "        '        if _ps not in (\"/dev/null\", \"/dev/urandom\", \"NUL\", \"nul\"):\\n'\n"
        "        '            try:\\n'\n"
        "        '                _rp = _realpath(_abspath(_ps))\\n'\n"
        "        '                _np = _normcase(_rp)\\n'\n"
        "        '            except Exception:\\n'\n"
        "        '                raise PermissionError(\"Filesystem read outside stdlib/venv is blocked in symbolic sandbox\")\\n'\n"
        "        '            try:\\n'\n"
        "        '                if _normcase(_commonpath([' + repr(_runner_dir) + ', _np])) == ' + repr(_runner_dir) + ':\\n'\n"
        "        '                    raise PermissionError(\"Filesystem read outside stdlib/venv is blocked in symbolic sandbox\")\\n'\n"
        "        '            except PermissionError:\\n'\n"
        "        '                raise\\n'\n"
        "        '            except Exception:\\n'\n"
        "        '                pass\\n'\n"
        "        '            _ok = False\\n'\n"
        "        '            for _root in ' + repr(tuple(_allowed_read_roots)) + ':\\n'\n"
        "        '                try:\\n'\n"
        "        '                    if _normcase(_commonpath([_root, _np])) == _root:\\n'\n"
        "        '                        _ok = True\\n'\n"
        "        '                        break\\n'\n"
        "        '                except Exception:\\n'\n"
        "        '                    continue\\n'\n"
        "        '            if not _ok:\\n'\n"
        "        '                raise PermissionError(\"Filesystem read outside stdlib/venv is blocked in symbolic sandbox\")\\n'\n"
        "    )\n"
        "    _hook_ns = {\n"
        "        '__builtins__': {},\n"
        "        '_fsdecode': _os.fsdecode,\n"
        "        '_realpath': _os.path.realpath,\n"
        "        '_abspath': _os.path.abspath,\n"
        "        '_normcase': _os.path.normcase,\n"
        "        '_commonpath': _os.path.commonpath,\n"
        "        'PermissionError': _b.PermissionError,\n"
        "        'isinstance': _b.isinstance,\n"
        "        'int': _b.int,\n"
        "        'str': _b.str,\n"
        "        'len': _b.len,\n"
        "        'bool': _b.bool,\n"
        "        'Exception': _b.Exception,\n"
        "    }\n"
        "    _raw_exec(_raw_compile(_hook_src, '<sandbox-audit>', 'exec'), _hook_ns, _hook_ns)\n"
        "    _audit_hook = _hook_ns['_audit_hook']\n"
        "    assert _audit_hook.__closure__ is None\n"
        "    class _AllowlistFinder:\n"
        "        _ALLOWED = _META_ALLOWED\n"
        "        def find_spec(self, fullname, path=None, target=None):\n"
        "            _top = str(fullname or '').split('.', 1)[0]\n"
        "            if _top not in self._ALLOWED:\n"
        "                raise ImportError(f'Blocked non-allowlisted import in symbolic sandbox: {fullname!r}')\n"
        "            return None\n"
        "    assert _AllowlistFinder.find_spec.__closure__ is None\n"
        "    def _user_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "        if level and int(level) != 0:\n"
        "            raise ImportError('Relative imports are blocked in symbolic sandbox')\n"
        "        _parts = str(name or '').split('.')\n"
        "        _top = _parts[0]\n"
        "        if not _top or _top not in _USER_ALLOWED:\n"
        "            raise ImportError(f'Blocked sandbox import: {name!r}')\n"
        "        for _sub in _parts[1:]:\n"
        "            if not _sub or _sub.startswith('_') or _sub in _FORBIDDEN_NAMES:\n"
        "                raise ImportError(f'Blocked sandbox submodule import: {name!r}')\n"
        "        for _item in (fromlist or ()):\n"
        "            _s = str(_item or '')\n"
        "            if not _s or _s == '*' or _s.startswith('_') or _s in _FORBIDDEN_NAMES:\n"
        "                raise ImportError(f'Blocked sandbox symbol import: from {name} import {_s!r}')\n"
        "        return _orig_import(name, globals, locals, fromlist, level)\n"
        "    def _builtins_guarded_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "        if not level:\n"
        "            _top = str(name or '').split('.', 1)[0]\n"
        "            if not _top or _top not in _META_ALLOWED:\n"
        "                raise ImportError(f'Blocked sandbox import: {name!r}')\n"
        "        return _orig_import(name, globals, locals, fromlist, level)\n"
        "    _safe_builtins = {\n"
        "        _k: getattr(_b, _k)\n"
        "        for _k in _SAFE_BUILTIN_NAMES\n"
        "        if hasattr(_b, _k)\n"
        "    }\n"
        "    _safe_builtins['__import__'] = _user_import\n"
        "    _compiled = _raw_compile(_USER_CODE, '<sandbox>', 'exec')\n"
        "    class _BoundedStringIO(_io.StringIO):\n"
        "        def __init__(self, limit):\n"
        "            super().__init__()\n"
        "            self._limit = limit\n"
        "            self._written = 0\n"
        "        def write(self, s):\n"
        "            _txt = _orig_str(s)\n"
        "            _rem = self._limit - self._written\n"
        "            if _rem <= 0:\n"
        "                return len(_txt)\n"
        "            _piece = _txt[:_rem]\n"
        "            self._written += len(_piece)\n"
        "            super().write(_piece)\n"
        "            return len(_txt)\n"
        "    _buf = _BoundedStringIO(_MAX_BUF)\n"
        "    _user_ns = {\n"
        "        '__name__': '__main__',\n"
        "        '__builtins__': _safe_builtins,\n"
        "        'math': _math,\n"
        "        'Fraction': _fractions.Fraction,\n"
        "        'Decimal': _decimal.Decimal,\n"
        "    }\n"
        "    _failed = [False]\n"
        "    def _sb_excepthook(exc_type, exc_value, exc_tb):\n"
        "        _failed[0] = True\n"
        "        _orig_stderr.write(f'{exc_type.__name__}: {exc_value}\\n')\n"
        "        _orig_stderr.flush()\n"
        "    _sys.excepthook = _sb_excepthook\n"
        "    def _emit_result():\n"
        "        if _failed[0]:\n"
        "            return\n"
        "        _res_val = _user_ns.get('result', _user_ns.get('answer', None))\n"
        "        _out_json = _dumps(\n"
        "            {'stdout': _buf.getvalue(), 'result': None if _res_val is None else _orig_str(_res_val)},\n"
        "            ensure_ascii=False,\n"
        "        )\n"
        "        _orig_stdout.write(_out_json + '\\n')\n"
        "        _orig_stdout.flush()\n"
        "    _atexit.register(_emit_result)\n"
        "    _sys.meta_path.insert(0, _AllowlistFinder())\n"
        "    if hasattr(_sys, 'addaudithook'):\n"
        "        _sys.addaudithook(_audit_hook)\n"
        "    _sys.stdout = _buf\n"
        "    _keep_builtins = frozenset(_SAFE_BUILTIN_NAMES) | frozenset({\n"
        "        '__name__', '__import__',\n"
        "        'super', 'type', 'object',\n"
        "        'OSError', 'Warning', 'UserWarning', 'DeprecationWarning', 'RuntimeWarning',\n"
        "    })\n"
        "    _b.__dict__['__import__'] = _builtins_guarded_import\n"
        "    for _k in list(_b.__dict__.keys()):\n"
        "        if _k not in _keep_builtins:\n"
        "            del _b.__dict__[_k]\n"
        "    _main_mod = _sys.modules.get('__main__')\n"
        "    if _main_mod is not None:\n"
        "        _main_mod.__dict__.clear()\n"
        "        _main_mod.__dict__['__name__'] = '__main__'\n"
        "    return _functools.partial(_raw_exec, _compiled, _user_ns, _user_ns)\n"
        "_sb_bootstrap()()\n"
    )


def run_sandboxed_python(
    code: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """AST-validate and run quantitative Python code in an isolated zero-network subprocess."""
    tree = validate_sandbox_ast(code)
    timeout = max(0.5, min(30.0, float(timeout_seconds)))
    harness = _build_runner_harness(code, _requested_optional_modules(tree))

    t0 = time.perf_counter()
    stdout_buf = bytearray()
    stderr_buf = bytearray()
    returncode = -1
    timed_out = False

    with tempfile.TemporaryDirectory(prefix="sb_symbox_") as tmpd:
        runner_p = Path(tmpd) / "run_box.py"
        runner_p.write_text(harness, encoding="utf-8")
        clean_env = _build_clean_subprocess_env(tmpd)

        popen_kwargs: dict[str, Any] = {
            "cwd": tmpd,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": clean_env,
        }
        job: _Win32SandboxJob | None = None
        if os.name == "nt":
            # CREATE_NO_WINDOW (0x08000000) | CREATE_SUSPENDED (0x00000004)
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) | 0x00000004
            job = _Win32SandboxJob()
        else:
            popen_kwargs["preexec_fn"] = _make_posix_preexec(timeout)

        proc = subprocess.Popen(
            [sys.executable, "-I", "-B", "-X", "utf8", str(runner_p)],
            **popen_kwargs,
        )
        t_out = threading.Thread(
            target=_read_pipe_capped, args=(proc.stdout, MAX_PIPE_CAPTURE_BYTES, stdout_buf), daemon=True
        )
        t_err = threading.Thread(
            target=_read_pipe_capped, args=(proc.stderr, MAX_PIPE_CAPTURE_BYTES, stderr_buf), daemon=True
        )
        t_out.start()
        t_err.start()

        try:
            if job is not None:
                job.attach_and_resume(proc)
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(proc)
        finally:
            if job is not None:
                job.close()
            t_out.join(timeout=1.5)
            t_err.join(timeout=1.5)

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
    if timed_out:
        return {
            "ok": False,
            "stdout": "",
            "result": None,
            "error": f"Sandbox execution timed out after {timeout:.1f}s.",
            "elapsed_ms": elapsed_ms,
        }

    stdout_text = stdout_buf.decode("utf-8", errors="replace")
    stderr_text = stderr_buf.decode("utf-8", errors="replace")

    if returncode != 0:
        err = (stderr_text or stdout_text or f"exit {returncode}").strip()[:1500]
        return {
            "ok": False,
            "stdout": "",
            "result": None,
            "error": err,
            "elapsed_ms": elapsed_ms,
        }

    lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
    if not lines:
        return {"ok": True, "stdout": "", "result": None, "error": None, "elapsed_ms": elapsed_ms}
    try:
        payload = json.loads(lines[-1])
        return {
            "ok": True,
            "stdout": str(payload.get("stdout") or "")[:MAX_STDOUT_CHARS],
            "result": payload.get("result"),
            "error": None,
            "elapsed_ms": elapsed_ms,
        }
    except json.JSONDecodeError:
        return {
            "ok": True,
            "stdout": "\n".join(lines)[:MAX_STDOUT_CHARS],
            "result": lines[-1],
            "error": None,
            "elapsed_ms": elapsed_ms,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain AST-Jailed Symbolic Sandbox")
    parser.add_argument("--code", required=True)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    out = run_sandboxed_python(args.code, timeout_seconds=args.timeout)
    print(json.dumps(out, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
