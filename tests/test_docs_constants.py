"""The editorial pipeline reference must not drift from the code.

This runs the same check as the CI ``Docs (constants drift)`` job and its
``--allow-imports`` step, so a stale constant in ``docs/EDITORIAL_PIPELINE.md``
also shows up in the test report next to the behaviour it describes.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_doc_constants import check

ROOT = Path(__file__).resolve().parents[1]

# Ten settings from ``app/config.py`` plus PROMPT_VERSION, MIDDLE_EAST_OFFICIALS,
# COUNT_ONLY_STATUSES, MAX_CONTENT_BYTES and PAGE_CONTENT_BUDGET_BYTES.
DOCUMENTED_CONSTANTS = 15


def test_documented_constants_match_their_sources():
    errors, deferred, checked = check(
        ROOT / "docs" / "EDITORIAL_PIPELINE.md",
        ROOT / "app" / "config.py",
        ROOT,
        allow_imports=True,
    )

    assert not errors, "\n".join(errors)
    assert not deferred
    assert checked == DOCUMENTED_CONSTANTS, (
        "the constants table changed size; update DOCUMENTED_CONSTANTS together with the "
        "table so a removed row cannot pass unnoticed"
    )
