"""Editorial newsworthiness gate: no sport, tourism or routine-incident items.

The digest covers politics, security, diplomacy and the economy of the region. A
league result, a holiday-resort promotion or a highway pile-up can still name a
regional country and clear the geography gate, yet carry no political value for the
reader; the 2026-11-21 export showed a cricket tournament in Doha and a children's
entertainment programme in AlUla reaching the regional briefing as soon as their
importance reached 2.

The gate is lexical on purpose: the model's ten category codes have no sport,
tourism or accident code, so the analysed headline, summary and actors are the only
evidence available. Suppression needs two conditions at once — the story is inside
our geography at all, and no political, security or economic term or actor appears
anywhere in it. That second condition keeps politically loaded sport (a boycott, an
arrest, a defection) publishable, which is why this is a veto list rather than an
allowlist of "safe" words.
"""

from __future__ import annotations

from app.editorial.regions import _name_appears, has_middle_east_nexus, normalize_place

SPORT = "sport"
TOURISM = "tourism"
INCIDENT = "incident"


def _markers(*words: str) -> tuple[str, ...]:
    """Normalize marker words the same way analysed text is normalized."""
    return tuple(dict.fromkeys(normalize_place(word) for word in words if word.strip()))


# A result, a fixture or a federation — not a political use of the words.
SPORTS_MARKERS = _markers(
    # Persian
    "ورزش", "ورزشی", "ورزشکار", "ورزشگاه", "فوتبال", "والیبال", "بسکتبال",
    "کریکت", "المپیک", "پارالمپیک", "فدراسیون فوتبال", "فدراسیون کشتی",
    "فدراسیون والیبال", "مسابقه", "مسابقات", "لیگ", "جام جهانی", "جام ملت‌ها",
    "قهرمانی", "قهرمان", "مدال", "دربی", "هوادار", "مربی", "بازیکن", "تیم ملی",
    "دور نهایی", "نیمه‌نهایی", "وزنه‌برداری", "تکواندو", "دوومیدانی", "شطرنج",
    # English
    "football", "soccer", "cricket", "basketball", "volleyball", "olympic",
    "paralympic", "tournament", "world cup", "stadium", "league match", "athlete",
    "fifa", "uefa",
)

TOURISM_MARKERS = _markers(
    # Persian
    "گردشگری", "گردشگر", "توریسم", "توریست", "جاذبه گردشگری", "اقامتگاه",
    "استراحتگاه", "تفرجگاه", "هتل", "تفریح", "تفریحی", "سفر تفریحی",
    "نمایشگاه گردشگری", "پارک آبی", "کمپینگ",
    # English
    "tourism", "tourist", "resort", "hotel", "holidaymakers", "water park",
)

# Routine local accidents and crime-free incidents. Mass-casualty disasters
# (earthquake, flood, plane crash) are deliberately absent: those are briefable.
INCIDENT_MARKERS = _markers(
    # Persian
    "تصادف", "سانحه رانندگی", "آتش‌سوزی", "ریزش ساختمان", "غرق شدن",
    "مسمومیت", "حادثه کارگاه", "انفجار گاز شهری", "سرقت", "دزدی",
    # English
    "car crash", "road accident", "traffic accident", "building collapse",
    "drowning", "gas explosion", "burglary",
)

# Any of these anywhere in the analysed story means it is a political, security or
# economic item that merely borrows a sport/tourism/incident word. The veto wins.
POLITICAL_OVERRIDE_MARKERS = _markers(
    # Persian
    "انتخابات", "پارلمان", "مجلس", "وزیر", "وزارت", "دولت", "دیپلمات",
    "سفارت", "سفیر", "تحریم", "مذاکره", "مذاکرات", "توافق", "آتش‌بس", "حمله",
    "موشک", "پهپاد", "ارتش", "نظامی", "امنیتی", "ترور", "بازداشت", "زندانی",
    "دادگاه", "محکوم", "اعدام", "حقوق بشر", "اعتراض", "اعتصاب", "جنگ",
    "بمباران", "شهید", "سیاسی", "پناهنده", "پناهندگی", "شورای امنیت",
    "سازمان ملل", "جاسوسی", "تسلیحاتی", "لیگ عرب", "اشغال", "شهرک‌سازی",
    "قدس", "مرز", "قانون", "بودجه", "تورم",
    # English
    "election", "parliament", "minister", "sanction", "ceasefire", "attack",
    "missile", "drone", "army", "military", "security", "arrest", "court",
    "human rights", "protest", "war", "refugee", "arms race", "sanctions",
)


def _text(value: object) -> str:
    """Coerce a field to text, ignoring anything that is not a real string.

    Callers include reporting code over partially populated event rows, so a missing
    or non-text field must simply contribute nothing rather than raise.
    """
    return value if isinstance(value, str) else ""


def non_political_story_reason(
    title: str | None,
    summary: str | None = None,
    countries: list[str] | None = None,
    actors: list[str] | None = None,
    location: str | None = None,
) -> str | None:
    """Why a regional story carries no briefing value, or ``None`` to keep it.

    Returns ``"sport"``, ``"tourism"`` or ``"incident"``. A story outside our
    geography is not this gate's business, and any political/security/economic
    marker vetoes the suppression even when a sport or tourism word is present.
    """
    if not has_middle_east_nexus(countries, location):
        return None

    text = normalize_place(
        " | ".join(
            part
            for part in (
                _text(title),
                _text(summary),
                " | ".join(_text(actor) for actor in actors or []),
            )
            if part
        )
    )
    if not text:
        return None
    if any(_name_appears(marker, text) for marker in POLITICAL_OVERRIDE_MARKERS):
        return None

    for reason, markers in (
        (SPORT, SPORTS_MARKERS),
        (TOURISM, TOURISM_MARKERS),
        (INCIDENT, INCIDENT_MARKERS),
    ):
        if any(_name_appears(marker, text) for marker in markers):
            return reason
    return None
