"""Pure-Python Iranian Solar Hijri (شمسی) date formatting."""

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


def to_persian_digits(value: str | int) -> str:
    """Replace ASCII digits with Persian digits."""
    return str(value).translate(_FA_DIGITS)


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


def format_jalali_date(value: date) -> str:
    """Numeric Iranian Solar Hijri date with Persian digits (``۱۴۰۵/۰۸/۲۳``)."""
    jy, jm, jd = gregorian_to_jalali(value)
    return to_persian_digits(f"{jy:04d}/{jm:02d}/{jd:02d}")


def format_jalali_long(value: date) -> str:
    """Weekday + Iranian Solar Hijri date (``شنبه ۲۳ آبان ۱۴۰۵``)."""
    weekday = _PERSIAN_WEEKDAYS[value.weekday()]
    jy, jm, jd = gregorian_to_jalali(value)
    return to_persian_digits(f"{weekday} {jd} {_JALALI_MONTHS[jm]} {jy}")


def format_jalali_datetime(moment: datetime, timezone_name: str = "Asia/Tehran") -> str:
    """Local Solar Hijri date and HH:MM time in the target timezone."""
    tz = ZoneInfo(timezone_name)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo("UTC"))
    local = moment.astimezone(tz)
    date_part = format_jalali_date(local.date())
    time_part = to_persian_digits(local.strftime("%H:%M"))
    return f"{date_part} {time_part}"
