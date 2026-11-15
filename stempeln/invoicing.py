"""Rechnungen, Kunden und Rechnungseinstellungen (optional je Benutzer aktivierbar)."""

import hashlib
import json
import re
import sqlite3
import time
from datetime import date, timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from . import billing, einvoice, i18n, invoice_pdf
from .i18n import gettext as _
from .timecalc import day_bounds, fmt_duration, to_local, today

bp = Blueprint("invoicing", __name__)


def store():
    return current_app.extensions["stempeln_store"]


def invoicing_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not store().invoice_settings(current_user.uid)["enabled"]:
            flash(_("Rechnungen sind ausgeschaltet. Du kannst sie unter Konto aktivieren."), "error")
            return redirect(url_for("account"))
        return view(*args, **kwargs)
    return wrapped


def clean(text, limit=200):
    return " ".join((text or "").split())[:limit]


def parse_day(text):
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def parse_money(text):
    text = (text or "").strip().replace("€", "").replace(" ", "")
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"-?\d{1,7}(\.\d{1,2})?", text):
        return None
    return int((Decimal(text) * 100).to_integral_value())


def own_invoice(invoice_id):
    invoice = store().invoice(current_user.uid, invoice_id)
    if not invoice:
        abort(404)
    return invoice


def safe_filename(text):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "rechnung"


def item_dicts(items):
    return [{**item, "quantity": Decimal(item["quantity"]), "vat_rate": Decimal(item["vat_rate"])} for item in items]


def build_doc(invoice, items=None, settings=None, customer=None):
    """Datensatz für PDF/XML; abgeschlossene Rechnungen nutzen ausschließlich ihre Momentaufnahme."""
    items = item_dicts(items if items is not None else store().invoice_items(invoice["id"]))
    if invoice["status"] != "draft":
        seller, buyer, mode = json.loads(invoice["seller_json"]), json.loads(invoice["buyer_json"]), invoice["tax_mode"]
        settings = settings if settings and settings.get("user_id") == invoice["user_id"] else None
    else:
        settings = settings or store().invoice_settings(current_user.uid)
        customer = customer or (store().customer(current_user.uid, invoice["customer_id"]) if invoice["customer_id"] else {}) or {}
        seller, buyer, mode = billing.snapshot_seller(settings), billing.snapshot_buyer(customer), billing.tax_mode(settings, customer)
    preceding = None
    if invoice["corrects_id"]:
        original = store().invoice(invoice["user_id"], invoice["corrects_id"])
        preceding = (original["number"], billing.as_date(original["issue_date"]))
    summary = billing.totals(items, mode)
    doc = {
        "number": invoice["number"] or "Entwurf", "type_code": invoice["type_code"],
        "issue_date": billing.as_date(invoice["issue_date"]), "due_date": billing.as_date(invoice["due_date"]),
        "service_from": billing.as_date(invoice["service_from"]), "service_to": billing.as_date(invoice["service_to"]),
        "buyer_reference": invoice["buyer_reference"], "notes": [], "preceding": preceding,
        "seller": seller, "buyer": buyer, "items": items, "tax_mode": mode,
        "intro": invoice["intro"], "closing": invoice["closing"], "language": invoice["language"],
        "salutation": invoice.get("salutation") or "",
        "layout": billing.layout_values(settings["layout"] if settings else store().invoice_layout(invoice["user_id"])),
        "logo": logo_path(invoice["user_id"]),
    }
    language = invoice["language"]
    days = (doc["due_date"] - doc["issue_date"]).days if doc["due_date"] else 0
    doc["footer"] = billing.fill_placeholders(invoice["footer"], doc, summary["gross"], days, language)
    doc["intro"] = billing.fill_placeholders(invoice["intro"], doc, summary["gross"], days, language)
    if invoice["type_code"] == "380" and doc["due_date"]:
        doc["payment_terms"] = _("Zahlbar bis %(date)s ohne Abzug.", language, date=i18n.fmt_date(doc["due_date"], language))
    doc["notes"] = [note for note in (doc["intro"], doc["footer"]) if note]
    return doc, summary


def branding_dir(user_id):
    path = Path(current_app.config["DATA_DIR"]) / "branding" / str(user_id)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def logo_path(user_id):
    path = Path(current_app.config["DATA_DIR"]) / "branding" / str(user_id) / "logo.png"
    return path if path.exists() else None


def default_salutation(customer, language):
    return (customer or {}).get("salutation") or _("Sehr geehrte Damen und Herren,", language)


def timesheet_rows(invoice):
    rows = []
    for entry in store().invoice_entries(invoice["id"]):
        start, end = to_local(entry["start_at"]), to_local(entry["end_at"])
        seconds = entry["end_at"] - entry["start_at"]
        rows.append({"day": i18n.fmt_date(start, invoice["language"]), "time": f"{start:%H:%M}–{end:%H:%M}", "duration": fmt_duration(seconds),
                     "project": entry["project_name"] or "", "note": entry["note"], "seconds": seconds})
    return rows


def archive_dir(user_id):
    path = Path(current_app.config["DATA_DIR"]) / "invoices" / str(user_id)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def renumber(connection, invoice_id):
    rows = connection.execute("SELECT id FROM invoice_items WHERE invoice_id = ? ORDER BY position, id", (invoice_id,)).fetchall()
    connection.executemany("UPDATE invoice_items SET position = ? WHERE id = ?", [(index, row["id"]) for index, row in enumerate(rows, 1)])


def collect_positions(connection, invoice, mode, settings):
    """Hängt offene Zeiten des Kunden im Leistungszeitraum an den Entwurf und erzeugt Positionen."""
    start, _ignored = day_bounds(billing.as_date(invoice["service_from"]))
    _, end = day_bounds(billing.as_date(invoice["service_to"]))
    entries = store().unbilled_entries(current_user.uid, invoice["customer_id"], start, end, connection)
    projects = {project["id"]: project for project in store().projects(current_user.uid)}
    default_vat = "0" if settings["vat_mode"] == "small_business" else settings["default_vat_rate"]
    positions = billing.positions_from_entries(entries, projects, mode, settings["rounding_minutes"], default_vat, invoice["language"])
    connection.executemany("UPDATE entries SET invoice_id = ? WHERE id = ? AND invoice_id IS NULL",
                           [(invoice["id"], entry["id"]) for entry in entries])
    offset = connection.execute("SELECT COALESCE(MAX(position), 0) FROM invoice_items WHERE invoice_id = ?", (invoice["id"],)).fetchone()[0]
    connection.executemany(
        "INSERT INTO invoice_items (invoice_id, position, description, detail, quantity, unit, price_cents, vat_rate, line_cents) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(invoice["id"], offset + index, item["description"], item["detail"], str(item["quantity"]), item["unit"],
          item["price_cents"], str(item["vat_rate"]), item["line_cents"]) for index, item in enumerate(positions, 1)])
    return entries, positions


def next_number(connection, settings, issue_day):
    counter = settings["next_number"]
    if settings["reset_yearly"] and settings["number_year"] != issue_day.year:
        counter = 1
    while True:
        number = billing.format_number(settings["number_format"], counter, issue_day)
        if not connection.execute("SELECT 1 FROM invoices WHERE user_id = ? AND number = ?", (current_user.uid, number)).fetchone():
            break
        counter += 1
    connection.execute("UPDATE invoice_settings SET next_number = ?, number_year = ? WHERE user_id = ?",
                       (counter + 1, issue_day.year, current_user.uid))
    return number


def finalize(invoice_id):
    """Vergibt die Nummer, friert Absender/Empfänger ein und archiviert das PDF unveränderlich."""
    store().invoice_settings(current_user.uid)
    written, created = None, False
    with store().connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        settings = dict(connection.execute("SELECT * FROM invoice_settings WHERE user_id = ?", (current_user.uid,)).fetchone())
        invoice = dict(connection.execute("SELECT * FROM invoices WHERE id = ? AND user_id = ?", (invoice_id, current_user.uid)).fetchone())
        if invoice["status"] != "draft":
            return None
        if invoice["corrects_id"]:
            original = connection.execute("SELECT seller_json, buyer_json, tax_mode FROM invoices WHERE id = ?", (invoice["corrects_id"],)).fetchone()
            seller, buyer, mode = json.loads(original["seller_json"]), json.loads(original["buyer_json"]), original["tax_mode"]
        customer = store().customer(current_user.uid, invoice["customer_id"]) or {} if invoice["customer_id"] else {}
        if not invoice["corrects_id"]:
            seller, buyer, mode = billing.snapshot_seller(settings), billing.snapshot_buyer(customer), billing.tax_mode(settings, customer)
        items = item_dicts([dict(row) for row in connection.execute(
            "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id", (invoice_id,))])
        problems = billing.seller_problems(seller) + billing.buyer_problems(buyer, mode)
        if not items:
            problems.append("mindestens eine Position")
        summary = billing.totals(items, mode)
        if summary["gross"] <= 0:
            problems.append("ein Rechnungsbetrag größer 0 €")
        if problems:
            flash(_("Für den Abschluss fehlt noch: %(fields)s.", fields=", ".join(_(problem) for problem in problems)), "error")
            return None
        issue_day = billing.as_date(invoice["issue_date"])
        number = next_number(connection, settings, issue_day)
        days = customer.get("payment_days") if customer.get("payment_days") is not None else settings["payment_days"]
        due = None if invoice["type_code"] == "381" else (billing.as_date(invoice["due_date"]) or issue_day + timedelta(days=days))
        invoice.update(number=number, status="open", seller_json=json.dumps(seller), buyer_json=json.dumps(buyer),
                       tax_mode=mode, due_date=due.isoformat() if due else None)
        doc, _ignored = build_doc(invoice, [dict(item, quantity=str(item["quantity"]), vat_rate=str(item["vat_rate"])) for item in items],
                                  settings)
        sheet = timesheet_rows(invoice) if invoice["timesheet"] else None
        pdf, _ignored = invoice_pdf.render_final(doc, sheet)
        written = archive_dir(current_user.uid) / f"{safe_filename(number)}.pdf"
        try:
            with open(written, "xb") as handle:
                created = True
                handle.write(pdf)
            written.chmod(0o400)
            connection.execute(
                "UPDATE invoices SET number = ?, status = ?, seller_json = ?, buyer_json = ?, tax_mode = ?, due_date = ?, "
                "net_cents = ?, tax_cents = ?, gross_cents = ?, pdf_file = ?, pdf_sha256 = ?, finalized_at = ? WHERE id = ?",
                (number, "open", invoice["seller_json"], invoice["buyer_json"], mode, invoice["due_date"], summary["net"],
                 summary["tax"], summary["gross"], written.name, hashlib.sha256(pdf).hexdigest(), int(time.time()), invoice_id))
        except BaseException:
            if created:
                written.chmod(0o600)
                written.unlink(missing_ok=True)
            raise
    return number


# ------------------------------------------------------------------ Übersicht

@bp.route("/rechnungen", methods=["GET"])
@invoicing_required
def invoices():
    year = request.args.get("jahr", type=int)
    with store().connect() as connection:
        query = ("SELECT invoices.*, customers.name AS customer_name FROM invoices LEFT JOIN customers ON customers.id = invoices.customer_id "
                 "WHERE invoices.user_id = ?")
        parameters = [current_user.uid]
        if year:
            query += " AND substr(invoices.issue_date, 1, 4) = ?"
            parameters.append(str(year))
        rows = [dict(row) for row in connection.execute(query + " ORDER BY invoices.status = 'draft' DESC, invoices.issue_date DESC, invoices.id DESC", parameters)]
        years = [row[0] for row in connection.execute(
            "SELECT DISTINCT substr(issue_date, 1, 4) FROM invoices WHERE user_id = ? ORDER BY 1 DESC", (current_user.uid,))]
        unbilled = [dict(row) for row in connection.execute(
            "SELECT customers.id, customers.name, COUNT(entries.id) AS entries, SUM(entries.end_at - entries.start_at) AS seconds, "
            "MIN(entries.start_at) AS first, MAX(entries.start_at) AS last FROM entries "
            "JOIN projects ON projects.id = entries.project_id JOIN customers ON customers.id = projects.customer_id "
            "WHERE entries.user_id = ? AND entries.end_at IS NOT NULL AND entries.invoice_id IS NULL "
            "GROUP BY customers.id ORDER BY customers.name COLLATE NOCASE", (current_user.uid,))]
        amounts = {}
        for row in connection.execute(
                "SELECT customers.id AS customer_id, projects.rate_cents, projects.billing_unit, projects.day_hours, "
                "SUM(entries.end_at - entries.start_at) AS seconds FROM entries JOIN projects ON projects.id = entries.project_id "
                "JOIN customers ON customers.id = projects.customer_id WHERE entries.user_id = ? AND entries.end_at IS NOT NULL "
                "AND entries.invoice_id IS NULL GROUP BY projects.id", (current_user.uid,)):
            amounts[row["customer_id"]] = amounts.get(row["customer_id"], 0) + billing.project_amount(row["seconds"], dict(row))
    for row in unbilled:
        row["amount"] = amounts.get(row["id"], 0)
        row["first"], row["last"] = to_local(row["first"]).date(), to_local(row["last"]).date()
    current = today()
    for row in rows:
        row["overdue"] = row["status"] == "open" and row["type_code"] == "380" and row["due_date"] and row["due_date"] < current.isoformat()
    open_total = sum(row["gross_cents"] for row in rows if row["status"] == "open" and row["type_code"] == "380")
    paid_year = sum(row["gross_cents"] for row in rows if row["status"] == "paid" and (row["paid_on"] or "")[:4] == str(current.year))
    last_month_end = current.replace(day=1) - timedelta(days=1)
    settings = store().invoice_settings(current_user.uid)
    return render_template(
        "invoices.html", invoices=rows, years=years, year=year, unbilled=unbilled, open_total=open_total, paid_year=paid_year,
        customers=store().customers(current_user.uid, include_archived=False), settings=settings,
        default_from=last_month_end.replace(day=1).isoformat(), default_to=last_month_end.isoformat(),
        problems=[_(problem) for problem in billing.seller_problems(settings)],
    )


@bp.post("/rechnungen/neu")
@invoicing_required
def create_invoice():
    customer = store().customer(current_user.uid, request.form.get("customer_id", type=int) or 0)
    first, last = parse_day(request.form.get("from")), parse_day(request.form.get("to"))
    mode = request.form.get("mode", "project")
    if not customer or not first or not last or last < first or mode not in (*billing.POSITION_MODES, "empty"):
        flash(_("Bitte Kunde und einen gültigen Leistungszeitraum wählen."), "error")
        return redirect(url_for("invoicing.invoices"))
    settings = store().invoice_settings(current_user.uid)
    language = customer.get("language") or "de"

    def text(value, default):
        # Unveränderte Standardtexte werden in die Rechnungssprache übersetzt, eigene Texte bleiben wie sie sind
        value = value or default
        return _(value, language) if value in (default, billing.DEFAULT_CLOSING) else value

    with store().connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        invoice_id = connection.execute(
            "INSERT INTO invoices (user_id, customer_id, issue_date, service_from, service_to, buyer_reference, salutation, intro, footer, "
            "closing, timesheet, language, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (current_user.uid, customer["id"], today().isoformat(), first.isoformat(), last.isoformat(), customer["leitweg_id"],
             default_salutation(customer, language),
             text(settings["intro"], billing.DEFAULT_INTRO), text(settings["footer"], billing.DEFAULT_FOOTER),
             text(settings["closing"], billing.DEFAULT_CLOSING), 0 if mode == "empty" else settings["timesheet"], language,
             int(time.time()))).lastrowid
        invoice = dict(connection.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone())
        if mode != "empty":
            entries, positions = collect_positions(connection, invoice, mode, settings)
            if not entries:
                connection.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
                flash(_("Für %(name)s gibt es im Zeitraum keine offenen Zeiten. Tipp: „Leere Rechnung“ für Pauschalen.", name=customer["name"]), "error")
                return redirect(url_for("invoicing.invoices"))
            if any(not item["price_cents"] for item in positions):
                flash(_("Mindestens ein Projekt hat keinen Stunden- bzw. Tagessatz – bitte den Preis in den Positionen ergänzen."), "error")
    return redirect(url_for("invoicing.invoice", invoice_id=invoice_id))


# ------------------------------------------------------------------ Einzelne Rechnung

@bp.route("/rechnungen/<int:invoice_id>", methods=["GET", "POST"])
@invoicing_required
def invoice(invoice_id):
    invoice = own_invoice(invoice_id)
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("header", "item", "add", "delete-item", "move", "recalc", "finalize") and invoice["status"] != "draft":
            abort(409)
        settings = store().invoice_settings(current_user.uid)
        if action == "header":
            issue, first, last = (parse_day(request.form.get(key)) for key in ("issue_date", "service_from", "service_to"))
            due = parse_day(request.form.get("due_date"))
            if not issue or (first and last and last < first) or (due and due < issue):
                flash(_("Bitte die Datumsangaben prüfen."), "error")
            else:
                with store().connect() as connection:
                    connection.execute(
                        "UPDATE invoices SET issue_date = ?, service_from = ?, service_to = ?, due_date = ?, buyer_reference = ?, "
                        "salutation = ?, intro = ?, footer = ?, closing = ?, timesheet = ? WHERE id = ?",
                        (issue.isoformat(), first.isoformat() if first else None, last.isoformat() if last else None,
                         due.isoformat() if due else None, clean(request.form.get("buyer_reference"), 100),
                         clean(request.form.get("salutation"), 200),
                         request.form.get("intro", "").strip()[:2000], request.form.get("footer", "").strip()[:2000],
                         request.form.get("closing", "").strip()[:300], 1 if request.form.get("timesheet") else 0, invoice_id))
                flash(_("Rechnungsdaten gespeichert."), "success")
        elif action in ("item", "add"):
            description = clean(request.form.get("description"))
            quantity = billing.parse_quantity(request.form.get("quantity"))
            price = parse_money(request.form.get("price"))
            unit = request.form.get("unit")
            rate = Decimal(0) if settings["vat_mode"] == "small_business" else billing.parse_rate(request.form.get("vat_rate"))
            if not description or quantity is None or quantity <= 0 or price is None or price < 0 or unit not in billing.UNITS or rate is None:
                flash(_("Bitte Beschreibung, Menge (> 0), Einheit, Preis und Steuersatz prüfen."), "error")
            else:
                values = (description, request.form.get("detail", "").strip()[:1000], str(quantity), unit, price, str(rate),
                          billing.line_cents(quantity, price))
                with store().connect() as connection:
                    if action == "add":
                        position = connection.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM invoice_items WHERE invoice_id = ?", (invoice_id,)).fetchone()[0]
                        connection.execute("INSERT INTO invoice_items (description, detail, quantity, unit, price_cents, vat_rate, line_cents, "
                                           "invoice_id, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*values, invoice_id, position))
                    else:
                        connection.execute("UPDATE invoice_items SET description = ?, detail = ?, quantity = ?, unit = ?, price_cents = ?, "
                                           "vat_rate = ?, line_cents = ? WHERE id = ? AND invoice_id = ?",
                                           (*values, request.form.get("item_id", type=int), invoice_id))
                flash(_("Position gespeichert."), "success")
        elif action == "delete-item":
            with store().connect() as connection:
                connection.execute("DELETE FROM invoice_items WHERE id = ? AND invoice_id = ?", (request.form.get("item_id", type=int), invoice_id))
                renumber(connection, invoice_id)
        elif action == "move":
            item_id, step = request.form.get("item_id", type=int), -1 if request.form.get("direction") == "up" else 1
            with store().connect() as connection:
                renumber(connection, invoice_id)
                rows = [row["id"] for row in connection.execute("SELECT id FROM invoice_items WHERE invoice_id = ? ORDER BY position", (invoice_id,))]
                if item_id in rows and 0 <= rows.index(item_id) + step < len(rows):
                    index = rows.index(item_id)
                    rows[index], rows[index + step] = rows[index + step], rows[index]
                    connection.executemany("UPDATE invoice_items SET position = ? WHERE id = ?", [(i, row) for i, row in enumerate(rows, 1)])
        elif action == "recalc":
            mode = request.form.get("mode", settings["position_mode"])
            if mode not in billing.POSITION_MODES or not invoice["service_from"] or not invoice["service_to"]:
                flash(_("Für die Neuberechnung braucht der Entwurf einen Leistungszeitraum."), "error")
            else:
                with store().connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("UPDATE entries SET invoice_id = NULL WHERE invoice_id = ?", (invoice_id,))
                    connection.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
                    entries, _ignored = collect_positions(connection, invoice, mode, settings)
                flash(_("Positionen aus %(count)s Einträgen neu berechnet.", count=len(entries)), "success")
        elif action == "delete" and invoice["status"] == "draft":
            with store().connect() as connection:
                connection.execute("UPDATE entries SET invoice_id = NULL WHERE invoice_id = ?", (invoice_id,))
                connection.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))
            flash(_("Entwurf gelöscht. Die Zeiten sind wieder offen."), "success")
            return redirect(url_for("invoicing.invoices"))
        elif action == "finalize":
            number = finalize(invoice_id)
            if number:
                flash(_("Rechnung %(number)s abgeschlossen und archiviert.", number=number), "success")
        elif action == "paid" and invoice["status"] in ("open", "paid") and invoice["type_code"] == "380":
            paid_on = parse_day(request.form.get("paid_on")) or today()
            with store().connect() as connection:
                if invoice["status"] == "open":
                    connection.execute("UPDATE invoices SET status = 'paid', paid_on = ? WHERE id = ?", (paid_on.isoformat(), invoice_id))
                else:
                    connection.execute("UPDATE invoices SET status = 'open', paid_on = NULL WHERE id = ?", (invoice_id,))
        elif action == "cancel" and invoice["status"] in ("open", "paid") and invoice["type_code"] == "380":
            return cancel(invoice)
        else:
            abort(400)
        return redirect(url_for("invoicing.invoice", invoice_id=invoice_id) + ("#positionen" if action in ("item", "add", "delete-item", "move") else ""))

    settings = store().invoice_settings(current_user.uid)
    customer = store().customer(current_user.uid, invoice["customer_id"]) if invoice["customer_id"] else None
    items = store().invoice_items(invoice_id)
    doc, summary = build_doc(invoice, items, settings, customer)
    with store().connect() as connection:
        related = [dict(row) for row in connection.execute(
            "SELECT id, number, type_code, issue_date FROM invoices WHERE user_id = ? AND (corrects_id = ? OR id = ?)",
            (current_user.uid, invoice_id, invoice["corrects_id"] or 0))]
    problems = []
    if invoice["status"] == "draft":
        problems = billing.seller_problems(doc["seller"]) + billing.buyer_problems(doc["buyer"], doc["tax_mode"])
    return render_template(
        "invoice.html", invoice=invoice, items=item_dicts(items), doc=doc,
        editing=next((item for item in item_dicts(items) if item["id"] == request.args.get("position", type=int)), None), summary=summary, customer=customer, settings=settings,
        entries=store().invoice_entries(invoice_id), related=related, problems=[_(problem) for problem in problems],
        xrechnung_missing=[_(problem) for problem in billing.xrechnung_problems(doc["seller"], doc["buyer"])],
        today=today(),
    )


def cancel(invoice):
    """Stornorechnung (Typ 381) mit denselben Positionen; die Zeiten werden wieder freigegeben."""
    language = invoice["language"]
    with store().connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM invoices WHERE corrects_id = ?", (invoice["id"],)).fetchone():
            abort(409)
        cancellation_id = connection.execute(
            "INSERT INTO invoices (user_id, customer_id, type_code, corrects_id, issue_date, service_from, service_to, buyer_reference, "
            "intro, footer, closing, timesheet, language, created_at) VALUES (?, ?, '381', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (current_user.uid, invoice["customer_id"], invoice["id"], today().isoformat(), invoice["service_from"], invoice["service_to"],
             invoice["buyer_reference"],
             _("Hiermit storniere ich die Rechnung %(number)s vom %(date)s vollständig.", language, number=invoice["number"],
               date=i18n.fmt_date(invoice["issue_date"], language)),
             _("Bereits gezahlte Beträge werden erstattet bzw. verrechnet.", language) if invoice["status"] == "paid" else
             _("Die ursprüngliche Rechnung ist damit gegenstandslos und muss nicht bezahlt werden.", language),
             invoice["closing"], language, int(time.time()))).lastrowid
        connection.execute(
            "INSERT INTO invoice_items (invoice_id, position, description, detail, quantity, unit, price_cents, vat_rate, line_cents) "
            "SELECT ?, position, description, detail, quantity, unit, price_cents, vat_rate, line_cents FROM invoice_items WHERE invoice_id = ?",
            (cancellation_id, invoice["id"]))
    number = finalize(cancellation_id)
    if not number:
        with store().connect() as connection:
            connection.execute("DELETE FROM invoices WHERE id = ?", (cancellation_id,))
        return redirect(url_for("invoicing.invoice", invoice_id=invoice["id"]))
    with store().connect() as connection:
        connection.execute("UPDATE invoices SET status = 'cancelled' WHERE id = ?", (invoice["id"],))
        connection.execute("UPDATE entries SET invoice_id = NULL WHERE invoice_id = ?", (invoice["id"],))
    flash(_("Stornorechnung %(number)s erstellt. Die Zeiten sind wieder offen und können neu abgerechnet werden.", number=number), "success")
    return redirect(url_for("invoicing.invoice", invoice_id=cancellation_id))


@bp.get("/rechnungen/<int:invoice_id>/pdf")
@invoicing_required
def invoice_pdf_file(invoice_id):
    invoice = own_invoice(invoice_id)
    inline = request.args.get("ansehen") == "1"
    if invoice["status"] == "draft":
        doc, _ignored = build_doc(invoice)
        pdf = invoice_pdf.render(doc, draft=True, timesheet=timesheet_rows(invoice) if invoice["timesheet"] else None)
        return Response(pdf, mimetype="application/pdf",
                        headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="Entwurf-{invoice_id}.pdf"'})
    path = archive_dir(current_user.uid) / invoice["pdf_file"]
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != invoice["pdf_sha256"]:
        abort(410, _("Archivdatei fehlt oder wurde verändert."))
    return send_file(path, mimetype="application/pdf", as_attachment=not inline, download_name=path.name)


@bp.get("/rechnungen/<int:invoice_id>/<variant>.xml")
@invoicing_required
def invoice_xml(invoice_id, variant):
    invoice = own_invoice(invoice_id)
    if invoice["status"] == "draft" or variant not in ("zugferd", "xrechnung"):
        abort(404)
    doc, _ignored = build_doc(invoice)
    if variant == "xrechnung":
        missing = billing.xrechnung_problems(doc["seller"], doc["buyer"])
        if missing:
            flash(_("Für XRechnung fehlt: %(fields)s. Ergänzen und die Rechnung ggf. stornieren und neu erstellen.", fields=", ".join(_(m) for m in missing)), "error")
            return redirect(url_for("invoicing.invoice", invoice_id=invoice_id))
    xml = einvoice.build_cii(doc, xrechnung=variant == "xrechnung")
    name = f"{safe_filename(invoice['number'])}{'-xrechnung' if variant == 'xrechnung' else ''}.xml"
    return Response(xml, mimetype="application/xml", headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------------ Kunden

@bp.route("/kunden", methods=["GET", "POST"])
@login_required
def customers():
    if request.method == "POST":
        customer_id = request.form.get("customer_id", type=int)
        values = {key: clean(request.form.get(key), 120) for key in
                  ("number", "name", "contact", "street", "zip", "city", "email", "vat_id", "leitweg_id", "salutation")}
        values["country"] = clean(request.form.get("country"), 2).upper() or "DE"
        values["tax_mode"] = request.form.get("tax_mode", "domestic")
        values["language"] = request.form.get("language", "de")
        days = request.form.get("payment_days", "").strip()
        values["payment_days"] = int(days) if days.isdigit() and 0 <= int(days) <= 365 else None
        if (not values["name"] or values["tax_mode"] not in billing.CUSTOMER_TAX_MODES or values["language"] not in i18n.LANGUAGES or not re.fullmatch(r"[A-Z]{2}", values["country"])
                or (values["email"] and "@" not in values["email"]) or (days and values["payment_days"] is None)):
            flash(_("Bitte Name, Ländercode (z. B. DE), E-Mail und Zahlungsziel prüfen."), "error")
            return redirect(url_for("invoicing.customers", edit=customer_id))
        try:
            with store().connect() as connection:
                if customer_id:
                    columns = ", ".join(f"{key} = ?" for key in values)
                    if not store().customer(current_user.uid, customer_id):
                        abort(404)
                    connection.execute(f"UPDATE customers SET {columns} WHERE id = ?", (*values.values(), customer_id))
                    connection.execute("UPDATE projects SET client = ? WHERE customer_id = ?", (values["name"], customer_id))
                else:
                    columns = ", ".join(values)
                    customer_id = connection.execute(
                        f"INSERT INTO customers (user_id, {columns}) VALUES (?, {', '.join('?' * len(values))})",
                        (current_user.uid, *values.values())).lastrowid
        except sqlite3.IntegrityError:
            flash(_("Einen Kunden mit diesem Namen gibt es schon."), "error")
            return redirect(url_for("invoicing.customers"))
        flash(_("Kunde „%(name)s“ gespeichert.", name=values["name"]), "success")
        return redirect(url_for("invoicing.customers"))
    with store().connect() as connection:
        stats = {row["customer_id"]: dict(row) for row in connection.execute(
            "SELECT customer_id, COUNT(*) AS invoices, SUM(CASE WHEN status = 'open' AND type_code = '380' THEN gross_cents ELSE 0 END) AS open_cents "
            "FROM invoices WHERE user_id = ? GROUP BY customer_id", (current_user.uid,))}
        project_counts = {}
        for project in store().projects(current_user.uid):
            if project["customer_id"]:
                project_counts[project["customer_id"]] = project_counts.get(project["customer_id"], 0) + 1
    edit = store().customer(current_user.uid, request.args.get("edit", type=int)) if request.args.get("edit") else None
    all_customers = store().customers(current_user.uid)
    suggestion = ""
    numbers = [int(match.group(1)) for c in all_customers if (match := re.fullmatch(r"\D*(\d+)", c["number"] or ""))]
    if numbers:
        prefix = re.match(r"\D*", next(c["number"] for c in all_customers if re.fullmatch(r"\D*\d+", c["number"] or ""))).group(0)
        suggestion = f"{prefix}{max(numbers) + 1}"
    return render_template("customers.html", customers=all_customers, edit=edit, stats=stats, project_counts=project_counts,
                           suggestion=suggestion or "K-1001",
                           invoicing=store().invoice_settings(current_user.uid)["enabled"])


@bp.post("/kunden/<int:customer_id>/<action>")
@login_required
def manage_customer(customer_id, action):
    customer = store().customer(current_user.uid, customer_id)
    if not customer or action not in ("archive", "delete"):
        abort(404)
    with store().connect() as connection:
        if action == "archive":
            connection.execute("UPDATE customers SET archived = 1 - archived WHERE id = ?", (customer_id,))
        elif customer["shared"] and current_user.role != "admin":
            flash(_("Freigegebene Kunden kann nur %(owner)s löschen.", owner=customer["owner_name"]), "error")
            return redirect(url_for("invoicing.customers", edit=customer_id))
        elif connection.execute("SELECT 1 FROM invoices WHERE customer_id = ?", (customer_id,)).fetchone():
            flash(_("Kunden mit Rechnungen können nur archiviert werden."), "error")
            return redirect(url_for("invoicing.customers", edit=customer_id))
        else:
            connection.execute("UPDATE projects SET client = '' WHERE customer_id = ?", (customer_id,))
            connection.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
            flash(_("„%(name)s“ gelöscht.", name=customer["name"]), "success")
    return redirect(url_for("invoicing.customers"))


# ------------------------------------------------------------------ Einstellungen

TEXT_FIELDS = ("company", "contact_name", "street", "zip", "city", "email", "phone", "website", "tax_number", "vat_id",
               "bank_name", "iban", "bic", "account_holder")


@bp.route("/rechnungen/einstellungen", methods=["GET", "POST"])
@invoicing_required
def invoice_settings():
    if request.method == "POST":
        values = {key: clean(request.form.get(key), 120) for key in TEXT_FIELDS}
        values["iban"] = values["iban"].replace(" ", "").upper()
        values["bic"] = values["bic"].replace(" ", "").upper()
        values["country"] = clean(request.form.get("country"), 2).upper() or "DE"
        values["vat_mode"] = request.form.get("vat_mode")
        rate = billing.parse_rate(request.form.get("default_vat_rate"))
        values["number_format"] = request.form.get("number_format", "").strip()
        values["reset_yearly"] = 1 if request.form.get("reset_yearly") else 0
        values["position_mode"] = request.form.get("position_mode")
        values["rounding_minutes"] = request.form.get("rounding_minutes", type=int)
        values["timesheet"] = 1 if request.form.get("timesheet") else 0
        values["payment_days"] = request.form.get("payment_days", type=int)
        values["next_number"] = request.form.get("next_number", type=int)
        for key in ("intro", "footer"):
            # Leer = Standardtext, der in der jeweiligen Rechnungssprache eingesetzt wird
            values[key] = request.form.get(key, "").strip()[:2000] or None
        values["closing"] = request.form.get("closing", "").strip()[:300]
        errors = []
        if values["vat_mode"] not in billing.VAT_MODES or rate is None:
            errors.append("Umsatzsteuer")
        if not billing.valid_number_pattern(values["number_format"]):
            errors.append("Nummernformat (braucht z. B. {NNN})")
        if values["position_mode"] not in billing.POSITION_MODES or values["rounding_minutes"] not in billing.ROUNDING_CHOICES:
            errors.append("Positionen")
        if values["payment_days"] is None or not 0 <= values["payment_days"] <= 365:
            errors.append("Zahlungsziel")
        if values["next_number"] is None or not 1 <= values["next_number"] <= 10 ** 7:
            errors.append("nächste Nummer")
        if values["iban"] and not valid_iban(values["iban"]):
            errors.append("IBAN (Prüfziffer)")
        if values["email"] and "@" not in values["email"]:
            errors.append("E-Mail")
        if errors:
            flash(_("Bitte prüfen: %(fields)s.", fields=", ".join(_(error) for error in errors)), "error")
        else:
            values["default_vat_rate"] = str(rate)
            store().update_invoice_settings(current_user.uid, **values)
            flash(_("Rechnungseinstellungen gespeichert."), "success")
        return redirect(url_for("invoicing.invoice_settings"))
    settings = store().invoice_settings(current_user.uid)
    preview_day = today()
    counter = 1 if settings["reset_yearly"] and settings["number_year"] not in (None, preview_day.year) else settings["next_number"]
    return render_template(
        "invoice_settings.html", settings=settings, DEFAULT_INTRO=billing.DEFAULT_INTRO, DEFAULT_FOOTER=billing.DEFAULT_FOOTER,
        preview=billing.format_number(settings["number_format"], counter, preview_day),
    )


# ------------------------------------------------------------------ Layout

def sample_doc(settings, layout):
    """Beispielrechnung für die Live-Vorschau: echte Absenderdaten, Beispielkunde und -positionen."""
    from decimal import Decimal as D
    language = i18n.current()
    customer = next((c for c in store().customers(current_user.uid, include_archived=False) if c["street"]), None)
    buyer = billing.snapshot_buyer(customer or {"number": "K-1001", "name": "Muster GmbH", "contact": "", "street": "Beispielweg 1",
                                                "zip": "80331", "city": "München", "country": "DE"})
    seller = billing.snapshot_seller(settings)
    seller["company"] = seller["company"] or _("Deine Firma")
    rate = D("0") if settings["vat_mode"] == "small_business" else D(settings["default_vat_rate"])
    items = [{"description": _("Konstruktion und Entwurf"), "detail": _("Beispielposition aus erfassten Zeiten"),
              "quantity": D("3"), "unit": "DAY", "price_cents": 90000, "vat_rate": rate},
             {"description": _("Abstimmung"), "detail": "", "quantity": D("2.5"), "unit": "HUR", "price_cents": 11250, "vat_rate": rate}]
    for item in items:
        item["line_cents"] = billing.line_cents(item["quantity"], item["price_cents"])
    issue = today()
    mode = billing.tax_mode(settings, customer or {})
    doc = {"number": "RE-0000", "type_code": "380", "issue_date": issue, "due_date": issue + timedelta(days=settings["payment_days"]),
           "service_from": issue.replace(day=1), "service_to": issue, "buyer_reference": "", "preceding": None,
           "seller": seller, "buyer": buyer, "items": items, "tax_mode": mode, "language": language,
           "salutation": default_salutation(customer, language), "closing": _(billing.DEFAULT_CLOSING) if settings["closing"] in ("", billing.DEFAULT_CLOSING) else settings["closing"],
           "layout": layout, "logo": logo_path(current_user.uid)}
    summary = billing.totals(items, mode)
    doc["intro"] = billing.fill_placeholders(settings["intro"] or _(billing.DEFAULT_INTRO), doc, summary["gross"], settings["payment_days"], language)
    doc["footer"] = billing.fill_placeholders(settings["footer"] or _(billing.DEFAULT_FOOTER), doc, summary["gross"], settings["payment_days"], language)
    return doc


@bp.route("/rechnungen/layout", methods=["GET", "POST"])
@invoicing_required
def invoice_layout():
    settings = store().invoice_settings(current_user.uid)
    if request.method == "POST":
        layout = billing.layout_from_form(request.form)
        store().update_invoice_settings(current_user.uid, layout=json.dumps(layout))
        flash(_("Rechnungslayout gespeichert."), "success")
        return redirect(url_for("invoicing.invoice_layout"))
    return render_template("invoice_layout.html", layout=billing.layout_values(settings["layout"]), fonts=billing.PDF_FONTS,
                           logo_positions=billing.LOGO_POSITIONS, has_logo=logo_path(current_user.uid) is not None,
                           defaults=billing.LAYOUT_DEFAULTS, settings=settings)


@bp.route("/rechnungen/layout/vorschau", methods=["GET", "POST"])
@invoicing_required
def invoice_layout_preview():
    settings = store().invoice_settings(current_user.uid)
    layout = billing.layout_from_form(request.form) if request.form.get("font") else billing.layout_values(settings["layout"])
    pdf = invoice_pdf.render(sample_doc(settings, layout), draft=False)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": 'inline; filename="vorschau.pdf"'})


@bp.post("/rechnungen/layout/logo")
@invoicing_required
def invoice_logo():
    from PIL import Image, UnidentifiedImageError
    target = branding_dir(current_user.uid) / "logo.png"
    if request.form.get("action") == "delete":
        target.unlink(missing_ok=True)
        flash(_("Logo entfernt."), "success")
        return redirect(url_for("invoicing.invoice_layout"))
    upload = request.files.get("logo")
    try:
        with Image.open(upload.stream) as image:
            if image.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                raise UnidentifiedImageError()
            image.thumbnail((1600, 800))
            image = image.convert("RGBA")
            # PDF/A-sicher: Transparenz auf weißem Hintergrund flach rechnen
            flat = Image.new("RGB", image.size, (255, 255, 255))
            flat.paste(image, mask=image.getchannel("A"))
            box = flat.point(lambda value: 255 - value).getbbox()
            if box:
                flat = flat.crop(box)
            flat.save(target, format="PNG", optimize=True)
            target.chmod(0o600)
        flash(_("Logo gespeichert."), "success")
    except (AttributeError, OSError, UnidentifiedImageError, ValueError):
        flash(_("Bitte ein Bild als PNG, JPG, WebP oder GIF hochladen (max. 4 MB)."), "error")
    return redirect(url_for("invoicing.invoice_layout"))


@bp.get("/rechnungen/layout/logo.png")
@invoicing_required
def invoice_logo_file():
    path = logo_path(current_user.uid)
    if not path:
        abort(404)
    return send_file(path, mimetype="image/png", max_age=0)


def valid_iban(iban):
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    digits = "".join(str(int(char, 36)) for char in iban[4:] + iban[:4])
    return int(digits) % 97 == 1

