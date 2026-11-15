import base64
import csv
import io
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path

import pyotp
import qrcode
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from . import clock
from .security import (DUMMY_HASH, clear_failures, hasher, load_keys, new_api_token, password_valid, rate_limited,
                       record_failure, recovery_hash, token_hash)
from .store import Store
from .timecalc import (ABSENCE_LABELS, REGIONS, WEEKDAYS, amount_cents, balance, day_bounds, day_report, days_between,
                       fmt_duration, fmt_money, fmt_time, parse_local, project_totals, range_report, required_break_net,
                       target_seconds, to_local, today, vacation_used)

SOURCE_LABELS = {"web": "Stempeluhr", "manual": "Nachgetragen", "api": "API", "heartbeat": "Lebenszeichen", "device": "Gerät"}
PROJECT_COLORS = ["blue", "green", "orange", "purple", "red", "teal", "yellow", "grey"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.role = row["role"]
        self.epoch = row["epoch"]
        self.totp_enabled = bool(row["totp_secret"])
        self.created_at = row["created_at"]

    def get_id(self):
        return f"{self.id}:{self.epoch}"

    @property
    def uid(self):
        return int(self.id)


def parse_hours(text):
    """'38,5', '38.5' oder '38:30' → Minuten."""
    text = (text or "").strip().replace(",", ".")
    match = re.fullmatch(r"(-?)(\d{1,4})(?::(\d{1,2}))?", text)
    if match:
        minutes = int(match.group(2)) * 60 + int(match.group(3) or 0)
        if match.group(3) and int(match.group(3)) > 59:
            return None
        return -minutes if match.group(1) else minutes
    try:
        return round(float(text) * 60)
    except ValueError:
        return None


def parse_day(text):
    try:
        return date.fromisoformat((text or "").strip())
    except ValueError:
        return None


def parse_money(text):
    """'85', '85,50' oder '' → Cent bzw. None."""
    text = (text or "").strip().replace("€", "").replace(" ", "").replace(".", "").replace(",", ".")
    if not text:
        return None
    try:
        value = round(float(text) * 100)
    except ValueError:
        return -1
    return value if 0 <= value <= 10_000_000 else -1


def finish_estimate(report, now):
    """Uhrzeit, zu der das Tagessoll inkl. gesetzlicher Pause erreicht ist."""
    remaining = report["target"] - report["net"]
    if remaining <= 0 or not report["open"]:
        return None
    for extra in range(60, 16 * 3600, 60):
        if required_break_net(report["worked"] + extra, report["breaks"]) >= report["target"]:
            return now + extra
    return None


def create_app(test_config=None):
    os.umask(0o077)
    app = Flask(__name__)
    app.config.update(
        DATA_DIR=os.getenv("STEMPEL_DATA_DIR", "/data"),
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=int(os.getenv("SESSION_DAYS", "14"))),
        MAX_CONTENT_LENGTH=64 * 1024,
        ENABLE_MONITOR=os.getenv("ENABLE_MONITOR", "true").lower() == "true",
        MONITOR_INTERVAL=max(20, int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))),
    )
    if test_config:
        app.config.update(test_config)
    data_dir = Path(app.config["DATA_DIR"])
    data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    app.secret_key, cipher = load_keys(data_dir)
    store = Store(data_dir / "stempel.db")
    monitor = clock.Monitor(store, app.config["MONITOR_INTERVAL"])
    app.extensions["stempel_store"] = store
    app.extensions["stempel_monitor"] = monitor
    csrf = CSRFProtect(app)
    login_manager = LoginManager(app)
    login_manager.login_view = "login"
    if os.getenv("TRUST_PROXY", "false").lower() == "true":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=0, x_for=0)

    app.jinja_env.filters.update(duration=fmt_duration, clocktime=fmt_time, money=fmt_money,
                                 stamp=lambda ts: to_local(ts).strftime("%d.%m.%Y %H:%M"),
                                 hours=lambda seconds: f"{seconds / 3600:.2f}".replace(".", ","))
    app.jinja_env.globals.update(
        signed=lambda seconds: fmt_duration(seconds, signed=True), WEEKDAYS=WEEKDAYS, ABSENCE_LABELS=ABSENCE_LABELS,
        SOURCE_LABELS=SOURCE_LABELS, REGIONS=REGIONS, MONTHS=MONTHS, PROJECT_COLORS=PROJECT_COLORS,
    )

    @app.context_processor
    def navigation_context():
        if current_user.is_authenticated:
            return {"nav_profile": store.profile(current_user.uid)}
        return {}

    @login_manager.user_loader
    def load_user(identifier):
        try:
            user_id, epoch = (int(value) for value in identifier.split(":"))
        except (ValueError, AttributeError):
            return None
        with store.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ? AND active = 1 AND epoch = ?", (user_id, epoch)).fetchone()
        return User(row) if row else None

    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith("/api/"):
            return jsonify(error="Anmeldung erforderlich"), 401
        return redirect(url_for("login"))

    def admin_required(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role != "admin":
                abort(403)
            return view(*args, **kwargs)
        return wrapped

    def subjects(username):
        return ["user:" + username.casefold(), "ip:" + (request.remote_addr or "unknown")]

    def verify_second_factor(user_id, code):
        code = code.strip().upper()
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute("SELECT totp_secret, last_totp_step FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone()
            if not user or not user["totp_secret"]:
                return False
            totp = pyotp.TOTP(cipher.decrypt(user["totp_secret"].encode()).decode())
            current_step = int(time.time()) // totp.interval
            for step in (current_step - 1, current_step, current_step + 1):
                if step > user["last_totp_step"] and secrets.compare_digest(totp.at(step * totp.interval), code):
                    connection.execute("UPDATE users SET last_totp_step = ? WHERE id = ?", (step, user_id))
                    return True
            return bool(connection.execute(
                "DELETE FROM recovery_codes WHERE user_id = ? AND code_hash = ?", (user_id, recovery_hash(code)),
            ).rowcount)

    def own_entry(entry_id):
        entry = store.entry(current_user.uid, entry_id)
        if not entry:
            abort(404)
        return entry

    def project_from_form(field="project_id"):
        """(ok, project_id): leer = ohne Projekt; fremde IDs sind ungültig."""
        value = request.form.get(field, "").strip()
        if not value:
            return True, None
        project = store.project(current_user.uid, int(value)) if value.isdigit() else None
        return (True, project["id"]) if project else (False, None)

    def project_summary(reports, projects):
        totals = project_totals(reports)
        by_id = {project["id"]: project for project in projects}
        rows = []
        for project_id, seconds in sorted(totals.items(), key=lambda item: -item[1]):
            project = by_id.get(project_id)
            rows.append({
                "project": project, "seconds": seconds,
                "amount": amount_cents(seconds, project["rate_cents"]) if project else 0,
            })
        return rows

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/health")
    def health():
        return {"status": "ok"}

    # ---------------------------------------------------------------- Anmeldung

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()[:80]
            password = request.form.get("password", "")[:1024]
            keys = subjects(username)
            if rate_limited(store, keys):
                flash("Zu viele Versuche. Bitte später erneut versuchen.", "error")
            else:
                with store.connect() as connection:
                    row = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
                valid = password_valid(password, row["password_hash"] if row else DUMMY_HASH)
                if not row or not row["active"] or not valid:
                    record_failure(store, keys)
                    flash("Anmeldung fehlgeschlagen.", "error")
                elif row["totp_secret"]:
                    session.clear()
                    session.update(pending_user=row["id"], pending_epoch=row["epoch"], pending_since=int(time.time()))
                    return redirect(url_for("login_otp"))
                else:
                    clear_failures(store, keys)
                    session.clear()
                    login_user(User(row))
                    session.permanent = True
                    return redirect(url_for("index"))
        return render_template("login.html", otp=False)

    @app.route("/login/2fa", methods=["GET", "POST"])
    def login_otp():
        user_id = session.get("pending_user")
        if not user_id or int(time.time()) - session.get("pending_since", 0) > 300:
            session.clear()
            return redirect(url_for("login"))
        with store.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ? AND active = 1 AND epoch = ?", (user_id, session.get("pending_epoch"))).fetchone()
        if not row:
            session.clear()
            return redirect(url_for("login"))
        if request.method == "POST":
            keys = subjects(row["username"])
            if rate_limited(store, keys):
                flash("Zu viele Versuche. Bitte später erneut versuchen.", "error")
            elif verify_second_factor(user_id, request.form.get("code", "")):
                clear_failures(store, keys)
                session.clear()
                login_user(User(row))
                session.permanent = True
                return redirect(url_for("index"))
            else:
                record_failure(store, keys)
                flash("Anmeldung fehlgeschlagen.", "error")
        return render_template("login.html", otp=True)

    @app.post("/logout")
    @login_required
    def logout():
        logout_user()
        session.clear()
        return redirect(url_for("login"))

    # ---------------------------------------------------------------- Heute

    @app.get("/")
    @login_required
    def index():
        now = int(time.time())
        profile = store.profile(current_user.uid)
        current_day = today()
        monday = current_day - timedelta(days=current_day.weekday())
        week = range_report(store, current_user.uid, profile, monday, monday + timedelta(days=6), now)
        report = week[current_day.weekday()]
        yesterday_balance = balance(store, current_user.uid, profile, current_user.created_at, current_day - timedelta(days=1), now)
        year = current_day.year
        projects = store.projects(current_user.uid)
        month = range_report(store, current_user.uid, profile, current_day.replace(day=1), current_day, now)
        month_projects = project_summary(month, projects)
        return render_template(
            "index.html", now=now, profile=profile, report=report, week=week,
            week_net=sum(day["net"] for day in week), week_target=sum(day["target"] for day in week),
            balance=yesterday_balance, balance_now=yesterday_balance + report["diff"],
            open_entry=store.open_entry(current_user.uid), finish=finish_estimate(report, now),
            vacation_left=profile["vacation_days"] - vacation_used(store, current_user.uid, profile, year),
            device=monitor.snapshot(current_user.uid), projects=[p for p in projects if not p["archived"]],
            last_project=store.last_project_id(current_user.uid), month_projects=month_projects,
            month_worked=sum(row["seconds"] for row in month_projects), month_amount=sum(row["amount"] for row in month_projects),
            today_projects=project_summary([report], projects),
        )

    @app.post("/stamp")
    @login_required
    def stamp():
        action = request.form.get("action")
        valid, project_id = project_from_form()
        if not valid:
            abort(400)
        note = request.form.get("note", "").strip()
        if action == "in":
            if not clock.clock_in(store, current_user.uid, "web", note=note, project_id=project_id):
                flash("Du bist bereits eingestempelt.", "error")
        elif action == "switch":
            if clock.switch(store, current_user.uid, project_id, "web", note=note):
                project = store.project(current_user.uid, project_id) if project_id else None
                flash(f"Gewechselt zu {project['name'] if project else 'ohne Projekt'}.", "success")
        elif action == "out":
            if not clock.clock_out(store, current_user.uid):
                flash("Du bist nicht eingestempelt.", "error")
        else:
            abort(400)
        return redirect(url_for("index"))

    # ---------------------------------------------------------------- Zeiten

    def month_from_request():
        text = request.args.get("monat", "")
        try:
            first = datetime.strptime(text, "%Y-%m").date()
        except ValueError:
            first = today().replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        return first, following - timedelta(days=1), following

    @app.get("/zeiten")
    @login_required
    def times():
        now = int(time.time())
        first, last, following = month_from_request()
        profile = store.profile(current_user.uid)
        project_filter = request.args.get("projekt", type=int)
        reports = range_report(store, current_user.uid, profile, first, last, now, project_filter)
        projects = store.projects(current_user.uid)
        edit = own_entry(request.args.get("edit", type=int)) if request.args.get("edit") else None
        if edit:
            edit["start_day"] = to_local(edit["start_at"]).date().isoformat()
        until = min(last, today())
        month_balance = balance(store, current_user.uid, profile, current_user.created_at, until, now)
        return render_template(
            "times.html", reports=reports, first=first, edit=edit, today=today(),
            previous=(first - timedelta(days=1)).strftime("%Y-%m"), following=following.strftime("%Y-%m"),
            total_net=sum(r["net"] for r in reports), total_target=sum(r["target"] for r in reports if r["day"] <= until),
            total_diff=sum(r["diff"] for r in reports if r["day"] <= until), month_balance=month_balance,
            default_day=request.args.get("tag") or min(max(today(), first), last).isoformat(),
            projects=projects, project_filter=project_filter, month_projects=project_summary(reports, projects),
            last_project=store.last_project_id(current_user.uid),
        )

    def entry_from_form(exclude_id=None, allow_open=False):
        day = parse_day(request.form.get("day"))
        start_text = request.form.get("start", "").strip()
        end_text = request.form.get("end", "").strip()
        note = request.form.get("note", "").strip()[:200]
        try:
            start = parse_local(day.isoformat(), start_text) if day else None
            end = parse_local(day.isoformat(), end_text) if day and end_text else None
        except ValueError:
            start = None
        if start is None or (end is None and not allow_open):
            flash("Bitte Datum, Beginn und Ende im Format HH:MM angeben.", "error")
            return None
        if end is not None and end <= start:
            end += 86400
        if end is not None and end - start > 24 * 3600:
            flash("Ein Eintrag darf höchstens 24 Stunden lang sein.", "error")
            return None
        if end is None and start > time.time():
            flash("Ein laufender Eintrag kann nicht in der Zukunft beginnen.", "error")
            return None
        if store.overlaps(current_user.uid, start, end, exclude_id):
            flash("Der Eintrag überschneidet sich mit einem vorhandenen Eintrag.", "error")
            return None
        valid, project_id = project_from_form()
        if not valid:
            flash("Unbekanntes Projekt.", "error")
            return None
        return start, end, note, day, project_id

    @app.post("/entries")
    @login_required
    def add_entry():
        values = entry_from_form()
        if values:
            start, end, note, day, project_id = values
            with store.connect() as connection:
                connection.execute(
                    "INSERT INTO entries (user_id, start_at, end_at, source, note, project_id) VALUES (?, ?, ?, 'manual', ?, ?)",
                    (current_user.uid, start, end, note, project_id),
                )
            flash("Eintrag gespeichert.", "success")
            return redirect(url_for("times", monat=day.strftime("%Y-%m")))
        return redirect(url_for("times", monat=request.form.get("day", "")[:7]))

    @app.post("/entries/<int:entry_id>")
    @login_required
    def update_entry(entry_id):
        entry = own_entry(entry_id)
        values = entry_from_form(exclude_id=entry_id, allow_open=entry["end_at"] is None)
        if not values:
            return redirect(url_for("times", monat=request.form.get("day", "")[:7], edit=entry_id))
        start, end, note, day, project_id = values
        with store.connect() as connection:
            connection.execute(
                "UPDATE entries SET start_at = ?, end_at = ?, note = ?, project_id = ?, "
                "source = CASE WHEN ? IS NULL THEN source ELSE 'manual' END WHERE id = ?",
                (start, end, note, project_id, end, entry_id),
            )
        flash("Eintrag aktualisiert.", "success")
        return redirect(url_for("times", monat=day.strftime("%Y-%m")))

    @app.post("/entries/<int:entry_id>/delete")
    @login_required
    def delete_entry(entry_id):
        entry = own_entry(entry_id)
        with store.connect() as connection:
            connection.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
        flash("Eintrag gelöscht.", "success")
        return redirect(url_for("times", monat=to_local(entry["start_at"]).strftime("%Y-%m")))

    @app.get("/zeiten.csv")
    @login_required
    def times_csv():
        first, last, _ = month_from_request()
        profile = store.profile(current_user.uid)
        project_filter = request.args.get("projekt", type=int)
        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        reports = range_report(store, current_user.uid, profile, first, last, project_id=project_filter)
        suffix = ""
        if project_filter:
            project = store.project(current_user.uid, project_filter)
            suffix = "-" + re.sub(r"[^A-Za-z0-9_-]+", "-", project["name"]).strip("-") if project else ""
        if request.args.get("art") == "eintraege":
            writer.writerow(["Datum", "Beginn", "Ende", "Dauer", "Stunden", "Projekt", "Kunde", "Stundensatz", "Betrag", "Notiz"])
            for report in reports:
                for begin, finish, entry in report["segments"]:
                    seconds = finish - begin
                    rate = entry["project_rate"]
                    writer.writerow([
                        report["day"].strftime("%d.%m.%Y"), fmt_time(begin), fmt_time(finish), fmt_duration(seconds),
                        f"{seconds / 3600:.2f}".replace(".", ","), entry["project_name"] or "", entry["project_client"] or "",
                        f"{rate / 100:.2f}".replace(".", ",") if rate else "",
                        f"{amount_cents(seconds, rate) / 100:.2f}".replace(".", ",") if rate else "", entry["note"],
                    ])
            filename = f"stempel-eintraege-{first:%Y-%m}{suffix}.csv"
            return Response("\ufeff" + output.getvalue(), mimetype="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="{filename}"'})
        writer.writerow(["Datum", "Tag", "Zeiten", "Brutto", "Pausenabzug", "Netto", "Netto (h)", "Soll", "Differenz", "Hinweis", "Projekte", "Notizen"])
        for report in reports:
            spans = ", ".join(f"{fmt_time(begin)}–{fmt_time(finish)}" for begin, finish, _ in report["segments"])
            hint = report["holiday"] or (ABSENCE_LABELS[report["absence"]["kind"]] + (" (½)" if report["absence"]["half"] else "") if report["absence"] else "")
            notes = " | ".join(entry["note"] for _, _, entry in report["segments"] if entry["note"])
            project_names = ", ".join(dict.fromkeys(entry["project_name"] for _, _, entry in report["segments"] if entry["project_name"]))
            writer.writerow([
                report["day"].strftime("%d.%m.%Y"), report["weekday"], spans, fmt_duration(report["worked"]),
                fmt_duration(report["deducted"]), fmt_duration(report["net"]), f"{report['net'] / 3600:.2f}".replace(".", ","),
                fmt_duration(report["target"]), fmt_duration(report["diff"], signed=True), hint, project_names, notes,
            ])
        filename = f"stempel-{first:%Y-%m}{suffix}.csv"
        return Response("﻿" + output.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ---------------------------------------------------------------- Projekte

    @app.route("/projekte", methods=["GET", "POST"])
    @login_required
    def projects():
        if request.method == "POST":
            project_id = request.form.get("project_id", type=int)
            name = " ".join(request.form.get("name", "").split())[:80]
            client = " ".join(request.form.get("client", "").split())[:80]
            rate = parse_money(request.form.get("rate"))
            color = request.form.get("color", "blue")
            if not name or rate == -1 or color not in PROJECT_COLORS:
                flash("Bitte einen Namen und ggf. einen gültigen Stundensatz angeben.", "error")
                return redirect(url_for("projects", edit=project_id) if project_id else url_for("projects"))
            try:
                with store.connect() as connection:
                    if project_id:
                        if not connection.execute(
                                "UPDATE projects SET name = ?, client = ?, rate_cents = ?, color = ? WHERE id = ? AND user_id = ?",
                                (name, client, rate, color, project_id, current_user.uid)).rowcount:
                            abort(404)
                    else:
                        connection.execute("INSERT INTO projects (user_id, name, client, rate_cents, color) VALUES (?, ?, ?, ?, ?)",
                                           (current_user.uid, name, client, rate, color))
            except sqlite3.IntegrityError:
                flash("Ein Projekt mit diesem Namen gibt es schon.", "error")
                return redirect(url_for("projects"))
            flash(f"Projekt „{name}“ gespeichert.", "success")
            return redirect(url_for("projects"))
        first, last, following = month_from_request()
        profile = store.profile(current_user.uid)
        all_projects = store.projects(current_user.uid)
        month = project_summary(range_report(store, current_user.uid, profile, first, last), all_projects)
        month_by_id = {row["project"]["id"] if row["project"] else None: row for row in month}
        with store.connect() as connection:
            totals = {row["project_id"]: row["seconds"] for row in connection.execute(
                "SELECT project_id, SUM(end_at - start_at) AS seconds FROM entries WHERE user_id = ? AND end_at IS NOT NULL GROUP BY project_id",
                (current_user.uid,))}
        edit = store.project(current_user.uid, request.args.get("edit", type=int)) if request.args.get("edit") else None
        return render_template(
            "projects.html", projects=all_projects, month=month_by_id, totals=totals, first=first, edit=edit,
            previous=(first - timedelta(days=1)).strftime("%Y-%m"), following=following.strftime("%Y-%m"),
            month_seconds=sum(row["seconds"] for row in month), month_amount=sum(row["amount"] for row in month),
        )

    @app.post("/projekte/<int:project_id>/<action>")
    @login_required
    def manage_project(project_id, action):
        project = store.project(current_user.uid, project_id)
        if not project or action not in ("archive", "delete"):
            abort(404)
        with store.connect() as connection:
            if action == "archive":
                connection.execute("UPDATE projects SET archived = 1 - archived WHERE id = ?", (project_id,))
                flash(f"„{project['name']}“ {'wieder aktiv' if project['archived'] else 'archiviert'}.", "success")
            else:
                connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                flash(f"„{project['name']}“ gelöscht. Die Einträge bleiben ohne Projekt erhalten.", "success")
        return redirect(url_for("projects"))

    # ---------------------------------------------------------------- Abwesenheit

    @app.route("/abwesenheit", methods=["GET", "POST"])
    @login_required
    def absences():
        profile = store.profile(current_user.uid)
        if request.method == "POST":
            first, last = parse_day(request.form.get("from")), parse_day(request.form.get("to") or request.form.get("from"))
            kind = request.form.get("kind")
            half = 1 if request.form.get("half") == "1" else 0
            if not first or not last or last < first or (last - first).days > 366 or kind not in ABSENCE_LABELS:
                flash("Bitte einen gültigen Zeitraum und eine Art angeben.", "error")
            else:
                days = [day for day in days_between(first, last) if target_seconds(profile, day)]
                if half and len(days) != 1:
                    flash("Halbe Tage bitte einzeln eintragen.", "error")
                elif not days:
                    flash("Im Zeitraum liegen keine Arbeitstage.", "error")
                else:
                    with store.connect() as connection:
                        connection.executemany(
                            "INSERT OR REPLACE INTO absences (user_id, day, kind, half) VALUES (?, ?, ?, ?)",
                            [(current_user.uid, day.isoformat(), kind, half) for day in days],
                        )
                    flash(f"{len(days) / (2 if half else 1):g} Arbeitstag(e) als {ABSENCE_LABELS[kind]} eingetragen.", "success")
            return redirect(url_for("absences", jahr=(first or today()).year))
        year = request.args.get("jahr", type=int) or today().year
        entries = store.absences_between(current_user.uid, f"{year}-01-01", f"{year}-12-31")
        grouped = []
        for day_text, absence in entries.items():
            day = date.fromisoformat(day_text)
            last_group = grouped[-1] if grouped else None
            if (last_group and last_group["kind"] == absence["kind"] and not absence["half"] and not last_group["half"]
                    and all(not target_seconds(profile, gap) for gap in days_between(last_group["to"] + timedelta(days=1), day - timedelta(days=1)))):
                last_group["to"] = day
                last_group["days"] += 1
            else:
                grouped.append({"from": day, "to": day, "kind": absence["kind"], "half": absence["half"], "days": 0.5 if absence["half"] else 1})
        used = vacation_used(store, current_user.uid, profile, year)
        sick = sum(0.5 if a["half"] else 1 for a in entries.values() if a["kind"] == "sick")
        return render_template("absences.html", year=year, groups=grouped, used=used, sick=sick, profile=profile, today=today())

    @app.post("/abwesenheit/delete")
    @login_required
    def delete_absence():
        first, last = parse_day(request.form.get("from")), parse_day(request.form.get("to"))
        if not first or not last:
            abort(400)
        with store.connect() as connection:
            connection.execute("DELETE FROM absences WHERE user_id = ? AND day BETWEEN ? AND ?",
                               (current_user.uid, first.isoformat(), last.isoformat()))
        flash("Abwesenheit entfernt.", "success")
        return redirect(url_for("absences", jahr=first.year))

    # ---------------------------------------------------------------- Automatik

    @app.route("/automatik", methods=["GET", "POST"])
    @login_required
    def automation():
        if request.method == "POST":
            action = request.form.get("action")
            if action == "device":
                host = request.form.get("device_host", "").strip()
                idle = request.form.get("idle_minutes", type=int)
                if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.\-:]*", host) or host.startswith("-") or idle is None or not 2 <= idle <= 240:
                    flash("Bitte einen gültigen Hostnamen bzw. eine IP und eine Leerlaufzeit von 2–240 Minuten angeben.", "error")
                else:
                    valid, project_id = project_from_form("default_project_id")
                    store.update_profile(current_user.uid, device_host=host, idle_minutes=idle,
                                         default_project_id=project_id if valid else None)
                    flash("Automatik gespeichert.", "success")
            elif action == "test":
                host = store.profile(current_user.uid)["device_host"]
                if host:
                    online = clock.probe(host)
                    flash(f"{host} ist erreichbar." if online else f"{host} antwortet nicht.", "success" if online else "error")
            elif action == "token":
                name = request.form.get("name", "").strip()[:60] or "Token"
                token, hashed = new_api_token()
                with store.connect() as connection:
                    connection.execute("INSERT INTO api_tokens (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                                       (current_user.uid, name, hashed, int(time.time())))
                session["new_token"] = token
            elif action == "revoke":
                with store.connect() as connection:
                    connection.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?",
                                       (request.form.get("token_id", type=int), current_user.uid))
                flash("Token widerrufen.", "success")
            return redirect(url_for("automation"))
        with store.connect() as connection:
            tokens = [dict(row) for row in connection.execute(
                "SELECT id, name, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC", (current_user.uid,))]
        return render_template("automation.html", profile=store.profile(current_user.uid), tokens=tokens,
                               projects=store.projects(current_user.uid, include_archived=False),
                               new_token=session.pop("new_token", None), device=monitor.snapshot(current_user.uid),
                               base_url=request.host_url.rstrip("/"))

    @app.get("/automatik/agent/<system>")
    @login_required
    def agent_script(system):
        files = {"windows": ("agent_windows.txt", "stempel-agent.ps1"), "unix": ("agent_unix.txt", "stempel-agent.sh")}
        if system not in files:
            abort(404)
        template, filename = files[system]
        body = render_template(template, base_url=request.host_url.rstrip("/"))
        return Response(body, mimetype="text/plain", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    # ---------------------------------------------------------------- API

    def api_user():
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.lower().startswith("bearer ") else ""
        keys = ["ip:" + (request.remote_addr or "unknown")]
        if not token or rate_limited(store, keys):
            return None
        with store.connect() as connection:
            row = connection.execute(
                "SELECT users.id, api_tokens.id AS token_id FROM api_tokens JOIN users ON users.id = api_tokens.user_id "
                "WHERE token_hash = ? AND users.active = 1", (token_hash(token),)).fetchone()
            if row:
                connection.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (int(time.time()), row["token_id"]))
        if not row:
            record_failure(store, keys)
            return None
        return row["id"]

    def api_state(user_id, result=None):
        now = int(time.time())
        with store.connect() as connection:
            created = connection.execute("SELECT created_at FROM users WHERE id = ?", (user_id,)).fetchone()["created_at"]
        profile = store.profile(user_id)
        current_day = today()
        start, end = day_bounds(current_day)
        absence = store.absences_between(user_id, current_day.isoformat(), current_day.isoformat()).get(current_day.isoformat())
        report = day_report(profile, current_day, store.entries_between(user_id, start, end), absence, now)
        saldo = balance(store, user_id, profile, created, current_day, now)
        entry = store.open_entry(user_id)
        return {
            "state": "in" if entry else "out",
            "since": to_local(entry["start_at"]).isoformat() if entry else None,
            "source": entry["source"] if entry else None,
            "project": entry["project_name"] if entry else None,
            "today_net_minutes": report["net"] // 60,
            "today_target_minutes": report["target"] // 60,
            "balance_minutes": saldo // 60,
            "message": api_message(result, entry, report, saldo, now, bool(profile["track_balance"])),
        }

    def api_message(result, entry, report, saldo, now, profile_tracks_balance=True):
        today_text = f"heute {fmt_duration(report['net'])} h"
        if report["target"]:
            today_text += f" von {fmt_duration(report['target'])} h"
        summary = today_text
        if profile_tracks_balance:
            summary += f" · Saldo {fmt_duration(saldo, signed=True)}"
        if entry:
            since = fmt_time(entry["start_at"])
            project = f" · {entry['project_name']}" if entry["project_name"] else ""
            if result == "started":
                lead = f"✅ Eingestempelt um {since}{project}"
            elif result == "switched":
                lead = f"🔀 Gewechselt um {since} zu {entry['project_name'] or 'ohne Projekt'}"
            elif result == "unchanged":
                lead = f"Schon eingestempelt seit {since}{project}"
            else:
                lead = f"⏱ Läuft seit {since}{project}"
            finish = finish_estimate(report, now)
            return f"{lead} · {today_text}" + (f" · Soll erreicht ca. {fmt_time(finish)}" if finish else "")
        if result == "stopped":
            return f"👋 Ausgestempelt um {fmt_time(now)} · {summary}"
        if result == "suppressed":
            return f"Automatik pausiert (manuell ausgestempelt) · {summary}"
        return f"Nicht eingestempelt · {summary}"

    def api_response(payload, status=200):
        wants_text = request.args.get("format") == "text" or "text/plain" in request.headers.get("Accept", "").split(",")[0]
        if wants_text:
            return Response(payload.get("message") or payload.get("error", ""), status=status, mimetype="text/plain")
        return jsonify(payload), status

    @app.post("/api/v1/clock")
    @csrf.exempt
    def api_clock():
        user_id = api_user()
        if user_id is None:
            return api_response({"error": "Ungültiges oder fehlendes Token"}, 401)
        data = request.get_json(silent=True) or request.form
        action = str(data.get("action") or request.args.get("action") or "toggle").strip().lower()
        reference = data.get("project") or request.args.get("project")
        project_id = None
        if reference not in (None, ""):
            project = store.find_project(user_id, reference)
            if not project:
                return api_response({"error": f"Projekt „{reference}“ nicht gefunden"}, 400)
            project_id = project["id"]
        if action == "in":
            entry = store.open_entry(user_id)
            if entry and project_id is not None and entry["project_id"] != project_id:
                result = "switched" if clock.switch(store, user_id, project_id, "api") else "unchanged"
            else:
                result = "started" if clock.clock_in(store, user_id, "api", project_id=project_id) else "unchanged"
        elif action == "out":
            result = "stopped" if clock.clock_out(store, user_id) else "unchanged"
        elif action == "toggle":
            result = {"in": "started", "out": "stopped", "switched": "switched"}[clock.toggle(store, user_id, "api", project_id=project_id)]
        elif action == "heartbeat":
            result = clock.signal(store, user_id, "heartbeat")
        else:
            return api_response({"error": "action muss in, out, toggle oder heartbeat sein"}, 400)
        return api_response({"result": result, **api_state(user_id, result)})

    @app.get("/api/v1/status")
    def api_status():
        user_id = api_user()
        if user_id is None:
            return api_response({"error": "Ungültiges oder fehlendes Token"}, 401)
        return api_response(api_state(user_id))

    # ---------------------------------------------------------------- Konto

    @app.route("/account", methods=["GET", "POST"])
    @login_required
    def account():
        if request.method == "POST":
            action = request.form.get("action")
            with store.connect() as connection:
                row = connection.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
            if action == "model":
                weekly = parse_hours(request.form.get("weekly_hours"))
                workdays = "".join(sorted(set(day for day in request.form.getlist("workdays") if day in "0123456" and len(day) == 1)))
                region = request.form.get("region", "")
                offset = parse_hours(request.form.get("balance_offset") or "0")
                start_text = request.form.get("balance_start", "").strip()
                try:
                    vacation = float(request.form.get("vacation_days", "30").replace(",", "."))
                except ValueError:
                    vacation = -1
                if (weekly is None or not 0 <= weekly <= 6000 or region not in REGIONS or offset is None
                        or not 0 <= vacation <= 100 or (start_text and not parse_day(start_text))):
                    flash("Bitte die Angaben zum Arbeitszeitmodell prüfen.", "error")
                else:
                    store.update_profile(current_user.uid, track_balance=1 if request.form.get("track_balance") else 0,
                                         weekly_minutes=weekly, workdays=workdays, region=region,
                                         break_rule=1 if request.form.get("break_rule") else 0, vacation_days=vacation,
                                         balance_start=start_text or None, balance_offset=offset)
                    flash("Arbeitszeitmodell gespeichert.", "success")
            elif action == "password":
                keys = subjects(current_user.username)
                new_password = request.form.get("new_password", "")
                if not rate_limited(store, keys) and len(new_password) >= 12 and password_valid(request.form.get("current_password", ""), row["password_hash"]):
                    with store.connect() as connection:
                        connection.execute("UPDATE users SET password_hash = ?, epoch = epoch + 1 WHERE id = ?", (hasher.hash(new_password), current_user.id))
                    logout_user()
                    session.clear()
                    return redirect(url_for("login"))
                record_failure(store, keys)
                flash("Passwort konnte nicht geändert werden.", "error")
            elif action == "start" and not row["totp_secret"]:
                secret = pyotp.random_base32()
                with store.connect() as connection:
                    connection.execute("UPDATE users SET totp_pending = ? WHERE id = ?", (cipher.encrypt(secret.encode()).decode(), current_user.id))
            elif action == "confirm" and row["totp_pending"] and not row["totp_secret"]:
                secret = cipher.decrypt(row["totp_pending"].encode()).decode()
                keys = subjects(current_user.username)
                totp = pyotp.TOTP(secret)
                current_step = int(time.time()) // totp.interval
                code = request.form.get("code", "").strip()
                matched_step = next((step for step in (current_step - 1, current_step, current_step + 1)
                                     if secrets.compare_digest(totp.at(step * totp.interval), code)), None)
                if not rate_limited(store, keys) and matched_step is not None:
                    codes = [secrets.token_hex(5).upper() for _ in range(8)]
                    with store.connect() as connection:
                        connection.execute("UPDATE users SET totp_secret = totp_pending, totp_pending = NULL, last_totp_step = ? WHERE id = ?", (matched_step, current_user.id))
                        connection.executemany("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)", [(current_user.id, recovery_hash(c)) for c in codes])
                    return render_template("recovery.html", codes=codes)
                record_failure(store, keys)
                flash("Der Code ist ungültig.", "error")
            elif action == "disable" and row["totp_secret"]:
                keys = subjects(current_user.username)
                if not rate_limited(store, keys) and password_valid(request.form.get("password", ""), row["password_hash"]) and verify_second_factor(current_user.id, request.form.get("code", "")):
                    with store.connect() as connection:
                        connection.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL, epoch = epoch + 1 WHERE id = ?", (current_user.id,))
                        connection.execute("DELETE FROM recovery_codes WHERE user_id = ?", (current_user.id,))
                    logout_user()
                    session.clear()
                    return redirect(url_for("login"))
                record_failure(store, keys)
                flash("Bestätigung fehlgeschlagen.", "error")
            return redirect(url_for("account"))
        with store.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ?", (current_user.id,)).fetchone()
        qr = secret = None
        if row["totp_pending"] and not row["totp_secret"]:
            secret = cipher.decrypt(row["totp_pending"].encode()).decode()
            image = qrcode.make(pyotp.TOTP(secret).provisioning_uri(name=current_user.username, issuer_name="Stempel"))
            output = io.BytesIO()
            image.save(output, format="PNG")
            qr = base64.b64encode(output.getvalue()).decode()
        profile = store.profile(current_user.uid)
        return render_template("account.html", qr=qr, secret=secret, profile=profile,
                               weekly=fmt_duration(profile["weekly_minutes"] * 60),
                               offset=fmt_duration(profile["balance_offset"] * 60))

    # ---------------------------------------------------------------- Benutzer

    @app.route("/users", methods=["GET", "POST"])
    @admin_required
    def users():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            role = request.form.get("role", "user")
            if not 1 <= len(username) <= 80 or len(password) < 12 or role not in ("admin", "user"):
                flash("Bitte gültigen Namen und ein Passwort mit mindestens 12 Zeichen eingeben.", "error")
            else:
                try:
                    with store.connect() as connection:
                        connection.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (username, hasher.hash(password), role))
                    flash("Benutzer angelegt.", "success")
                except sqlite3.IntegrityError:
                    flash("Dieser Benutzername ist bereits vergeben.", "error")
            return redirect(url_for("users"))
        with store.connect() as connection:
            entries = [dict(row) for row in connection.execute("SELECT id, username, role, active, totp_secret IS NOT NULL AS totp_enabled FROM users ORDER BY username")]
        return render_template("users.html", users=entries)

    @app.post("/users/<int:user_id>/<action>")
    @admin_required
    def manage_user(user_id, action):
        if action not in ("toggle", "reset-2fa", "password", "role", "delete"):
            abort(404)
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user:
                abort(404)
            admins = connection.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1").fetchone()[0]
            if user["role"] == "admin" and user["active"] and admins == 1 and action in ("toggle", "role", "delete"):
                flash("Der letzte aktive Administrator kann nicht entfernt werden.", "error")
            elif action == "toggle":
                connection.execute("UPDATE users SET active = 1 - active, epoch = epoch + 1 WHERE id = ?", (user_id,))
            elif action == "role":
                connection.execute("UPDATE users SET role = CASE role WHEN 'admin' THEN 'user' ELSE 'admin' END, epoch = epoch + 1 WHERE id = ?", (user_id,))
            elif action == "reset-2fa":
                connection.execute("UPDATE users SET totp_secret = NULL, totp_pending = NULL, last_totp_step = -1, epoch = epoch + 1 WHERE id = ?", (user_id,))
                connection.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
            elif action == "delete":
                connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            else:
                password = request.form.get("password", "")
                if len(password) < 12:
                    flash("Das Passwort muss mindestens 12 Zeichen haben.", "error")
                else:
                    connection.execute("UPDATE users SET password_hash = ?, epoch = epoch + 1 WHERE id = ?", (hasher.hash(password), user_id))
        return redirect(url_for("users"))

    if app.config["ENABLE_MONITOR"]:
        monitor.start()
    return app
