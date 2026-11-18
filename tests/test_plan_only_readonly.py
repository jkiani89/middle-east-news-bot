"""PLAN_ONLY must never write, publish, reach the network or touch Telegram.

The test runs the real backfill script in a subprocess with ``PLAN_ONLY=true`` and a
recording database session plus hard failures installed wherever a side effect could
happen, so an accidental write, page create or Telegram send fails loudly instead of
silently reaching production.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "_backfill_phase12.py"
PROJECT_ROOT = SCRIPT.parents[1]
EXPECTED_SLOTS = [
    "2026-11-15 08:00",
    "2026-11-16 20:00",
    "2026-11-17 08:00",
    "2026-11-17 20:00",
    "2026-11-18 08:00",
]

_STUB = '''
"""Run the backfill script in PLAN_ONLY mode with every side effect trapped."""
import json
import os
import runpy
import socket
import sys
from pathlib import Path

ROOT = Path(os.environ["PROJECT_ROOT"])
SCRIPT = Path(os.environ["SCRIPT_PATH"])
TRACE = Path("plan_only_trace.json")
sys.path.insert(0, str(ROOT))

trace = {"statements": [], "writes": [], "forbidden": []}


class Result:
    def scalar_one_or_none(self):
        return None

    def scalar(self):
        return None

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter(())


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def execute(self, stmt, *args, **kwargs):
        trace["statements"].append(" ".join(str(stmt).split()))
        return Result()

    def add(self, obj):
        trace["writes"].append("add")

    async def delete(self, obj):
        trace["writes"].append("delete")

    async def flush(self):
        trace["writes"].append("flush")

    async def commit(self):
        trace["writes"].append("commit")

    async def rollback(self):
        trace["writes"].append("rollback")


def forbid(kind):
    def _fail(*args, **kwargs):
        trace["forbidden"].append(kind)
        raise AssertionError("PLAN_ONLY must not call " + kind)

    return _fail


import httpx  # noqa: E402

httpx.AsyncClient.request = forbid("httpx.request")
httpx.AsyncClient.send = forbid("httpx.send")
httpx.request = forbid("httpx.request")
socket.socket.connect = forbid("socket.connect")

import app.database.connection as connection  # noqa: E402
import app.reports.telegraph_client as telegraph_client  # noqa: E402
import app.reports.telegraph_report as report_module  # noqa: E402

connection.SessionLocal = Session
report_module.publish_telegraph_report = forbid("publish_telegraph_report")
report_module.build_client = forbid("build_client")
telegraph_client.build_client = forbid("build_client")


def item(event_id, topic_key="ir"):
    return {
        "event_id": event_id,
        "topic_key": topic_key,
        "topic_fa": "ایران و آمریکا" if topic_key == "ir" else "عربستان و یمن",
        "headline_fa": "تیتر خبر شماره " + str(event_id),
        "summary_fa": "خلاصهٔ خبر " * 20,
        "primary_source": "IRNA",
        "primary_url": "https://irna.ir/news/" + str(event_id),
        "first_seen_at": "2026-11-17T19:30:00+00:00",
        "verification_status": "unconfirmed",
        "verification_status_fa": "❓ تأییدنشده (تک‌منبعی)",
        "additional_sources": [],
    }


async def fake_items(session, *, report_date, slot_hour, **kwargs):
    return report_date, [item(1), item(2), item(3), item(100, "ye")], {}


report_module.fetch_complete_window_items = fake_items

before_telegram = sorted(m for m in sys.modules if m.startswith("app.telegram"))
before_scheduler = sorted(m for m in sys.modules if m.startswith("app.scheduler"))
try:
    runpy.run_path(str(SCRIPT), run_name="__main__")
finally:
    trace["telegram_modules"] = sorted(m for m in sys.modules if m.startswith("app.telegram"))
    trace["scheduler_modules"] = sorted(m for m in sys.modules if m.startswith("app.scheduler"))
    trace["before_telegram"] = before_telegram
    trace["before_scheduler"] = before_scheduler
    TRACE.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
'''


def _run_plan_only(tmp_path: Path) -> subprocess.CompletedProcess:
    stub = tmp_path / "plan_only_stub.py"
    stub.write_text(_STUB, encoding="utf-8")
    env = {
        **os.environ,
        "PLAN_ONLY": "true",
        "PROJECT_ROOT": str(PROJECT_ROOT),
        "SCRIPT_PATH": str(SCRIPT),
        "PYTHONPATH": str(PROJECT_ROOT),
    }
    for name in ("SLOT_DATE", "SLOT_HOUR", "DRY_RUN", "FORCE", "PERSIST", "CHECK_ONLY"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, str(stub)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_plan_only_performs_no_write_network_or_telegram_call(tmp_path):
    completed = _run_plan_only(tmp_path)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PLAN_ONLY_MODE" in completed.stdout
    assert "PLAN_ONLY_DONE" in completed.stdout
    # main() returned immediately: the publishing path never ran.
    assert "ALL_DONE" not in completed.stdout

    trace = json.loads((tmp_path / "plan_only_trace.json").read_text(encoding="utf-8"))
    assert trace["forbidden"] == []
    assert trace["writes"] == []
    assert trace["statements"], "plan_only should read the slot row"
    assert all(text.upper().startswith("SELECT") for text in trace["statements"])
    # No Telegram or scheduler code was even imported as a side effect of the run.
    assert trace["telegram_modules"] == trace["before_telegram"]
    assert trace["scheduler_modules"] == trace["before_scheduler"]

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "plan_only_report.json",
        "plan_only_stub.py",
        "plan_only_trace.json",
    ]

    report = json.loads((tmp_path / "plan_only_report.json").read_text(encoding="utf-8"))
    assert [row["slot"] for row in report] == EXPECTED_SLOTS
    for row in report:
        assert row["planned_pages_with_index"] == row["planned_parts"] + 1
        assert row["item_count"] == 4
        assert row["failures"] == []
        assert row["stored_status"] is None
        assert row["stored_topic_keys"] == []
        assert row["topics"] and all(topic["items"] for topic in row["topics"])
