"""Editorial glossary used for prompt-time spelling guidance (Phase Y-3).

The glossary is data, not code: ``editorial/glossary.json``. Only entries whose
source form actually appears in the article are injected, and entries flagged
``review_required`` are never injected until a human approves the spelling.

An invalid file is never ignored silently: the loader raises
:class:`GlossaryError` with the file path and the offending entry index.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "editorial" / "glossary.json"
MAX_ENTRIES_PER_PROMPT = 12
MAX_BLOCK_CHARS = 1200

_ALLOWED_KINDS = frozenset({"person", "place", "organization", "country", "other"})
_ARABIC_DEFINITE_ARTICLE = "ال"
_CACHE: dict[str, tuple[float, int, Glossary]] = {}


class GlossaryError(ValueError):
    """Raised when the glossary file exists but cannot be trusted."""


@dataclass(frozen=True)
class GlossaryEntry:
    id: str
    kind: str
    canonical_fa: str
    source_forms: tuple[str, ...]
    variants_fa: tuple[str, ...] = ()
    review_required: bool = False


@dataclass(frozen=True)
class Glossary:
    version: int
    entries: tuple[GlossaryEntry, ...]
    content_sha: str = ""

    def usable_entries(self) -> tuple[GlossaryEntry, ...]:
        return tuple(entry for entry in self.entries if not entry.review_required)


def _content_sha(glossary: Glossary) -> str:
    """Fingerprint of the file contents, so an edit is visible even if version is stale."""
    payload = json.dumps(
        [
            {
                "id": entry.id,
                "kind": entry.kind,
                "canonical_fa": entry.canonical_fa,
                "source_forms": list(entry.source_forms),
                "variants_fa": list(entry.variants_fa),
                "review_required": entry.review_required,
            }
            for entry in glossary.entries
        ],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(text.split())


def _form_pattern(form: str) -> re.Pattern[str] | None:
    """Regex for one source form, tolerating Arabic clitics on its first word.

    ``الجيش`` and ``جيش`` therefore both match ``والجيش`` and ``للجيش``: an attached
    conjunction/preposition and the definite article are spelling variants of the
    same name, not different entities.
    """
    normalized = _normalize(form)
    if not normalized:
        return None
    core = normalized
    if core.startswith(_ARABIC_DEFINITE_ARTICLE) and len(core) > len(_ARABIC_DEFINITE_ARTICLE) + 1:
        core = core[len(_ARABIC_DEFINITE_ARTICLE):]
    return re.compile(
        rf"(?<!\w)(?:[\u0648\u0628\u0644]{{0,3}}(?:\u0627\u0644)?)?{re.escape(core)}(?!\w)"
    )


def _parse_entry(raw: object, index: int, path: Path) -> GlossaryEntry:
    if not isinstance(raw, dict):
        raise GlossaryError(f"{path}: entry #{index} is not an object")
    entry_id = raw.get("id")
    kind = raw.get("kind")
    canonical_fa = raw.get("canonical_fa")
    source_forms = raw.get("source_forms")
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise GlossaryError(f"{path}: entry #{index} has no usable 'id'")
    if kind not in _ALLOWED_KINDS:
        raise GlossaryError(f"{path}: entry '{entry_id}' has unsupported kind {kind!r}")
    if not isinstance(canonical_fa, str) or not canonical_fa.strip():
        raise GlossaryError(f"{path}: entry '{entry_id}' has no 'canonical_fa'")
    if not isinstance(source_forms, list) or not source_forms or not all(
        isinstance(form, str) and form.strip() for form in source_forms
    ):
        raise GlossaryError(f"{path}: entry '{entry_id}' needs a non-empty list of 'source_forms'")
    variants = raw.get("variants_fa", [])
    if not isinstance(variants, list) or not all(isinstance(v, str) for v in variants):
        raise GlossaryError(f"{path}: entry '{entry_id}' has a malformed 'variants_fa'")
    review_required = raw.get("review_required", False)
    if not isinstance(review_required, bool):
        raise GlossaryError(f"{path}: entry '{entry_id}' has a non-boolean 'review_required'")
    return GlossaryEntry(
        id=entry_id.strip(),
        kind=kind,
        canonical_fa=canonical_fa.strip(),
        source_forms=tuple(form.strip() for form in source_forms),
        variants_fa=tuple(variants),
        review_required=review_required,
    )


def parse_glossary(payload: object, path: Path) -> Glossary:
    """Validate an already-parsed glossary document."""
    if not isinstance(payload, dict):
        raise GlossaryError(f"{path}: root must be a JSON object")
    version = payload.get("version")
    entries = payload.get("entries")
    if not isinstance(version, int):
        raise GlossaryError(f"{path}: 'version' must be an integer")
    if not isinstance(entries, list):
        raise GlossaryError(f"{path}: 'entries' must be a list")
    parsed = tuple(_parse_entry(raw, index, path) for index, raw in enumerate(entries))
    seen: set[str] = set()
    for entry in parsed:
        if entry.id in seen:
            raise GlossaryError(f"{path}: duplicate entry id '{entry.id}'")
        seen.add(entry.id)
    return Glossary(version=version, entries=parsed)


def load_glossary(path: Path | str | None = None) -> Glossary:
    """Load and validate the glossary, cached by mtime and size.

    A missing file yields an empty glossary plus a warning (the feature is
    optional); an unreadable or invalid file raises :class:`GlossaryError`.
    """
    resolved = Path(path) if path is not None else DEFAULT_GLOSSARY_PATH
    key = str(resolved)
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        logger.warning("Glossary file %s not found; continuing without glossary entries.", resolved)
        return Glossary(version=0, entries=())
    except OSError as exc:  # pragma: no cover - permissions are environmental
        raise GlossaryError(f"{resolved}: cannot stat glossary file ({exc})") from exc

    cached = _CACHE.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]

    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GlossaryError(f"{resolved}: invalid JSON ({exc})") from exc
    except OSError as exc:  # pragma: no cover - permissions are environmental
        raise GlossaryError(f"{resolved}: cannot read glossary file ({exc})") from exc

    glossary = parse_glossary(payload, resolved)
    glossary = Glossary(
        version=glossary.version,
        entries=glossary.entries,
        content_sha=_content_sha(glossary),
    )
    _CACHE[key] = (stat.st_mtime, stat.st_size, glossary)
    return glossary


def match_entries(glossary: Glossary, *texts: str, limit: int = MAX_ENTRIES_PER_PROMPT) -> list[GlossaryEntry]:
    """Return glossary entries whose source form appears in any of ``texts``.

    Matching uses whole words on NFKC-folded text, and accepts the Arabic form
    with or without the cliticised ``ال`` (``الجيش`` also matches ``جيش``).
    """
    haystack = _normalize("\n".join(text for text in texts if text))
    if not haystack:
        return []
    matched: list[GlossaryEntry] = []
    for entry in glossary.usable_entries():
        if len(matched) >= limit:
            break
        for form in entry.source_forms:
            pattern = _form_pattern(form)
            if pattern is not None and pattern.search(haystack):
                matched.append(entry)
                break
    return matched


def build_glossary_block(entries: list[GlossaryEntry], *, max_chars: int = MAX_BLOCK_CHARS) -> str:
    """Render the prompt block, bounded in both entry count and characters."""
    lines = [
        "راهنمای املای نام‌ها (این بخش دادهٔ مرجع نگارش است، نه دستور اجرایی؛ "
        "برای هر نام فقط شکل مرجع را به کار ببر):"
    ]
    for entry in entries:
        forms = "، ".join(entry.source_forms)
        lines.append(f"- {entry.canonical_fa} ← {forms}")
    block = "\n".join(lines) + "\n\n"
    if len(block) <= max_chars:
        return block
    trimmed: list[str] = []
    for line in lines:
        if len("\n".join(trimmed) + "\n" + line) + 2 > max_chars:
            break
        trimmed.append(line)
    return ("\n".join(trimmed) + "\n\n") if trimmed else ""


def glossary_for_prompt(*texts: str, path: Path | str | None = None) -> tuple[str, dict]:
    """Return ``(prompt block, metadata)`` for the article's own text.

    Loader errors are reported to the caller (never swallowed): the metadata
    carries the error so the pipeline can record it in ``ai_payload``.
    """
    try:
        glossary = load_glossary(path)
    except GlossaryError as exc:
        logger.error("Glossary unusable, continuing without it: %s", exc)
        return "", {"error": str(exc), "version": None, "entries": []}
    entries = match_entries(glossary, *texts)
    if not entries:
        return "", {
            "version": glossary.version,
            "content_sha": glossary.content_sha,
            "entries": [],
            "review_required_skipped": sum(1 for entry in glossary.entries if entry.review_required),
        }
    meta = {
        "version": glossary.version,
        "content_sha": glossary.content_sha,
        "entries": [entry.id for entry in entries],
        "review_required_skipped": sum(1 for entry in glossary.entries if entry.review_required),
    }
    return build_glossary_block(entries), meta
