#!/usr/bin/env python3
"""Fail when the constants table in ``docs/EDITORIAL_PIPELINE.md`` drifts from ``app/config.py``.

The reference doc is the place where operators read pipeline constants, and an outdated
table is worse than no table: it silently tells a reviewer that a 30-second timeout is a
6-second one. This checker reads the documented value for every row whose location is
``app/config.py`` and compares it with the numeric default in the ``Settings`` class.

Only the standard library is used, so the check runs without installing the project's
dependencies and without constructing ``settings`` (which would require environment
variables and could apply validators on top of the raw default).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

DEFAULT_DOC = Path("docs/EDITORIAL_PIPELINE.md")
DEFAULT_CONFIG = Path("app/config.py")

HEADING = "## ۱۰. جدول ثابت‌های کلیدی"
CONFIG_LOCATION = "app/config.py"

_PERSIAN_AND_ARABIC_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫",
    "01234567890123456789.",
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BACKTICKED_NAME = re.compile(r"`([a-z][a-z0-9_]*)`")


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


def _config_defaults(config_path: Path) -> dict[str, int | float]:
    """Numeric literal defaults of every annotated class attribute in ``config_path``."""
    tree = ast.parse(config_path.read_text(encoding="utf-8"))
    defaults: dict[str, int | float] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or statement.value is None:
                continue
            if not isinstance(statement.target, ast.Name):
                continue
            try:
                value = ast.literal_eval(statement.value)
            except ValueError:
                continue  # computed defaults are not part of this check
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                defaults[statement.target.id] = value
    return defaults


def check(doc_path: Path, config_path: Path) -> list[str]:
    errors: list[str] = []
    defaults = _config_defaults(config_path)

    try:
        rows = _table_rows(doc_path.read_text(encoding="utf-8"), HEADING)
    except (OSError, ValueError) as exc:
        return [f"{doc_path}: {exc}"]

    checked = 0
    for cells in rows:
        if len(cells) < 3 or CONFIG_LOCATION not in cells[2]:
            continue
        name_match = _BACKTICKED_NAME.search(cells[0])
        if name_match is None:
            errors.append(f"{doc_path}: row {cells[0]!r} does not name a setting in backticks")
            continue
        name = name_match.group(1)
        if name not in defaults:
            errors.append(f"{doc_path}: `{name}` has no numeric default in {config_path}")
            continue
        documented_match = _NUMBER.search(_to_ascii_digits(cells[1]))
        if documented_match is None:
            errors.append(f"{doc_path}: `{name}` has no numeric value in its row")
            continue
        documented = float(documented_match.group(0))
        actual = float(defaults[name])
        if documented != actual:
            errors.append(
                f"{doc_path}: `{name}` documents {documented_match.group(0)} "
                f"but {config_path} defaults to {defaults[name]}"
            )
        checked += 1

    if checked == 0 and not errors:
        errors.append(
            f"{doc_path}: no rows located in {CONFIG_LOCATION} were found; "
            "the constants table may have been renamed or reformatted"
        )
    if errors:
        return errors
    print(f"OK: {checked} documented constants match {config_path}")
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc", type=Path, default=DEFAULT_DOC, help="reference document to read")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="settings module to compare against")
    args = parser.parse_args(argv)

    errors = check(args.doc, args.config)
    for error in errors:
        print(f"DRIFT: {error}", file=sys.stderr)
    if errors:
        print(f"\n{len(errors)} constant(s) out of sync.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
