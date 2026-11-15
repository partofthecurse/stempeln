"""Ein- und Ausstempeln: manuell, per API-Signal und per Geräteerkennung im Heimnetz."""

import logging
import random
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

AUTO_SOURCES = ("heartbeat", "device")
MIN_ENTRY_SECONDS = 60
log = logging.getLogger(__name__)


def _open(connection, user_id):
    return connection.execute("SELECT * FROM entries WHERE user_id = ? AND end_at IS NULL", (user_id,)).fetchone()


def _ensure_profile(connection, user_id):
    connection.execute("INSERT OR IGNORE INTO profiles (user_id) VALUES (?)", (user_id,))
    return connection.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone()


def _close(connection, entry, end_at):
    if end_at - entry["start_at"] < MIN_ENTRY_SECONDS:
        connection.execute("DELETE FROM entries WHERE id = ?", (entry["id"],))
        return None
    connection.execute("UPDATE entries SET end_at = ? WHERE id = ?", (end_at, entry["id"]))
    return end_at


def clock_in(store, user_id, source="web", now=None, note="", project_id=None):
    now = int(now or time.time())
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_profile(connection, user_id)
        if _open(connection, user_id):
            return False
        connection.execute(
            "INSERT INTO entries (user_id, start_at, last_seen, source, note, project_id) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, now, now, source, note[:200], project_id),
        )
        connection.execute("UPDATE profiles SET auto_suppressed = 0 WHERE user_id = ?", (user_id,))
        return True


def clock_out(store, user_id, now=None):
    """Manuelles Ausstempeln. Unterdrückt das automatische Wiedereinstempeln, bis das Gerät eine Weile weg war."""
    now = int(now or time.time())
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_profile(connection, user_id)
        entry = _open(connection, user_id)
        if not entry:
            return False
        _close(connection, entry, max(now, entry["start_at"]))
        connection.execute("UPDATE profiles SET auto_suppressed = 1 WHERE user_id = ?", (user_id,))
        return True


def switch(store, user_id, project_id, source="web", now=None, note=""):
    """Wechselt das Projekt: laufenden Eintrag beenden und sofort einen neuen starten."""
    now = int(now or time.time())
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _ensure_profile(connection, user_id)
        entry = _open(connection, user_id)
        if entry and entry["project_id"] == project_id:
            return False
        if entry:
            source = entry["source"]
            if now - entry["start_at"] < MIN_ENTRY_SECONDS:
                connection.execute("UPDATE entries SET project_id = ? WHERE id = ?", (project_id, entry["id"]))
                return True
            _close(connection, entry, now)
        connection.execute(
            "INSERT INTO entries (user_id, start_at, last_seen, source, note, project_id) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, now, now, source, note[:200], project_id),
        )
        connection.execute("UPDATE profiles SET auto_suppressed = 0 WHERE user_id = ?", (user_id,))
        return True


def toggle(store, user_id, source="web", now=None, project_id=None):
    """Ohne Projekt: ein/aus. Mit Projekt: läuft ein anderes Projekt, wird gewechselt."""
    entry = store.open_entry(user_id)
    if entry:
        if project_id is not None and entry["project_id"] != project_id:
            switch(store, user_id, project_id, source, now)
            return "switched"
        clock_out(store, user_id, now)
        return "out"
    clock_in(store, user_id, source, now, project_id=project_id)
    return "in"


def signal(store, user_id, source="heartbeat", now=None):
    """Lebenszeichen eines Geräts: startet eine Sitzung oder hält sie offen."""
    now = int(now or time.time())
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        profile = _ensure_profile(connection, user_id)
        previous = profile["last_signal"]
        connection.execute("UPDATE profiles SET last_signal = ? WHERE user_id = ?", (now, user_id))
        entry = _open(connection, user_id)
        if entry:
            if entry["source"] in AUTO_SOURCES:
                connection.execute("UPDATE entries SET last_seen = MAX(COALESCE(last_seen, 0), ?) WHERE id = ?", (now, entry["id"]))
            return "running"
        if profile["auto_suppressed"]:
            if previous is not None and now - previous <= profile["idle_minutes"] * 60:
                return "suppressed"
            connection.execute("UPDATE profiles SET auto_suppressed = 0 WHERE user_id = ?", (user_id,))
        default_project = connection.execute(
            "SELECT id FROM projects WHERE id = ? AND archived = 0 AND (user_id = ? OR id IN "
            "(SELECT project_id FROM project_shares WHERE user_id = ?))", (profile["default_project_id"], user_id, user_id)
        ).fetchone()
        connection.execute(
            "INSERT INTO entries (user_id, start_at, last_seen, source, project_id) VALUES (?, ?, ?, ?, ?)",
            (user_id, now, now, source, default_project[0] if default_project else None),
        )
        return "started"


def reap(store, now=None):
    """Beendet automatische Sitzungen, deren Gerät länger als die Leerlaufzeit stumm ist."""
    now = int(now or time.time())
    closed = 0
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT entries.*, profiles.idle_minutes FROM entries JOIN profiles USING (user_id) "
            "WHERE end_at IS NULL AND source IN (?, ?)", AUTO_SOURCES,
        ).fetchall()
        for entry in rows:
            last_seen = entry["last_seen"] or entry["start_at"]
            if now - last_seen > entry["idle_minutes"] * 60:
                _close(connection, entry, last_seen)
                closed += 1
    return closed


def parse_target(target):
    target = target.strip()
    if target.count(":") == 1:
        host, _, port = target.partition(":")
        if port.isdigit() and 0 < int(port) < 65536:
            return host, int(port)
    return target, None


def probe(target, timeout=2):
    host, port = parse_target(target)
    if not host:
        return False
    if port:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(timeout), host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


class Monitor:
    """Hintergrund-Thread: prüft Geräte im Heimnetz und schließt verwaiste Sitzungen."""

    def __init__(self, store, interval=60):
        self.store = store
        self.interval = interval
        self.status = {}
        self.lock = threading.Lock()

    def tick(self, now=None):
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT profiles.user_id, profiles.device_host FROM profiles JOIN users ON users.id = profiles.user_id "
                "WHERE users.active = 1 AND profiles.device_host != ''"
            ).fetchall()
        targets = [(row["user_id"], row["device_host"]) for row in rows]
        if targets:
            with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
                results = list(pool.map(lambda item: probe(item[1]), targets))
            for (user_id, host), online in zip(targets, results):
                with self.lock:
                    self.status[user_id] = {"host": host, "online": online, "checked": int(time.time())}
                if online:
                    signal(self.store, user_id, "device", now)
        reap(self.store, now)

    def snapshot(self, user_id):
        with self.lock:
            return dict(self.status.get(user_id, {}))

    def start(self):
        def loop():
            while True:
                try:
                    self.tick()
                except Exception:
                    log.exception("Monitor tick failed")
                time.sleep(self.interval + random.uniform(0, 5))
        threading.Thread(target=loop, daemon=True, name="stempeln-monitor").start()
