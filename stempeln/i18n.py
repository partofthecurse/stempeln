"""Leichtgewichtige Übersetzung: Deutsch ist die Quellsprache, weitere Sprachen liegen als JSON (deutsch → Ziel) vor."""

import json
import os
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from flask import g, has_request_context
from markupsafe import Markup

LANGUAGES = {"de": "Deutsch", "en": "English"}
TRANSLATION_DIR = Path(__file__).parent / "translations"
MONTHS = {
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
}
WEEKDAYS = {"de": ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"], "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]}


def default_language():
    language = os.getenv("DEFAULT_LANGUAGE", "de").strip().lower()
    return language if language in LANGUAGES else "de"


@lru_cache(maxsize=None)
def catalog(language):
    path = TRANSLATION_DIR / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def current():
    if has_request_context():
        return g.get("lang") or default_language()
    return default_language()


def gettext(text, language=None, **values):
    language = language or current()
    if language != "de":
        text = catalog(language).get(text) or text
    return text % values if values else text


def template_gettext(text, **values):
    """Für Jinja: Übersetzung gilt als vertrauenswürdig, eingesetzte Werte werden escaped."""
    translated = Markup(gettext(text))
    return translated % values if values else translated


def negotiate(accept_header):
    """Erste unterstützte Sprache aus Accept-Language."""
    best, best_quality = None, 0.0
    for part in (accept_header or "").split(","):
        pieces = part.strip().split(";")
        code = pieces[0].strip().lower()[:2]
        quality = 1.0
        for piece in pieces[1:]:
            if piece.strip().startswith("q="):
                try:
                    quality = float(piece.strip()[2:])
                except ValueError:
                    quality = 0.0
        if code in LANGUAGES and quality > best_quality:
            best, best_quality = code, quality
    return best


# ------------------------------------------------------------------ Formate

def fmt_number(value, decimals=2, language=None, strip=False):
    language = language or current()
    text = f"{value:,.{decimals}f}"
    if strip and "." in text:
        text = text.rstrip("0").rstrip(".")
    if language == "de":
        text = text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return text


def fmt_money(cents, language=None):
    language = language or current()
    cents = int(round(cents))
    amount = fmt_number(abs(cents) / 100, 2, language)
    sign = "-" if cents < 0 else ""
    return f"{sign}€{amount}" if language == "en" else f"{sign}{amount} €"


def as_day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10]) if value else None


def fmt_date(value, language=None, short=False):
    day = as_day(value)
    if not day:
        return ""
    language = language or current()
    if language == "en":
        month = MONTHS["en"][day.month - 1][:3]
        return f"{day.day} {month}" if short else f"{day.day} {month} {day.year}"
    return day.strftime("%d.%m.") if short else day.strftime("%d.%m.%Y")


def N_(text):
    """Markiert Text zur Übersetzung, ohne ihn sofort zu übersetzen (für Konstanten)."""
    return text
