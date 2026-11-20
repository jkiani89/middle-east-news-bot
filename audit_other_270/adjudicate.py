#!/usr/bin/env python3
"""Blind adjudication harness for the 270-event classification audit (ISS-036).

Reads ONLY the CSV files written by the DBA read-only export
(``export_other_270.sql``). It never connects to a database and never modifies
project data.

    python audit_other_270/adjudicate.py build  --bundle DIR --out DIR
    python audit_other_270/adjudicate.py check  --sheet S.csv --judgments J.csv
    python audit_other_270/adjudicate.py reveal --sheet S.csv --judgments J.csv \
        --holdout H.csv --out DIR

Blindness contract: ``build`` writes the system label only to
``labels_holdout.csv``; ``blind_sheet.csv`` never contains it. ``reveal`` is the
only command that opens the holdout file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.editorial.topics import match_topics  # noqa: E402

CLASSES = ("A", "B", "C", "D", "E")
CONFIDENCES = ("High", "Medium", "Low")
POPULATION_LABEL = "other"
SNIPPET_CHARS = 500

BLIND_COLUMNS = [
    "case_id",
    "case_type",
    "event_id",
    "title",
    "countries",
    "actors",
    "location",
    "importance",
    "status",
    "event_type",
    "first_seen_at",
    "article_count",
    "article_ids",
    "sources",
    "article_urls",
    "article_times",
    "article_statuses",
    "text_evidence",
    "summary_headline_fa",
    "verification_status",
    "empty_fields",
    "independent_class",
    "probable_topic",
    "decision_reason",
    "confidence",
    "supporting_article_ids",
    "notes",
]
JUDGMENT_COLUMNS = [
    "case_id",
    "independent_class",
    "probable_topic",
    "decision_reason",
    "confidence",
    "supporting_article_ids",
    "notes",
]
HOLDOUT_COLUMNS = ["case_id", "event_id", "system_topics", "article_count"]
SEALED_DIRNAME = "sealed"
REQUIRED_COLUMNS = {
    "events.csv": {
        "id",
        "title",
        "event_type",
        "countries",
        "actors",
        "location",
        "importance",
        "status",
        "first_seen_at",
    },
    "articles.csv": {
        "id",
        "title",
        "source_name",
        "original_url",
        "published_at",
        "collected_at",
        "status",
        "original_text",
    },
    "article_events.csv": {"article_id", "event_id"},
    "summaries.csv": {"event_id", "headline_fa", "verification_status", "model_name"},
}


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _cell(row: dict[str, str], key: str) -> str:
    return (row.get(key) or "").strip()


def _json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [raw]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _require_columns(path: Path, required: set[str]) -> None:
    with path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), [])
    missing = sorted(required - set(header))
    if missing:
        raise ValueError(f"{path.name}: missing required column(s): {', '.join(missing)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _empty_fields(countries: list[str], actors: list[str], location: str) -> str:
    missing = []
    if not countries:
        missing.append("countries")
    if not actors:
        missing.append("actors")
    if not location.strip():
        missing.append("location")
    return ",".join(missing)


def _write(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _article_columns(row: dict[str, str]) -> dict[str, str]:
    return {
        "title": _cell(row, "title"),
        "sources": _cell(row, "source_name"),
        "article_urls": _cell(row, "original_url"),
        "article_times": _cell(row, "collected_at"),
        "article_statuses": _cell(row, "status"),
        "text_evidence": _cell(row, "original_text")[:SNIPPET_CHARS].replace("\n", " "),
    }


def build(bundle: Path, out: Path) -> dict:
    """Turn the raw DBA export into a blind sheet plus a sealed label file.

    Raises ``ValueError`` when the bundle is incomplete or internally inconsistent;
    a partially usable bundle must never become a blind sheet.
    """
    for name, required in REQUIRED_COLUMNS.items():
        path = bundle / name
        if not path.exists():
            raise ValueError(f"{name}: missing from the bundle")
        _require_columns(path, required)

    events = _rows(bundle / "events.csv")
    article_rows = _rows(bundle / "articles.csv")
    articles = {int(_cell(row, "id")): row for row in article_rows}
    links = _rows(bundle / "article_events.csv")
    summaries = _rows(bundle / "summaries.csv")
    event_ids = {int(_cell(row, "id")) for row in events}

    if len(event_ids) != len(events) or len(articles) != len(article_rows):
        raise ValueError("bundle contains duplicate event or article ids")

    dangling = [
        f"{_cell(link, 'article_id')}->{_cell(link, 'event_id')}"
        for link in links
        if int(_cell(link, "article_id")) not in articles
        or int(_cell(link, "event_id")) not in event_ids
    ]
    if dangling:
        raise ValueError(f"bundle has {len(dangling)} dangling link(s): {dangling[:5]}")

    per_event: dict[int, list[int]] = {}
    for link in links:
        per_event.setdefault(int(_cell(link, "event_id")), []).append(
            int(_cell(link, "article_id"))
        )

    summary_by_event: dict[int, dict[str, str]] = {}
    for row in summaries:
        summary_by_event.setdefault(int(_cell(row, "event_id")), row)

    linked = {article_id for ids in per_event.values() for article_id in ids}

    blind: list[dict[str, str]] = []
    holdout: list[dict[str, str]] = []
    excluded = 0

    for row in events:
        event_id = int(_cell(row, "id"))
        countries = _json_list(row.get("countries"))
        actors = _json_list(row.get("actors"))
        location = _cell(row, "location")
        title = _cell(row, "title")

        topics = [
            topic.key for topic in match_topics(countries, actors, location, title)
        ] or [POPULATION_LABEL]
        if topics != [POPULATION_LABEL]:
            excluded += 1
            continue

        case_id = f"E{event_id}"
        article_ids = per_event.get(event_id, [])
        supporting = [articles[i] for i in article_ids if i in articles]
        summary = summary_by_event.get(event_id, {})

        blind.append(
            {
                "case_id": case_id,
                "case_type": "event_other",
                "event_id": str(event_id),
                "title": title,
                "countries": "; ".join(countries),
                "actors": "; ".join(actors),
                "location": location,
                "importance": _cell(row, "importance"),
                "status": _cell(row, "status"),
                "event_type": _cell(row, "event_type"),
                "first_seen_at": _cell(row, "first_seen_at"),
                "article_count": str(len(supporting)),
                "article_ids": ";".join(str(i) for i in article_ids),
                "sources": "; ".join(_cell(a, "source_name") for a in supporting),
                "article_urls": "; ".join(_cell(a, "original_url") for a in supporting),
                "article_times": "; ".join(_cell(a, "collected_at") for a in supporting),
                "article_statuses": "; ".join(_cell(a, "status") for a in supporting),
                "text_evidence": " || ".join(
                    _cell(a, "original_text")[:SNIPPET_CHARS].replace("\n", " ")
                    for a in supporting
                ),
                "summary_headline_fa": _cell(summary, "headline_fa"),
                "verification_status": _cell(summary, "verification_status"),
                "empty_fields": _empty_fields(countries, actors, location),
                "independent_class": "",
                "probable_topic": "",
                "decision_reason": "",
                "confidence": "",
                "supporting_article_ids": "",
                "notes": "",
            }
        )
        holdout.append(
            {
                "case_id": case_id,
                "event_id": str(event_id),
                "system_topics": POPULATION_LABEL,
                "article_count": str(len(supporting)),
            }
        )

    orphans: list[dict[str, str]] = []
    for article_id in sorted(articles):
        if article_id in linked:
            continue
        row = articles[article_id]
        orphans.append(
            {
                "case_id": f"A{article_id}",
                "case_type": "article_no_event",
                "event_id": "",
                "importance": "",
                "article_count": "1",
                "article_ids": str(article_id),
                "summary_headline_fa": "",
                "verification_status": "",
                "empty_fields": "",
                "independent_class": "",
                "probable_topic": "",
                "decision_reason": "",
                "confidence": "",
                "supporting_article_ids": "",
                "notes": "",
                **_article_columns(row),
            }
        )

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "counts": {
            "events_total": len(events),
            "events_in_other_population": len(blind),
            "events_excluded_not_other": excluded,
            "articles_total": len(articles),
            "articles_without_event": len(orphans),
        },
        "input_hashes": {
            name: _sha256(bundle / name)
            for name in ("events.csv", "articles.csv", "article_events.csv", "summaries.csv")
            if (bundle / name).exists()
        },
        "holdout_path": f"{SEALED_DIRNAME}/labels_holdout.csv",
        "data_checks": {
            "events_rows": len(events),
            "articles_rows": len(article_rows),
            "links_rows": len(links),
            "summaries_rows": len(summaries),
            "dangling_links": 0,
        },
        "warning": (
            f"{SEALED_DIRNAME}/labels_holdout.csv must stay unopened until "
            "judgments.csv passes check"
        ),
    }

    _write(out / "blind_sheet.csv", BLIND_COLUMNS, blind)
    _write(out / "blind_sheet_articles.csv", BLIND_COLUMNS, orphans)
    _write(out / SEALED_DIRNAME / "labels_holdout.csv", HOLDOUT_COLUMNS, holdout)
    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return meta


def check(sheet: Path, judgments: Path) -> list[str]:
    """Return every completeness/validity problem in the judgment file."""
    cases = [_cell(row, "case_id") for row in _rows(sheet)]
    known = set(cases)
    problems: list[str] = []
    seen: set[str] = set()

    for row in _rows(judgments):
        case_id = _cell(row, "case_id")
        if case_id not in known:
            problems.append(f"{case_id}: not in the blind sheet")
            continue
        if case_id in seen:
            problems.append(f"{case_id}: duplicate judgment")
        seen.add(case_id)

        klass = _cell(row, "independent_class")
        confidence = _cell(row, "confidence")
        if klass not in CLASSES:
            problems.append(f"{case_id}: invalid independent_class {klass!r}")
        if confidence not in CONFIDENCES:
            problems.append(f"{case_id}: invalid confidence {confidence!r}")
        if klass == "B" and not _cell(row, "probable_topic"):
            problems.append(f"{case_id}: class B needs probable_topic")

    problems.extend(f"{case_id}: missing judgment" for case_id in cases if case_id not in seen)
    return problems


def reveal(sheet: Path, judgments: Path, holdout: Path, out: Path) -> dict:
    """Merge judgments with the sealed labels and write the audit report.

    Refuses to reveal anything until ``check`` passes: an incomplete judgment file
    must not be able to open the labels.
    """
    problems = check(sheet, judgments)
    if problems:
        raise ValueError("judgments are not ready: " + "; ".join(problems[:5]))

    holdout_rows = {_cell(row, "case_id"): row for row in _rows(holdout)}
    judgment_rows = {_cell(row, "case_id"): row for row in _rows(judgments)}

    counts = {"correct": 0, "definite_error": 0, "uncertain": 0}
    report: list[dict[str, str]] = []

    for row in _rows(sheet):
        case_id = _cell(row, "case_id")
        judgment = judgment_rows.get(case_id)
        if judgment is None:
            continue
        klass = _cell(judgment, "independent_class")
        system_topics = _cell(holdout_rows.get(case_id, {}), "system_topics")
        if klass == "A":
            verdict = "correct"
        elif klass == "B":
            verdict = "definite_error"
        else:
            verdict = "uncertain"
        counts[verdict] += 1
        report.append(
            {
                "case_id": case_id,
                "case_type": _cell(row, "case_type"),
                "title": _cell(row, "title"),
                "countries": _cell(row, "countries"),
                "actors": _cell(row, "actors"),
                "location": _cell(row, "location"),
                "system_topics": system_topics,
                "independent_class": klass,
                "probable_topic": _cell(judgment, "probable_topic"),
                "confidence": _cell(judgment, "confidence"),
                "verdict": verdict,
                "decision_reason": _cell(judgment, "decision_reason"),
                "article_ids": _cell(row, "article_ids"),
                "article_urls": _cell(row, "article_urls"),
                "notes": _cell(judgment, "notes"),
            }
        )

    columns = list(report[0]) if report else ["case_id", "verdict"]
    _write(out / "audit_report.csv", columns, report)
    _write(
        out / "definite_errors.csv",
        columns,
        [row for row in report if row["verdict"] == "definite_error"],
    )
    _write(
        out / "uncertain.csv",
        columns,
        [row for row in report if row["verdict"] == "uncertain"],
    )
    return {"counts": counts, "judged": len(report)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build_parser = sub.add_parser("build", help="raw bundle -> blind sheet + sealed labels")
    build_parser.add_argument("--bundle", type=Path, required=True)
    build_parser.add_argument("--out", type=Path, required=True)

    check_parser = sub.add_parser("check", help="validate judgments against the blind sheet")
    check_parser.add_argument("--sheet", type=Path, required=True)
    check_parser.add_argument("--judgments", type=Path, required=True)

    reveal_parser = sub.add_parser("reveal", help="merge judgments with the sealed labels")
    reveal_parser.add_argument("--sheet", type=Path, required=True)
    reveal_parser.add_argument("--judgments", type=Path, required=True)
    reveal_parser.add_argument("--holdout", type=Path, required=True)
    reveal_parser.add_argument("--out", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "build":
        meta = build(args.bundle, args.out)
        print(json.dumps(meta["counts"], ensure_ascii=False))
        print(
            "[build] blind_sheet.csv written; the labels are sealed in "
            f"{SEALED_DIRNAME}/labels_holdout.csv and must stay unopened"
        )
        return 0

    if args.command == "check":
        problems = check(args.sheet, args.judgments)
        for problem in problems:
            print(f"[check] {problem}", file=sys.stderr)
        print(f"[check] {len(problems)} problem(s)")
        return 1 if problems else 0

    try:
        result = reveal(args.sheet, args.judgments, args.holdout, args.out)
    except ValueError as exc:
        print(f"[reveal] refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
