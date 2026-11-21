#!/usr/bin/env python3
"""Fail when the constants table in ``docs/EDITORIAL_PIPELINE.md`` drifts from the code.

The reference doc is the place where operators read pipeline constants, and an outdated
table is worse than no table: it silently tells a reviewer that a 30-second timeout is a
6-second one. This checker reads the documented value for every row of the table and
compares it with the value in the module named by the row's "location" column.

Values are read from the source with ``ast`` (no imports, no ``settings`` construction, so
no environment variables are needed). The one computed constant, ``PROMPT_VERSION``, is
only resolvable by importing its module, so it is deferred unless ``--allow-imports`` is
given; CI runs that mode in the job where the project's dependencies are installed.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_DOC = Path("docs/EDITORIAL_PIPELINE.md")
DEFAULT_CONFIG = Path("app/config.py")
DEFAULT_ROOT = Path(".")

HEADING = "## ۱۰. جدول ثابت‌های کلیدی"

_PERSIAN_AND_ARABIC_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫",
    "01234567890123456789.",
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BACKTICKED_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_BACKTICKED_TEXT = re.compile(r"`([^`]+)`")
_SOURCE_FILE = re.compile(r"`([A-Za-z0-9_./-]+\.py)`")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_BINARY_OPS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
    ast.FloorDiv: lambda left, right: left // right,
}
_ALLOWED_CALLS = {"frozenset": frozenset, "set": set, "tuple": tuple, "list": list, "dict": dict, "len": len}


class Unresolved(Exception):
    """The documented value cannot be derived from the source without importing it."""


def _to_ascii_digits(text: str) -> str:
    return text.translate(_PERSIAN_AND_ARABIC_DIGITS)


def _table_rows(doc_text: str, heading: str) -> list[list[str]]:
    """Return the cells of the Markdown table that follows ``heading``."""
    lines = doc_text.splitlines()
    heading_index = next(
        (index for index, line in enumerate(lines) if line.strip() == heading),
        None,
    )
    if heading_index is None:
        raise ValueError(f"heading {heading!r} not found")

    rows: list[list[str]] = []
    started = False
    for line in lines[heading_index + 1 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if started:
                break
            continue
        started = True
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":", " "} for cell in cells if cell):
            continue  # separator row
        rows.append(cells)
    if not started:
        raise ValueError(f"no table found under heading {heading!r}")
    return rows[1:]  # drop the header row


def _evaluate(node: ast.expr, namespace: dict[str, Any]) -> Any:
    """Evaluate the subset of expressions used by module-level constants."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in namespace:
            return namespace[node.id]
        raise Unresolved(f"name {node.id!r} is not a constant in this module")
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate(item, namespace) for item in node.elts)
    if isinstance(node, ast.List):
        return [_evaluate(item, namespace) for item in node.elts]
    if isinstance(node, ast.Set):
        return {_evaluate(item, namespace) for item in node.elts}
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            raise Unresolved("dict unpacking is not supported")
        return {
            _evaluate(key, namespace): _evaluate(value, namespace)
            for key, value in zip(node.keys, node.values, strict=True)
        }
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_evaluate(node.operand, namespace)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        return _BINARY_OPS[type(node.op)](_evaluate(node.left, namespace), _evaluate(node.right, namespace))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_CALLS:
        if node.keywords or any(isinstance(arg, ast.Starred) for arg in node.args):
            raise Unresolved("starred or keyword arguments are not supported")
        return _ALLOWED_CALLS[node.func.id](*[_evaluate(arg, namespace) for arg in node.args])
    raise Unresolved(f"{type(node).__name__} expression needs an import")


def _module_values(path: Path) -> dict[str, Any]:
    """Module-level constants that can be evaluated without importing the module."""
    namespace: dict[str, Any] = {}
    for statement in ast.parse(path.read_text(encoding="utf-8")).body:
        targets: list[ast.expr] = []
        if isinstance(statement, ast.Assign):
            targets = list(statement.targets)
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        if not targets or statement.value is None:
            continue
        try:
            value = _evaluate(statement.value, namespace)
        except Unresolved:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                namespace[target.id] = value
    return namespace


def _config_defaults(config_path: Path) -> dict[str, Any]:
    """Defaults of every annotated class attribute in ``config_path`` (values only, no checks)."""
    defaults: dict[str, Any] = {}
    for node in ast.walk(ast.parse(config_path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or statement.value is None:
                continue
            if not isinstance(statement.target, ast.Name):
                continue
            try:
                defaults[statement.target.id] = _evaluate(statement.value, {})
            except Unresolved:
                continue
    return defaults


def _import_value(module_rel: str, name: str, root: Path) -> Any:
    module_name = ".".join(Path(module_rel).with_suffix("").parts)
    root = root.resolve()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - depends on the caller's environment
        raise Unresolved(f"cannot import {module_name}: {exc}") from exc
    if not hasattr(module, name):
        raise Unresolved(f"{module_name} has no attribute {name!r}")
    return getattr(module, name)


def _compare(cells: list[str], actual: Any) -> tuple[bool, str]:
    """Compare a documented cell with the real value, tolerant of Persian digits and units."""
    documented = _to_ascii_digits(cells[1])

    if "KB" in documented and isinstance(actual, int):
        number = _NUMBER.search(documented)
        if number is None:
            raise ValueError("no number in a KB value")
        expected = int(float(number.group(0)) * 1024)
        return expected == actual, f"{number.group(0)}KB"

    if "ورودی" in documented:
        number = _NUMBER.search(documented)
        if number is None:
            raise ValueError("no number in a count value")
        return int(number.group(0)) == len(actual), f"len() == {number.group(0)}"

    bare = cells[1].strip().strip("`")
    if bare.startswith("{"):
        names = {name for name in _IDENTIFIER.findall(documented) if name not in {"frozenset", "set"}}
        return names == set(actual), "{" + ", ".join(sorted(names)) + "}"

    token = _BACKTICKED_TEXT.search(cells[1])
    if token is not None and isinstance(actual, str):
        return token.group(1) == actual, token.group(1)

    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        number = _NUMBER.search(documented)
        if number is None:
            raise ValueError("no numeric value")
        return float(number.group(0)) == float(actual), number.group(0)

    raise ValueError(f"cannot interpret {cells[1]!r} against {type(actual).__name__}")


def _short(value: Any, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def check(
    doc_path: Path,
    config_path: Path,
    root: Path = DEFAULT_ROOT,
    *,
    allow_imports: bool = False,
) -> tuple[list[str], list[str], int]:
    """Return ``(errors, deferred, checked)``; ``deferred`` lists rows that need imports."""
    errors: list[str] = []
    deferred: list[str] = []
    doc_text = doc_path.read_text(encoding="utf-8")
    try:
        rows = _table_rows(doc_text, HEADING)
    except ValueError as exc:
        return [f"{doc_path}: {exc}"], [], 0

    config_resolved = config_path.resolve()
    module_cache: dict[Path, dict[str, Any]] = {}
    checked = 0

    for cells in rows:
        if len(cells) < 3:
            continue
        name_match = _BACKTICKED_NAME.search(cells[0])
        if name_match is None:
            errors.append(f"{doc_path}: row {cells[0]!r} does not name a constant in backticks")
            continue
        name = name_match.group(1)

        sources = _SOURCE_FILE.findall(cells[2])
        if not sources:
            errors.append(f"{doc_path}: `{name}` does not name its source file in the location column")
            continue
        module_rel = sources[0]
        module_path = (root / module_rel).resolve()

        try:
            if module_path not in module_cache:
                module_cache[module_path] = (
                    _config_defaults(module_path) if module_path == config_resolved else _module_values(module_path)
                )
            if module_path == config_resolved:
                defaults = module_cache[module_path]
                if name not in defaults:
                    errors.append(f"{module_rel}: `{name}` is documented but has no default there")
                    continue
                actual = defaults[name]
            else:
                values = module_cache[module_path]
                if name in values:
                    actual = values[name]
                elif allow_imports:
                    actual = _import_value(module_rel, name, root)
                else:
                    deferred.append(f"{name} ({module_rel})")
                    continue
        except (OSError, Unresolved) as exc:
            errors.append(f"{doc_path}: `{name}` cannot be resolved from {module_rel}: {exc}")
            continue

        try:
            matches, documented = _compare(cells, actual)
        except (TypeError, ValueError) as exc:
            errors.append(f"{doc_path}: `{name}` cannot be checked: {exc}")
            continue
        if not matches:
            errors.append(f"{doc_path}: `{name}` documents {documented} but {module_rel} has {_short(actual)}")
        checked += 1

    if checked == 0 and not errors:
        errors.append(
            f"{doc_path}: no constants could be checked; the table may have been renamed or reformatted"
        )
    if allow_imports and deferred:
        errors.extend(f"{doc_path}: `{row}` needs an import that failed" for row in deferred)
        deferred = []
    return errors, deferred, checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc", type=Path, default=DEFAULT_DOC, help="reference document to read")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="settings module")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="directory for module paths")
    parser.add_argument(
        "--allow-imports",
        action="store_true",
        help="import modules for constants that cannot be read statically (needs project dependencies)",
    )
    args = parser.parse_args(argv)

    errors, deferred, checked = check(args.doc, args.config, args.root, allow_imports=args.allow_imports)
    for row in deferred:
        print(f"DEFERRED (needs --allow-imports): {row}")
    for error in errors:
        print(f"DRIFT: {error}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} constant(s) out of sync.", file=sys.stderr)
        return 1
    print(f"OK: {checked} documented constants match their sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
