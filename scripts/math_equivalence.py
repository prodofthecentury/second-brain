# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = []
# ///
"""Minerva Quantitative Formatting, LaTeX Preservation & Symbolic Equivalence (Epic 2E.1).

Provides:
- ``preserve_latex_in_html``: extracts MathJax ``<script type="math/tex">`` &
  KaTeX/MathML ``<annotation encoding="application/x-tex">`` before HTML stripping.
- ``normalize_latex_expr`` & balanced ``\\frac`` / ``\\sqrt`` parser (zero ``eval()``).
- Exact pure-stdlib AST + ``Fraction`` + Schwartz-Zippel multi-point symbolic
  equivalence checking (``\\frac{12}{36} == \\frac{1}{3}``, ``(x+1)^2 == x^2+2x+1``,
  ``100 cm == 1 m``, comma tuples/sets, equations), with optional ``sympy`` acceleration.
- Minerva CoT scratchpad builder + symbolic ``majority@k`` & ``Pass@1`` /
  ``Majority@k`` / ``Verifier-Pass@k`` metric aggregator.
"""

from __future__ import annotations

import argparse
import ast
import cmath
from dataclasses import asdict, dataclass
from fractions import Fraction
import importlib.util
import json
import math
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ in ("math_equivalence", "scripts.math_equivalence"):
    sys.modules.setdefault("math_equivalence", sys.modules[__name__])
    sys.modules.setdefault("scripts.math_equivalence", sys.modules[__name__])

_SYMPY_AVAILABLE = bool(importlib.util.find_spec("sympy"))

_LATEX_SPAN_RE = re.compile(
    r"(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|"
    r"\\begin\{(?:equation|align|gather|multline)\*?\}[\s\S]+?\\end\{(?:equation|align|gather|multline)\*?\}|"
    r"(?<!\\)\$[^$\n]+\$(?!\d))",
    re.MULTILINE,
)

# Canonical base-unit conversion table: unit -> (dimension, multiplier_to_base)
_UNIT_TABLE: dict[str, tuple[str, float]] = {
    "m": ("length", 1.0),
    "meter": ("length", 1.0),
    "meters": ("length", 1.0),
    "metre": ("length", 1.0),
    "cm": ("length", 0.01),
    "mm": ("length", 0.001),
    "km": ("length", 1000.0),
    "s": ("time", 1.0),
    "sec": ("time", 1.0),
    "secs": ("time", 1.0),
    "second": ("time", 1.0),
    "seconds": ("time", 1.0),
    "ms": ("time", 0.001),
    "min": ("time", 60.0),
    "minute": ("time", 60.0),
    "minutes": ("time", 60.0),
    "h": ("time", 3600.0),
    "hr": ("time", 3600.0),
    "hrs": ("time", 3600.0),
    "hour": ("time", 3600.0),
    "hours": ("time", 3600.0),
    "g": ("mass", 0.001),
    "kg": ("mass", 1.0),
    "mg": ("mass", 1e-6),
    "hz": ("frequency", 1.0),
    "khz": ("frequency", 1000.0),
    "mhz": ("frequency", 1e6),
    "ghz": ("frequency", 1e9),
    "j": ("energy", 1.0),
    "kj": ("energy", 1000.0),
    "w": ("power", 1.0),
    "kw": ("power", 1000.0),
    "v": ("voltage", 1.0),
    "mv": ("voltage", 0.001),
    "kv": ("voltage", 1000.0),
    "a": ("current", 1.0),
    "ma": ("current", 0.001),
}

_TRAILING_UNIT_RE = re.compile(
    r"^(?P<expr>.+?)\s*(?:\\(?:text|mathrm|operatorname|unit)\s*\{(?P<u>[a-zA-Z]+)\}|(?P<u2>[a-zA-Z]{1,8}))\s*$"
)


def preserve_latex_in_html(markup: str | bytes) -> str:
    """Convert HTML MathJax ``<script type='math/tex'>`` and KaTeX/MathML annotations to $...$/$$...$$."""
    text = markup.decode("utf-8", errors="replace") if isinstance(markup, (bytes, bytearray)) else str(markup or "")
    if not text or ("<script" not in text.lower() and "<math" not in text.lower() and "katex" not in text.lower() and "<annotation" not in text.lower()):
        return text
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return text

    soup = BeautifulSoup(text, "html.parser")
    # 1. Extract <script type="math/tex..."> before downstream HTML cleaners strip scripts
    for sc in list(soup.find_all("script")):
        stype = str(sc.get("type") or "").lower()
        if "math/tex" in stype or "math/asciimath" in stype:
            raw_tex = (sc.string or sc.get_text() or "").strip()
            if raw_tex:
                delim = "$$" if "display" in stype else "$"
                sc.replace_with(f" {delim}{raw_tex}{delim} ")
            else:
                sc.decompose()

    # 2. Extract KaTeX wrappers holding <annotation encoding="application/x-tex">
    for kwrap in list(soup.find_all(class_=lambda c: c and any(tok in (c if isinstance(c, list) else str(c).split()) for tok in ("katex-display", "katex")))):
        ann = kwrap.find("annotation", attrs={"encoding": re.compile(r"x-tex|tex", re.I)})
        if ann is not None:
            raw_tex = ann.get_text().strip()
            if raw_tex:
                raw_cls = kwrap.get("class")
                cls_tokens = list(raw_cls) if isinstance(raw_cls, list) else str(raw_cls or "").split()
                is_disp = "katex-display" in cls_tokens or any(
                    "katex-display" in (list(p.get("class")) if isinstance(p.get("class"), list) else str(p.get("class") or "").split())
                    for p in kwrap.parents
                    if getattr(p, "get", None)
                )
                delim = "$$" if is_disp else "$"
                if is_disp:
                    tag = soup.new_tag("p")
                    tag.string = f"{delim}{raw_tex}{delim}"
                    kwrap.replace_with(tag)
                else:
                    kwrap.replace_with(f" {delim}{raw_tex}{delim} ")

    # 3. Any remaining standalone MathML <math> with <annotation encoding="application/x-tex">
    for mwrap in list(soup.find_all("math")):
        ann = mwrap.find("annotation", attrs={"encoding": re.compile(r"x-tex|tex", re.I)})
        if ann is not None:
            raw_tex = ann.get_text().strip()
            if raw_tex:
                is_blk = str(mwrap.get("display") or "").lower() == "block"
                delim = "$$" if is_blk else "$"
                if is_blk:
                    tag = soup.new_tag("p")
                    tag.string = f"{delim}{raw_tex}{delim}"
                    mwrap.replace_with(tag)
                else:
                    mwrap.replace_with(f" {delim}{raw_tex}{delim} ")

    return str(soup)


def unescape_markdown_latex_spans(markdown_text: str) -> str:
    """Restore underscores/asterisks escaped by Markdown formatters inside $...$/$$...$$ spans."""
    def _fix(match: re.Match[str]) -> str:
        span = match.group(0)
        return span.replace("\\_", "_").replace("\\*", "*")

    return _LATEX_SPAN_RE.sub(_fix, str(markdown_text or ""))


def extract_math_spans(text: str) -> list[str]:
    """Return all raw LaTeX/MathJax spans in order."""
    return [m.group(0) for m in _LATEX_SPAN_RE.finditer(text or "")]


def contains_latex_math(text: str) -> bool:
    return bool(_LATEX_SPAN_RE.search(text or ""))


def _extract_braced(text: str, pos: int) -> tuple[str, int]:
    """Extract content inside `{...}` starting at index ``pos``, returning (inner, next_pos)."""
    n = len(text)
    while pos < n and text[pos].isspace():
        pos += 1
    if pos >= n:
        return "", pos
    if text[pos] != "{":
        # Single character operand like \frac 1 3 or \sqrt x
        return text[pos], pos + 1
    depth = 0
    start = pos + 1
    idx = pos
    while idx < n:
        ch = text[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:idx], idx + 1
        idx += 1
    return text[start:], n


def _strip_boxed(expr: str) -> str:
    txt = expr.strip()
    while True:
        m = re.search(r"\\boxed\s*\{", txt)
        if not m:
            break
        inner, end_pos = _extract_braced(txt, m.end() - 1)
        if m.start() == 0 and end_pos == len(txt):
            txt = inner.strip()
        else:
            txt = (txt[: m.start()] + inner + txt[end_pos:]).strip()
    return txt


def _replace_frac_and_sqrt(expr: str) -> str:
    out = expr
    # Replace \dfrac, \tfrac, \frac
    while True:
        m = re.search(r"\\(?:d|t)?frac(?![a-zA-Z])", out)
        if not m:
            break
        num, p1 = _extract_braced(out, m.end())
        den, p2 = _extract_braced(out, p1)
        repl = f"(({_replace_frac_and_sqrt(num)})/({_replace_frac_and_sqrt(den)}))"
        out = out[: m.start()] + repl + out[p2:]
    # Replace \sqrt[n]{x} and \sqrt{x}
    while True:
        m = re.search(r"\\sqrt(?![a-zA-Z])", out)
        if not m:
            break
        pos = m.end()
        while pos < len(out) and out[pos].isspace():
            pos += 1
        root_n = None
        if pos < len(out) and out[pos] == "[":
            end_br = out.find("]", pos + 1)
            if end_br != -1:
                root_n = out[pos + 1 : end_br].strip()
                pos = end_br + 1
        radicand, p_end = _extract_braced(out, pos)
        inner_r = _replace_frac_and_sqrt(radicand)
        repl = f"(({inner_r})**(1/({root_n})))" if root_n else f"sqrt({inner_r})"
        out = out[: m.start()] + repl + out[p_end:]
    return out


def strip_and_parse_units(expr: str) -> tuple[str, tuple[str, float] | None]:
    """Split trailing unit if present and valid in _UNIT_TABLE; return (core_expr, (dimension, mult) | None)."""
    clean = (expr or "").strip()
    m = _TRAILING_UNIT_RE.match(clean)
    if not m:
        return clean, None
    cand_unit = (m.group("u") or m.group("u2") or "").strip().lower()
    if cand_unit in _UNIT_TABLE:
        core = m.group("expr").strip()
        # Avoid stripping variable identifiers like "2 * x" or single-letter variables in algebraic formulas
        if cand_unit in {"g", "h", "m", "s", "a", "v", "w"} and re.search(r"[a-zA-Z]", core) and m.group("u") is None:
            return clean, None
        return core, _UNIT_TABLE[cand_unit]
    return clean, None


def normalize_latex_expr(expr: str) -> str:
    """Normalize a LaTeX math expression into a deterministic Python-parseable algebraic string."""
    txt = str(expr or "").strip().replace("\x00", "").replace("\x01", "")
    for left_d, right_d in (("$$", "$$"), ("\\[", "\\]"), ("\\(", "\\)"), ("$", "$")):
        if txt.startswith(left_d) and txt.endswith(right_d) and len(txt) >= len(left_d) + len(right_d):
            txt = txt[len(left_d) : -len(right_d)].strip()
    txt = _strip_boxed(txt)
    txt = txt.replace("\\left", "").replace("\\right", "")
    txt = txt.replace("\\,", "").replace("\\;", "").replace("\\!", "").replace("\\ ", " ")
    txt = txt.replace("\\cdot", "*").replace("\\times", "*").replace("\\ast", "*").replace("\\div", "/")
    txt = txt.replace("−", "-").replace("×", "*").replace("÷", "/")
    txt = re.sub(r"\\text\s*\{([^}]*)\}", r"\1", txt)
    txt = re.sub(r"\\mathrm\s*\{([^}]*)\}", r"\1", txt)
    txt = _replace_frac_and_sqrt(txt)
    # Handle percentages like 50% -> (50/100)
    txt = re.sub(r"(\d+(?:\.\d+)?)\s*\\%", r"((\1)/100)", txt)
    txt = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"((\1)/100)", txt)
    # Normalize known LaTeX constants/functions
    for cmd, fn in (
        (r"\\pi(?![a-zA-Z])", "pi"),
        (r"\\sin(?![a-zA-Z])", "sin"),
        (r"\\cos(?![a-zA-Z])", "cos"),
        (r"\\tan(?![a-zA-Z])", "tan"),
        (r"\\log(?![a-zA-Z])", "log"),
        (r"\\ln(?![a-zA-Z])", "ln"),
        (r"\\exp(?![a-zA-Z])", "exp"),
        (r"\\abs(?![a-zA-Z])", "abs"),
        (r"\\factorial(?![a-zA-Z])", "factorial"),
    ):
        txt = re.sub(cmd, fn, txt)
    # Braced exponents like x^{2} -> x**(2)
    txt = txt.replace("{", "(").replace("}", ")")
    txt = txt.replace("^", "**")
    # Protect scientific notation literals (e.g. 1e-5, 2.5E+3, 1e5) from implicit multiplication
    sci_literals: list[str] = []

    def _stash_sci(match: re.Match[str]) -> str:
        idx = len(sci_literals)
        sci_literals.append(match.group(0))
        return f"\x00SCI{idx}\x01"

    txt = re.sub(r"(?<![a-zA-Z0-9_.])(?:\d+(?:\.\d*)?|\.\d+)[eE][+-]?\d+", _stash_sci, txt)
    # Implicit multiplication: number/sci/) followed by var/(/sci, var followed by (
    known_fns = {"sqrt", "sin", "cos", "tan", "log", "ln", "exp", "abs", "factorial", "pi"}
    txt = re.sub(r"(\d|\)|\x01)\s*([a-zA-Z(\x00])", r"\1*\2", txt)

    def _var_paren(match: re.Match[str]) -> str:
        ident = match.group(1)
        return f"{ident}(" if ident in known_fns else f"{ident}*("

    txt = re.sub(r"\b([a-zA-Z][a-zA-Z0-9_]*)\s*\(", _var_paren, txt)
    if sci_literals:
        txt = re.sub(r"\x00SCI(\d+)\x01", lambda m: sci_literals[int(m.group(1))], txt)
    return re.sub(r"\s+", "", txt).strip()


def _safe_log(x: complex, base: complex | None = None) -> complex:
    if base is None:
        return cmath.log(x, 10)
    return cmath.log(x) / cmath.log(base)


def _safe_factorial(x: complex) -> complex:
    if abs(x.imag) > 1e-12:
        raise ValueError("factorial requires a non-negative real integer")
    n = round(x.real)
    if n < 0 or n > 170 or abs(x.real - n) > 1e-12:
        raise ValueError("factorial requires a non-negative integer <= 170")
    return complex(math.factorial(int(n)))


_ALLOWED_FUNCTIONS: dict[str, Any] = {
    "sqrt": cmath.sqrt,
    "sin": cmath.sin,
    "cos": cmath.cos,
    "tan": cmath.tan,
    "log": _safe_log,
    "ln": cmath.log,
    "exp": cmath.exp,
    "abs": abs,
    "factorial": _safe_factorial,
}
_ALLOWED_CONSTS: dict[str, complex] = {
    "pi": math.pi + 0j,
    "e": math.e + 0j,
    "i": 1j,
}
_MATH_IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_MAX_MATH_AST_DEPTH = 64
_MAX_MATH_AST_NODES = 512


def _validate_math_ast(tree: ast.AST) -> ast.Expression:
    """Strictly validate a parsed math expression AST against a closed allowlist.

    Permits ONLY:
    - ``ast.Expression``
    - numeric ``ast.Constant`` (int/float, rejecting bool/str/bytes/None/non-finite)
    - ``ast.Name`` matching ``^[a-zA-Z][a-zA-Z0-9_]*$`` (never starting with ``_``)
    - ``ast.UnaryOp`` (``UAdd``, ``USub``)
    - ``ast.BinOp`` (``Add``, ``Sub``, ``Mult``, ``Div``, ``Pow``)
    - ``ast.Call`` to ``_ALLOWED_FUNCTIONS`` with positional args only (no keywords/attributes/subscripts).
    """
    if not isinstance(tree, ast.Expression):
        raise ValueError(f"Expected ast.Expression root, got {type(tree).__name__}")

    node_count = 0

    def _check(node: ast.AST, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > _MAX_MATH_AST_NODES:
            raise ValueError("Math AST exceeds maximum node limit")
        if depth > _MAX_MATH_AST_DEPTH:
            raise ValueError("Math AST exceeds maximum nesting depth")

        if isinstance(node, ast.Expression):
            _check(node.body, depth + 1)
            return
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"Disallowed constant in math AST: {node.value!r}")
            if isinstance(node.value, float) and not math.isfinite(node.value):
                raise ValueError(f"Non-finite constant in math AST: {node.value!r}")
            return
        if isinstance(node, ast.Name):
            if not _MATH_IDENT_RE.fullmatch(node.id):
                raise ValueError(f"Disallowed identifier in math AST: {node.id!r}")
            return
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, (ast.UAdd, ast.USub)):
                raise ValueError(f"Disallowed unary operator in math AST: {type(node.op).__name__}")
            _check(node.operand, depth + 1)
            return
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                raise ValueError(f"Disallowed binary operator in math AST: {type(node.op).__name__}")
            _check(node.left, depth + 1)
            _check(node.right, depth + 1)
            return
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS or node.keywords:
                raise ValueError("Disallowed function call in math AST")
            max_args = 2 if node.func.id == "log" else 1
            if not (1 <= len(node.args) <= max_args):
                raise ValueError(f"Invalid argument count for {node.func.id} in math AST")
            for arg in node.args:
                _check(arg, depth + 1)
            return
        raise ValueError(f"Disallowed AST node in math expression: {type(node).__name__}")

    _check(tree, 0)
    return tree


def _safe_ast_eval(node: ast.AST, env: dict[str, complex | Fraction]) -> complex | Fraction:
    """Strictly evaluate numeric/algebraic AST with zero code execution."""
    if isinstance(node, ast.Expression):
        return _safe_ast_eval(node.body, env)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"Unsupported constant in math AST: {node.value!r}")
        if isinstance(node.value, int):
            return Fraction(node.value, 1)
        return Fraction(str(node.value)) if all(isinstance(v, Fraction) for v in env.values()) else float(node.value)
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id]
        if node.id in env:
            return env[node.id]
        raise ValueError(f"Unbound symbol {node.id!r}")
    if isinstance(node, ast.UnaryOp):
        v = _safe_ast_eval(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return +v
        if isinstance(node.op, ast.USub):
            return -v
        raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _safe_ast_eval(node.left, env)
        right = _safe_ast_eval(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ZeroDivisionError("division by zero")
            return left / right
        if isinstance(node.op, ast.Pow):
            # Guard absurd exponents
            if isinstance(right, (int, float, Fraction)) and abs(float(right)) > 200:
                raise ValueError("Exponent too large")
            if isinstance(left, Fraction) and isinstance(right, Fraction) and right.denominator == 1:
                return left ** int(right.numerator)
            return complex(left) ** complex(right)
        raise ValueError(f"Unsupported binary op: {type(node.op).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS or node.keywords:
            raise ValueError("Unsupported function call in math AST")
        args = [_safe_ast_eval(a, env) for a in node.args]
        return _ALLOWED_FUNCTIONS[node.func.id](*(complex(a) for a in args))
    raise ValueError(f"Disallowed AST node in math expression: {type(node).__name__}")


def _extract_variables(tree: ast.AST) -> list[str]:
    names = {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id not in _ALLOWED_FUNCTIONS and n.id not in _ALLOWED_CONSTS
    }
    return sorted(names)


def _ast_to_sympy(node: ast.AST, sp: Any) -> Any:
    """Convert an already-validated math AST directly into a SymPy expression (zero eval/sympify)."""
    if isinstance(node, ast.Expression):
        return _ast_to_sympy(node.body, sp)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"Unsupported constant for SymPy conversion: {node.value!r}")
        if isinstance(node.value, int):
            return sp.Integer(node.value)
        return sp.Rational(str(node.value))
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return sp.pi
        if node.id == "e":
            return sp.E
        if node.id == "i":
            return sp.I
        return sp.Symbol(node.id)
    if isinstance(node, ast.UnaryOp):
        operand = _ast_to_sympy(node.operand, sp)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _ast_to_sympy(node.left, sp)
        right = _ast_to_sympy(node.right, sp)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Pow):
            if isinstance(node.right, ast.Constant) and isinstance(node.right.value, (int, float)) and abs(float(node.right.value)) > 200:
                raise ValueError("Exponent too large")
            return left ** right
        raise ValueError(f"Unsupported binary op: {type(node.op).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCTIONS or node.keywords:
            raise ValueError("Unsupported function call for SymPy conversion")
        fn = node.func.id
        args = [_ast_to_sympy(a, sp) for a in node.args]
        if fn == "ln":
            return sp.log(args[0])
        if fn == "log":
            return sp.log(args[0], 10) if len(args) == 1 else sp.log(args[0], args[1])
        if fn == "sqrt":
            return sp.sqrt(args[0])
        if fn == "sin":
            return sp.sin(args[0])
        if fn == "cos":
            return sp.cos(args[0])
        if fn == "tan":
            return sp.tan(args[0])
        if fn == "exp":
            return sp.exp(args[0])
        if fn == "abs":
            return sp.Abs(args[0])
        if fn == "factorial":
            return sp.factorial(args[0])
    raise ValueError(f"Disallowed AST node for SymPy conversion: {type(node).__name__}")


def _try_sympy_equivalent(
    tree_a: ast.AST,
    tree_b: ast.AST,
    *,
    scale_a: float = 1.0,
    scale_b: float = 1.0,
) -> bool | None:
    if not _SYMPY_AVAILABLE:
        return None
    try:
        _validate_math_ast(tree_a)
        _validate_math_ast(tree_b)
        import sympy as sp

        ea = _ast_to_sympy(tree_a, sp) * sp.Rational(str(scale_a))
        eb = _ast_to_sympy(tree_b, sp) * sp.Rational(str(scale_b))
        return bool(sp.simplify(ea - eb) == 0)
    except Exception:
        return None


def _numeric_or_symbolic_single_equiv(
    raw_a: str,
    raw_b: str,
    *,
    tol: float = 1e-9,
    allow_nonzero_scalar_multiple: bool = False,
) -> bool:
    core_a, unit_a = strip_and_parse_units(raw_a)
    core_b, unit_b = strip_and_parse_units(raw_b)
    if (unit_a is None) != (unit_b is None):
        return False
    scale_a = 1.0
    scale_b = 1.0
    if unit_a is not None and unit_b is not None:
        if unit_a[0] != unit_b[0]:
            return False
        scale_a = unit_a[1]
        scale_b = unit_b[1]

    # Handle equations LHS = RHS by rewriting (LHS - RHS) == 0 up to nonzero scalar multiple
    if "=" in core_a and "=" in core_b:
        la, ra = core_a.split("=", 1)
        lb, rb = core_b.split("=", 1)
        return _numeric_or_symbolic_single_equiv(
            f"({la})-({ra})",
            f"({lb})-({rb})",
            tol=tol,
            allow_nonzero_scalar_multiple=True,
        )
    if ("=" in core_a) != ("=" in core_b):
        return False

    norm_a = normalize_latex_expr(core_a)
    norm_b = normalize_latex_expr(core_b)
    if not norm_a or not norm_b:
        return False

    # Strictly parse and validate BOTH ASTs BEFORE any equality shortcut, SymPy, or numeric evaluation
    try:
        tree_a = _validate_math_ast(ast.parse(norm_a, mode="eval"))
        tree_b = _validate_math_ast(ast.parse(norm_b, mode="eval"))
    except (SyntaxError, ValueError):
        return False

    if norm_a == norm_b and abs(scale_a - scale_b) <= 1e-12:
        return True

    vars_a = _extract_variables(tree_a)
    vars_b = _extract_variables(tree_b)
    all_vars = sorted(set(vars_a) | set(vars_b))

    if not all_vars:
        try:
            va = _safe_ast_eval(tree_a, {})
            vb = _safe_ast_eval(tree_b, {})
            if isinstance(va, Fraction) and isinstance(vb, Fraction):
                sa = Fraction(str(scale_a))
                sb = Fraction(str(scale_b))
                if va * sa == vb * sb:
                    return True
            ca = complex(va) * scale_a
            cb = complex(vb) * scale_b
            return abs(ca - cb) <= tol * max(1.0, abs(ca), abs(cb))
        except Exception:
            return False

    if _SYMPY_AVAILABLE and not allow_nonzero_scalar_multiple:
        sp_res = _try_sympy_equivalent(tree_a, tree_b, scale_a=scale_a, scale_b=scale_b)
        if sp_res is not None:
            return sp_res

    # Multivariate Schwartz-Zippel deterministic probe over the union of variables
    # (handles cancelling identities like "x - x" == "0" cleanly even without SymPy)
    probe_vectors = [
        {v: (1.137 + 0.21j * (i + 1)) for i, v in enumerate(all_vars)},
        {v: (2.718 + 0.17j * (i + 2)) for i, v in enumerate(all_vars)},
        {v: (-1.414 + 0.31j * (i + 1)) for i, v in enumerate(all_vars)},
        {v: (0.618 + 0.43 * (i + 1)) for i, v in enumerate(all_vars)},
        {v: (3.331 - 0.19j * (i + 1)) for i, v in enumerate(all_vars)},
        {v: (-0.771 + 0.53 * (i + 1)) for i, v in enumerate(all_vars)},
    ]
    successes = 0
    ref_ratio: complex | None = None
    for env in probe_vectors:
        try:
            va = complex(_safe_ast_eval(tree_a, env)) * scale_a
            vb = complex(_safe_ast_eval(tree_b, env)) * scale_b
        except ZeroDivisionError:
            continue
        except Exception:
            return False
        if allow_nonzero_scalar_multiple:
            if abs(va) <= tol and abs(vb) <= tol:
                successes += 1
                continue
            if abs(vb) <= tol or abs(va) <= tol:
                return False
            ratio = va / vb
            if ref_ratio is None:
                ref_ratio = ratio
            elif abs(ratio - ref_ratio) > tol * max(1.0, abs(ref_ratio)):
                return False
            successes += 1
        else:
            if abs(va - vb) > tol * max(1.0, abs(va), abs(vb)):
                return False
            successes += 1
    return successes >= 4


def are_math_equivalent(expr_a: str, expr_b: str, *, tol: float = 1e-9) -> bool:
    """Return True if two LaTeX/numeric/algebraic/set/tuple expressions are mathematically equivalent."""
    sa = str(expr_a or "").strip()
    sb = str(expr_b or "").strip()
    if not sa or not sb:
        return False

    # Comma-separated set/tuple support when delimited evenly outside brackets
    if sa.startswith("{") and sa.endswith("}") and sb.startswith("{") and sb.endswith("}") and "," in sa:
        items_a = [x.strip() for x in sa[1:-1].split(",") if x.strip()]
        items_b = [x.strip() for x in sb[1:-1].split(",") if x.strip()]
        if len(items_a) != len(items_b):
            return False
        used = [False] * len(items_b)
        for ia in items_a:
            matched = False
            for j, ib in enumerate(items_b):
                if not used[j] and are_math_equivalent(ia, ib, tol=tol):
                    used[j] = True
                    matched = True
                    break
            if not matched:
                return False
        return True

    if sa.startswith("(") and sa.endswith(")") and sb.startswith("(") and sb.endswith(")") and "," in sa:
        items_a = [x.strip() for x in sa[1:-1].split(",") if x.strip()]
        items_b = [x.strip() for x in sb[1:-1].split(",") if x.strip()]
        return len(items_a) == len(items_b) and all(
            are_math_equivalent(ia, ib, tol=tol) for ia, ib in zip(items_a, items_b)
        )

    return _numeric_or_symbolic_single_equiv(sa, sb, tol=tol)


def build_minerva_cot_prompt(problem: str, *, context_notes: str = "") -> str:
    """Build a structured Minerva step-by-step CoT scratchpad template ending with Final Answer."""
    parts = [
        "Solve the quantitative problem carefully step by step.",
        "Preserve exact fractions, radicals, symbols, and physical units without premature rounding.",
        "End your derivation with `Final Answer: \\boxed{...}`.",
    ]
    if context_notes.strip():
        parts.append(f"\nVerified Context:\n{context_notes.strip()}")
    parts.append(f"\nProblem:\n{problem.strip()}\n\nSolution:\n")
    return "\n".join(parts)


def extract_final_answer(text: str) -> str:
    """Extract the boxed or Final Answer span from a Minerva-style derivation."""
    raw = str(text or "").strip()
    if "\\boxed" in raw:
        idx = raw.rfind("\\boxed")
        inner, _ = _extract_braced(raw, idx + len("\\boxed"))
        if inner.strip():
            return inner.strip()
    m = re.search(r"Final Answer\s*:\s*(.+)$", raw, re.IGNORECASE | re.MULTILINE)
    if m:
        return m.group(1).strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def aggregate_majority_at_k(samples: list[str], *, tol: float = 1e-9) -> dict[str, Any]:
    """Cluster raw samples by symbolic equivalence and compute Minerva majority@k winner."""
    clusters: list[dict[str, Any]] = []
    for raw in samples or ():
        ans = extract_final_answer(raw)
        if not ans:
            continue
        placed = False
        for cl in clusters:
            if are_math_equivalent(ans, cl["representative"], tol=tol):
                cl["count"] += 1
                cl["members"].append(ans)
                placed = True
                break
        if not placed:
            clusters.append(
                {
                    "representative": ans,
                    "normalized": normalize_latex_expr(ans),
                    "count": 1,
                    "members": [ans],
                }
            )
    clusters.sort(key=lambda c: (-int(c["count"]), str(c["normalized"])))
    total = sum(int(c["count"]) for c in clusters)
    top = clusters[0] if clusters else None
    return {
        "total_samples": len(samples),
        "parsed_answers": total,
        "majority_answer": top["representative"] if top else "",
        "majority_votes": top["count"] if top else 0,
        "confidence": round((top["count"] / total), 4) if (top and total > 0) else 0.0,
        "clusters": clusters,
    }


def score_benchmark_metrics(
    problems: list[dict[str, Any]],
    *,
    tol: float = 1e-9,
) -> dict[str, Any]:
    """Compute separate AI4Math Survey evaluation metrics: Pass@1, Majority@k, and Verifier-Pass@k.

    Each item has ``{"ground_truth": str, "samples": list[str], "verified_candidate": str | None}``.
    """
    n = len(problems)
    if n == 0:
        return {"count": 0, "pass_at_1": 0.0, "majority_at_k": 0.0, "verifier_pass_at_k": 0.0}
    p1 = 0
    maj = 0
    ver = 0
    for item in problems:
        gt = str(item.get("ground_truth") or "")
        samples = list(item.get("samples") or ())
        if samples and are_math_equivalent(extract_final_answer(samples[0]), gt, tol=tol):
            p1 += 1
        agg = aggregate_majority_at_k(samples, tol=tol)
        if agg["majority_answer"] and are_math_equivalent(agg["majority_answer"], gt, tol=tol):
            maj += 1
        vc = item.get("verified_candidate")
        if vc and are_math_equivalent(str(vc), gt, tol=tol):
            ver += 1
    return {
        "count": n,
        "pass_at_1": round(p1 / n, 4),
        "majority_at_k": round(maj / n, 4),
        "verifier_pass_at_k": round(ver / n, 4),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Second Brain Symbolic Math Equivalence & Majority@k")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_eq = sub.add_parser("equiv", help="Check symbolic equivalence between two LaTeX/math expressions")
    p_eq.add_argument("expr_a")
    p_eq.add_argument("expr_b")
    args = parser.parse_args(argv)
    ok = are_math_equivalent(args.expr_a, args.expr_b)
    print(json.dumps({"equivalent": ok, "norm_a": normalize_latex_expr(args.expr_a), "norm_b": normalize_latex_expr(args.expr_b)}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
