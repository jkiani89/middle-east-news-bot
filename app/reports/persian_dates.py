"""Pure-Python Iranian Solar Hijri (شمسی) and Islamic Lunar Hijri (قمری) date formatting."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_PERSIAN_WEEKDAYS: tuple[str, ...] = (
    "دوشنبه",
    "سه‌شنبه",
    "چهارشنبه",
    "پنج‌شنبه",
    "جمعه",
    "شنبه",
    "یکشنبه",
)

_JALALI_MONTHS: tuple[str, ...] = (
    "",
    "فروردین",
    "اردیبهشت",
    "خرداد",
    "تیر",
    "مرداد",
    "شهریور",
    "مهر",
    "آبان",
    "آذر",
    "دی",
    "بهمن",
    "اسفند",
)

_HIJRI_MONTHS: tuple[str, ...] = (
    "",
    "محرم",
    "صفر",
    "ربیع‌الاول",
    "ربیع‌الثانی",
    "جمادی‌الاول",
    "جمادی‌الثانی",
    "رجب",
    "شعبان",
    "رمضان",
    "شوال",
    "ذی‌قعده",
    "ذی‌حجه",
)


def to_persian_digits(value: str | int) -> str:
    """Replace ASCII digits with Persian digits."""
    return str(value).translate(_FA_DIGITS)


def gregorian_to_jdn(year: int, month: int, day: int) -> int:
    """Return the Julian Day Number for a proleptic Gregorian date."""
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def gregorian_to_jalali(value: date) -> tuple[int, int, int]:
    """Convert a Gregorian ``date`` to Iranian Solar Hijri ``(year, month, day)``."""
    gy, gm, gd = value.year, value.month, value.day
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666
        + (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        + gd
        + g_d_m[gm - 1]
    )
    jy = -1595 + (33 * (days // 12053))
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm = 1 + (days // 31)
        jd = 1 + (days % 31)
    else:
        jm = 7 + ((days - 186) // 30)
        jd = 1 + ((days - 186) % 30)
    return jy, jm, jd


def gregorian_to_hijri(value: date) -> tuple[int, int, int]:
    """Convert a Gregorian ``date`` to Islamic Lunar Hijri ``(year, month, day)`` (civil tabular)."""
    jdn = gregorian_to_jdn(value.year, value.month, value.day)
    days = jdn - 1948440 + 10632
    cycles = (days - 1) // 10631
    days = days - 10631 * cycles + 354
    cycle_year = (
        ((10985 - days) // 5316) * ((50 * days) // 17719)
        + (days // 5670) * ((43 * days) // 15238)
    )
    days = (
        days
        - ((30 - cycle_year) // 15) * ((17719 * cycle_year) // 50)
        - (cycle_year // 16) * ((15238 * cycle_year) // 43)
        + 29
    )
    month = (24 * days) // 709
    day = days - (709 * month) // 24
    year = 30 * cycles + cycle_year - 30
    return year, month, day


def format_jalali_date(value: date) -> str:
    """Numeric Iranian Solar Hijri date with Persian digits (``۱۴۰۵/۰۸/۲۳``)."""
    jy, jm, jd = gregorian_to_jalali(value)
    return to_persian_digits(f"{jy:04d}/{jm:02d}/{jd:02d}")


def format_jalali_long(value: date) -> str:
    """Weekday + Iranian Solar Hijri date (``شنبه ۲۳ آبان ۱۴۰۵``)."""
    weekday = _PERSIAN_WEEKDAYS[value.weekday()]
    jy, jm, jd = gregorian_to_jalali(value)
    return to_persian_digits(f"{weekday} {jd} {_JALALI_MONTHS[jm]} {jy}")


def format_hijri_long(value: date) -> str:
    """Islamic Lunar Hijri date in Persian (``۳ جمادی‌الثانی ۱۴۴۸``)."""
    hy, hm, hd = gregorian_to_hijri(value)
    return to_persian_digits(f"{hd} {_HIJRI_MONTHS[hm]} {hy}")


def format_dual_persian_date(value: date) -> str:
    """Combined Solar Hijri and Lunar Hijri date for public Persian reports."""
    return f"{format_jalali_long(value)} | {format_hijri_long(value)}"


def format_jalali_datetime(moment: datetime, timezone_name: str = "Asia/Tehran") -> str:
    """Local Solar Hijri date and HH:MM time in the target timezone."""
    tz = ZoneInfo(timezone_name)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    local = moment.astimezone(tz)
    date_part = format_jalali_date(local.date())
    time_part = to_persian_digits(local.strftime("%H:%M"))
    return f"{date_part} {time_part}"
