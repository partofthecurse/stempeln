"""Arbeitszeit-Berechnung: Tagesaufteilung, gesetzliche Pausen, Soll/Ist und Saldo."""

import os
import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import holidays

REGIONS = {
    "": "Nur bundesweite Feiertage", "BW": "Baden-Württemberg", "BY": "Bayern", "BE": "Berlin",
    "BB": "Brandenburg", "HB": "Bremen", "HH": "Hamburg", "HE": "Hessen", "MV": "Mecklenburg-Vorpommern",
    "NI": "Niedersachsen", "NW": "Nordrhein-Westfalen", "RP": "Rheinland-Pfalz", "SL": "Saarland",
    "SN": "Sachsen", "ST": "Sachsen-Anhalt", "SH": "Schleswig-Holstein", "TH": "Thüringen",
}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
ABSENCE_LABELS = {"vacation": "Urlaub", "sick": "Krank", "other": "Frei"}
MIN_BREAK_GAP = 15 * 60


def local_zone():
    return ZoneInfo(os.getenv("TZ") or "Europe/Berlin")


def today():
    return datetime.now(local_zone()).date()


def day_bounds(day):
    zone = local_zone()
    start = datetime.combine(day, datetime.min.time(), zone)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), zone)
    return int(start.timestamp()), int(end.timestamp())


def to_local(timestamp):
    return datetime.fromtimestamp(timestamp, local_zone())


def parse_local(day_text, time_text):
    moment = datetime.strptime(f"{day_text} {time_text}", "%Y-%m-%d %H:%M")
    return int(moment.replace(tzinfo=local_zone()).timestamp())


@lru_cache(maxsize=64)
def holiday_calendar(region, year):
    return dict(holidays.country_holidays("DE", subdiv=region or None, years=year, language="de"))


def holiday_name(region, day):
    return holiday_calendar(region, day.year).get(day)


def required_break_net(worked, gaps):
    """Nettoarbeitszeit nach §4 ArbZG: 30 Min. ab 6 h, 45 Min. ab 9 h; Pausen ab 15 Min. zählen."""
    six, nine = 6 * 3600, 9 * 3600
    if worked <= six:
        return worked
    return min(
        worked,
        max(six, worked - max(0, 30 * 60 - gaps)),
        max(nine, worked - max(0, 45 * 60 - gaps)),
    )


def target_seconds(profile, day, absence=None):
    if not profile.get("track_balance", 1):
        return 0
    workdays = profile["workdays"]
    if not workdays or str(day.weekday()) not in workdays:
        return 0
    if holiday_name(profile["region"], day):
        return 0
    target = profile["weekly_minutes"] * 60 // len(workdays)
    if absence:
        return target // 2 if absence["half"] else 0
    return target


def day_report(profile, day, entries, absence=None, now=None):
    now = int(now if now is not None else time.time())
    start, end = day_bounds(day)
    segments = []
    for entry in entries:
        entry_end = entry["end_at"] if entry["end_at"] is not None else max(entry["start_at"], now)
        begin, finish = max(entry["start_at"], start), min(entry_end, end)
        if finish > begin:
            segments.append((begin, finish, entry))
    segments.sort(key=lambda item: item[0])
    worked = sum(finish - begin for begin, finish, _ in segments)
    gaps = sum(
        gap for gap in (segments[index + 1][0] - segments[index][1] for index in range(len(segments) - 1))
        if gap >= MIN_BREAK_GAP
    )
    net = required_break_net(worked, gaps) if profile["break_rule"] else worked
    target = target_seconds(profile, day, absence)
    return {
        "day": day,
        "weekday": WEEKDAYS[day.weekday()],
        "segments": segments,
        "worked": worked,
        "breaks": gaps,
        "deducted": worked - net,
        "net": net,
        "target": target,
        "diff": net - target,
        "holiday": holiday_name(profile["region"], day),
        "absence": absence,
        "open": any(entry["end_at"] is None for _, _, entry in segments),
    }


def days_between(first, last):
    current = first
    while current <= last:
        yield current
        current += timedelta(days=1)


def range_report(store, user_id, profile, first, last, now=None, project_id=None):
    start, _ = day_bounds(first)
    _, end = day_bounds(last)
    entries = store.entries_between(user_id, start, end, project_id)
    absences = store.absences_between(user_id, first.isoformat(), last.isoformat())
    return [day_report(profile, day, entries, absences.get(day.isoformat()), now) for day in days_between(first, last)]


def balance_start(store, user_id, profile, created_at):
    if profile["balance_start"]:
        return date.fromisoformat(profile["balance_start"])
    first = store.first_entry_start(user_id)
    candidates = [date.fromisoformat(created_at)]
    if first:
        candidates.append(to_local(first).date())
    return min(candidates)


def balance(store, user_id, profile, created_at, through, now=None):
    """Saldo in Sekunden vom Startdatum bis einschließlich ``through``."""
    first = balance_start(store, user_id, profile, created_at)
    total = profile["balance_offset"] * 60
    if through >= first:
        total += sum(report["diff"] for report in range_report(store, user_id, profile, first, through, now))
    return total


def vacation_used(store, user_id, profile, year):
    absences = store.absences_between(user_id, f"{year}-01-01", f"{year}-12-31")
    used = 0.0
    for day_text, absence in absences.items():
        day = date.fromisoformat(day_text)
        if absence["kind"] == "vacation" and target_seconds(profile, day):
            used += 0.5 if absence["half"] else 1
    return used


def fmt_duration(seconds, signed=False):
    sign = ""
    if seconds < 0:
        sign = "−"
    elif signed and seconds > 0:
        sign = "+"
    minutes = abs(int(seconds)) // 60
    return f"{sign}{minutes // 60}:{minutes % 60:02d}"


def fmt_time(timestamp):
    return to_local(timestamp).strftime("%H:%M")


def project_totals(reports):
    """Erfasste Zeit (ohne Pausenabzug) je Projekt-ID; None = ohne Projekt."""
    totals = {}
    for report in reports:
        for begin, finish, entry in report["segments"]:
            totals[entry["project_id"]] = totals.get(entry["project_id"], 0) + finish - begin
    return totals


def fmt_money(cents):
    euros, rest = divmod(int(round(cents)), 100)
    return f"{euros:,}".replace(",", ".") + f",{rest:02d} €"


def amount_cents(seconds, rate_cents):
    return seconds * rate_cents / 3600 if rate_cents else 0
