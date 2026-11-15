"""Rechnungen: Positionen aus Zeiten, Steuerlogik, Summen, Nummernkreis und Pflichtangaben."""

import re
from collections import OrderedDict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import i18n
from .i18n import N_
from .timecalc import to_local

UNITS = OrderedDict([("HUR", (N_("Std."), N_("Stunden"))), ("DAY", (N_("Tag"), N_("Tage"))), ("C62", (N_("Stk."), N_("Stück"))), ("LS", (N_("pauschal"), N_("Pauschal")))])
POSITION_MODES = OrderedDict([("project", N_("Eine Position je Projekt")), ("day", N_("Eine Position je Projekt und Tag")), ("entry", N_("Eine Position je Eintrag"))])
VAT_MODES = OrderedDict([("standard", N_("Regelbesteuerung (mit Umsatzsteuer)")), ("small_business", N_("Kleinunternehmer (§ 19 UStG, ohne Umsatzsteuer)"))])
CUSTOMER_TAX_MODES = OrderedDict([
    ("domestic", N_("Inland – normale Umsatzsteuer")),
    ("reverse_charge", N_("EU-Unternehmen – Reverse Charge (§ 13b UStG)")),
    ("non_eu", N_("Drittland – nicht im Inland steuerbar (§ 3a UStG)")),
])
DAY_ROUNDING = OrderedDict([("exact", N_("Anteilig (Stunden ÷ Tageslänge)")), ("half", N_("Auf halbe Tage runden")), ("full", N_("Jeder Arbeitstag = 1 Tag"))])
BILLING_UNITS = OrderedDict([("hour", N_("Stundensatz")), ("day", N_("Tagessatz"))])
ROUNDING_CHOICES = OrderedDict([(0, N_("Minutengenau")), (6, N_("6 Minuten (0,1 h)")), (15, N_("Viertelstunde")), (30, N_("Halbe Stunde")), (60, N_("Volle Stunde"))])
STATUS_LABELS = {"draft": N_("Entwurf"), "open": N_("Offen"), "paid": N_("Bezahlt"), "cancelled": N_("Storniert")}

TAX_CATEGORIES = {
    # mode: (UNCL5305-Kategorie, Befreiungsgrund, VATEX-Code)
    "small_business": ("E", N_("Kein Ausweis von Umsatzsteuer, da Kleinunternehmer gemäß § 19 UStG."), None),
    "reverse_charge": ("AE", N_("Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge, § 13b UStG)."), "VATEX-EU-AE"),
    "non_eu": ("O", N_("Nicht im Inland steuerbare Leistung (§ 3a UStG)."), "VATEX-EU-O"),
}

DEFAULT_INTRO = N_("Vielen Dank für Ihren Auftrag. Für meine Leistungen berechne ich Ihnen:")
DEFAULT_FOOTER = N_("Bitte überweisen Sie den Rechnungsbetrag bis zum {faellig} unter Angabe der Rechnungsnummer {nummer}.")
DEFAULT_CLOSING = N_("Mit freundlichen Grüßen")

CENT = Decimal("0.01")


def dec(value):
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def money(cents):
    return (Decimal(int(cents)) / 100).quantize(CENT)


def fmt_quantity(value, language=None):
    text = f"{dec(value).normalize():f}"
    if "." in text:
        whole, fraction = text.split(".")
    else:
        whole, fraction = text, ""
    whole = i18n.fmt_number(int(whole), 0, language) if whole.lstrip("-").isdigit() else whole
    separator = "." if (language or i18n.current()) == "en" else ","
    return whole + (separator + fraction if fraction else "")


def fmt_rate(value, language=None):
    return fmt_quantity(dec(value), language) + " %"


def unit_label(unit, quantity=1, language=None):
    singular, plural = UNITS.get(unit, (unit, unit))
    one = dec(quantity) == 1
    if (language or i18n.current()) == "en" and unit in ("HUR", "DAY"):
        return {"HUR": "hr" if one else "hrs", "DAY": "day" if one else "days"}[unit]
    label = (singular if one else plural) if unit == "DAY" else singular
    return i18n.gettext(label, language)


def line_cents(quantity, price_cents):
    return int((dec(quantity) * int(price_cents)).quantize(Decimal(1), ROUND_HALF_UP))


def parse_quantity(text):
    text = (text or "").strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"-?\d{1,6}(\.\d{1,4})?", text):
        return None
    return Decimal(text)


def parse_rate(text):
    text = (text or "").strip().replace("%", "").replace(",", ".").strip()
    if not re.fullmatch(r"\d{1,2}(\.\d{1,2})?", text):
        return None
    return Decimal(text)


# ------------------------------------------------------------------ Steuer

def tax_mode(settings, customer):
    """Steuerfall der Rechnung: Kleinunternehmer gewinnt, sonst die Einstellung am Kunden."""
    if settings["vat_mode"] == "small_business":
        return "small_business"
    mode = (customer or {}).get("tax_mode") or "domestic"
    return "standard" if mode == "domestic" else mode


def line_category(mode, rate):
    if mode in TAX_CATEGORIES:
        return TAX_CATEGORIES[mode][0], Decimal(0)
    rate = dec(rate)
    return ("S" if rate > 0 else "Z"), rate


def totals(items, mode):
    """Summen nach EN 16931: Steuer je Kategorie/Satz auf die Summe der Positionen."""
    groups = OrderedDict()
    for item in items:
        category, rate = line_category(mode, item["vat_rate"])
        group = groups.setdefault((category, rate), {"category": category, "rate": rate, "basis": 0})
        group["basis"] += item["line_cents"]
    for group in groups.values():
        group["tax"] = int((Decimal(group["basis"]) * group["rate"] / 100).quantize(Decimal(1), ROUND_HALF_UP))
        reason = TAX_CATEGORIES.get(mode)
        group["reason"], group["reason_code"] = (reason[1], reason[2]) if reason else (None, None)
    net = sum(item["line_cents"] for item in items)
    tax = sum(group["tax"] for group in groups.values())
    return {"net": net, "tax": tax, "gross": net + tax, "groups": list(groups.values())}


# ------------------------------------------------------------------ Nummernkreis

def format_number(pattern, counter, day):
    text =pattern.replace("{JJJJ}", f"{day.year:04d}").replace("{JJ}", f"{day.year % 100:02d}").replace("{MM}", f"{day.month:02d}")
    return re.sub(r"\{N+\}", lambda m: str(counter).zfill(len(m.group(0)) - 2), text)


def valid_number_pattern(pattern):
    return bool(re.search(r"\{N+\}", pattern or "")) and len(pattern) <= 40 and re.fullmatch(r"[A-Za-z0-9{}/_.\-]+", pattern or "")


def fill_placeholders(text, invoice, gross_cents, payment_days, language=None):
    values = {
        "{faellig}": i18n.fmt_date(invoice["due_date"], language) if invoice.get("due_date") else "",
        "{nummer}": invoice.get("number") or "",
        "{betrag}": i18n.fmt_money(gross_cents, language),
        "{tage}": str(payment_days),
    }
    values.update({"{due}": values["{faellig}"], "{number}": values["{nummer}"], "{amount}": values["{betrag}"], "{days}": values["{tage}"]})
    for key, value in values.items():
        text = text.replace(key, value)
    return text


# ------------------------------------------------------------------ Positionen aus Zeiten

def _round_seconds(seconds, rounding_minutes):
    if not rounding_minutes:
        return seconds
    step = rounding_minutes * 60
    return max(step, int(Decimal(seconds / step).quantize(Decimal(1), ROUND_HALF_UP)) * step) if seconds else 0


def _hours(seconds):
    return (Decimal(seconds) / 3600).quantize(CENT, ROUND_HALF_UP)


def _days(seconds_by_day, project):
    day_hours = dec(project.get("day_hours") or 8)
    rounding = project.get("day_rounding") or "exact"
    total = Decimal(0)
    for seconds in seconds_by_day:
        if seconds <= 0:
            continue
        if rounding == "full":
            total += 1
        elif rounding == "half":
            halves = (Decimal(seconds) / 3600 / day_hours * 2).quantize(Decimal(1), ROUND_HALF_UP)
            total += max(Decimal("0.5"), halves / 2)
        else:
            total += Decimal(seconds) / 3600 / day_hours
    return total.quantize(CENT, ROUND_HALF_UP)


def _span(days, language):
    first, last = min(days), max(days)
    if first == last:
        return i18n.fmt_date(first, language)
    return f"{i18n.fmt_date(first, language, short=True)}–{i18n.fmt_date(last, language)}"


def positions_from_entries(entries, projects, mode, rounding_minutes, default_vat, language="de"):
    """entries: abgeschlossene Einträge mit project_id; projects: {id: project}."""
    by_project = OrderedDict()
    for entry in sorted(entries, key=lambda e: (projects[e["project_id"]]["name"].casefold(), e["start_at"])):
        by_project.setdefault(entry["project_id"], []).append(entry)
    items = []

    def add(description, detail, quantity, unit, project):
        price = project.get("rate_cents") or 0
        items.append({"description": description, "detail": detail, "quantity": quantity, "unit": unit,
                      "price_cents": price, "vat_rate": dec(default_vat), "line_cents": line_cents(quantity, price)})

    for project_id, project_entries in by_project.items():
        project = projects[project_id]
        per_day = OrderedDict()
        for entry in project_entries:
            per_day.setdefault(to_local(entry["start_at"]).date(), []).append(entry)
        is_day = project.get("billing_unit") == "day"
        unit = "DAY" if is_day else "HUR"
        if mode == "project":
            if is_day:
                quantity = _days([sum(e["end_at"] - e["start_at"] for e in day) for day in per_day.values()], project)
            else:
                quantity = _hours(_round_seconds(sum(e["end_at"] - e["start_at"] for e in project_entries), rounding_minutes))
            count = len(per_day)
            label = i18n.gettext(N_("Arbeitstag") if count == 1 else N_("Arbeitstage"), language)
            label = label.lower() if language == "en" else label
            detail = f"{_span(list(per_day), language)} · {count} {label}"
            add(project["name"], detail, quantity, unit, project)
        elif mode == "day" or is_day:
            for day, day_entries in per_day.items():
                seconds = sum(e["end_at"] - e["start_at"] for e in day_entries)
                quantity = _days([seconds], project) if is_day else _hours(_round_seconds(seconds, rounding_minutes))
                notes = "; ".join(dict.fromkeys(e["note"] for e in day_entries if e["note"]))
                add(f"{project['name']} – {i18n.fmt_date(day, language)}", notes, quantity, unit, project)
        else:
            for entry in project_entries:
                start, end = to_local(entry["start_at"]), to_local(entry["end_at"])
                quantity = _hours(_round_seconds(entry["end_at"] - entry["start_at"], rounding_minutes))
                description = f"{project['name']} – {i18n.fmt_date(start, language)} {start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
                add(description, entry["note"], quantity, unit, project)
    return [item for item in items if item["quantity"] > 0]


# ------------------------------------------------------------------ Pflichtangaben

def seller_problems(settings):
    problems = []
    for key, label in (("company", N_("Name bzw. Firma")), ("street", N_("Straße")), ("zip", N_("PLZ")), ("city", N_("Ort")), ("iban", "IBAN")):
        if not (settings.get(key) or "").strip():
            problems.append(label)
    if not (settings.get("tax_number") or settings.get("vat_id") or "").strip():
        problems.append(N_("Steuernummer oder USt-IdNr."))
    return problems


def buyer_problems(customer, mode):
    problems = []
    for key, label in (("name", N_("Kunde: Name")), ("street", N_("Kunde: Straße")), ("zip", N_("Kunde: PLZ")), ("city", N_("Kunde: Ort"))):
        if not (customer.get(key) or "").strip():
            problems.append(label)
    if mode == "reverse_charge" and not (customer.get("vat_id") or "").strip():
        problems.append(N_("Kunde: USt-IdNr. (für Reverse Charge Pflicht)"))
    return problems


def xrechnung_problems(seller, buyer):
    """Zusätzliche Pflichtfelder der XRechnung (BR-DE-*) – nur für den XRechnung-Export."""
    problems = []
    if not (seller.get("contact_name") or "").strip():
        problems.append(N_("Ansprechpartner (Einstellungen)"))
    if not (seller.get("phone") or "").strip():
        problems.append(N_("Telefon (Einstellungen)"))
    if not (seller.get("email") or "").strip():
        problems.append(N_("E-Mail (Einstellungen)"))
    if not (buyer.get("email") or "").strip():
        problems.append(N_("E-Mail des Kunden"))
    return problems


def snapshot_seller(settings):
    keys = ("company", "contact_name", "street", "zip", "city", "country", "email", "phone", "website",
            "tax_number", "vat_id", "bank_name", "iban", "bic", "account_holder")
    return {key: (settings.get(key) or "").strip() for key in keys}


def snapshot_buyer(customer):
    keys = ("number", "name", "contact", "street", "zip", "city", "country", "email", "vat_id", "leitweg_id", "tax_mode")
    return {key: (customer.get(key) or "").strip() if isinstance(customer.get(key), str) else customer.get(key) for key in keys}


def as_date(value):
    if isinstance(value, date) or value is None:
        return value
    return date.fromisoformat(value)


def project_amount(seconds, project):
    """Wert erfasster Zeit in Cent (Stunden- oder Tagessatz, ohne Rundung)."""
    if not project or not project.get("rate_cents"):
        return 0
    hours = Decimal(seconds) / 3600
    if project.get("billing_unit") == "day":
        hours = hours / dec(project.get("day_hours") or 8)
    return int((hours * project["rate_cents"]).quantize(Decimal(1), ROUND_HALF_UP))


def rate_suffix(project):
    return N_("/Tag") if (project or {}).get("billing_unit") == "day" else "/h"


# ------------------------------------------------------------------ Rechnungslayout

PDF_FONTS = OrderedDict([("dmsans", "DM Sans"), ("anuphan", "Anuphan"), ("ibmplexsans", "IBM Plex Sans"),
                         ("inter", "Inter"), ("sourceserif", "Source Serif")])
LOGO_POSITIONS = OrderedDict([("right", N_("Rechts")), ("left", N_("Links")), ("center", N_("Mitte"))])
LAYOUT_DEFAULTS = {
    "font": "dmsans", "accent": "#2f55c9", "logo_position": "right", "logo_height": 16,
    "header_text": True, "sender_line": True, "fold_marks": False, "page_numbers": True, "footer": True,
    "footer_owner": False, "girocode": True, "salutation": True, "signature": True, "info_customer_number": True,
    "info_contact": False, "info_due_date": True, "address_country": False, "title_accent": False, "table_accent": False,
}
LAYOUT_FLAGS = [key for key, value in LAYOUT_DEFAULTS.items() if isinstance(value, bool)]


def layout_values(raw):
    """Gespeichertes Layout (JSON-Text oder dict) mit Standardwerten auffüllen und prüfen."""
    import json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    values = dict(LAYOUT_DEFAULTS)
    for key, value in (raw or {}).items():
        if key in LAYOUT_DEFAULTS and type(value) is type(LAYOUT_DEFAULTS[key]):
            values[key] = value
    if values["font"] not in PDF_FONTS:
        values["font"] = LAYOUT_DEFAULTS["font"]
    if values["logo_position"] not in LOGO_POSITIONS:
        values["logo_position"] = "right"
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", values["accent"]):
        values["accent"] = LAYOUT_DEFAULTS["accent"]
    values["logo_height"] = max(8, min(40, int(values["logo_height"])))
    return values


def layout_from_form(form):
    values = {key: bool(form.get(key)) for key in LAYOUT_FLAGS}
    values.update(font=form.get("font", ""), accent=(form.get("accent") or "").strip(), logo_position=form.get("logo_position", ""))
    try:
        values["logo_height"] = int(form.get("logo_height") or LAYOUT_DEFAULTS["logo_height"])
    except ValueError:
        values["logo_height"] = LAYOUT_DEFAULTS["logo_height"]
    return layout_values(values)


def hex_to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def epc_payload(seller, cents, reference):
    """Inhalt eines GiroCodes (EPC069-12, Version 002, SEPA-Überweisung in Euro)."""
    iban = (seller.get("iban") or "").replace(" ", "").upper()
    name = (seller.get("account_holder") or seller.get("company") or "").strip()[:70]
    if not iban or not name or not 1 <= cents <= 99999999999:
        return None
    text = (f"{reference}" if reference else "").strip()[:140]
    lines = ["BCD", "002", "1", "SCT", (seller.get("bic") or "").replace(" ", "").upper(), name, iban,
             f"EUR{cents // 100}.{cents % 100:02d}", "", "", text]
    return "\n".join(lines).rstrip("\n")
