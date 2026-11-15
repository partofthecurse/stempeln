import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    active INTEGER NOT NULL DEFAULT 1,
    epoch INTEGER NOT NULL DEFAULT 0,
    totp_secret TEXT,
    totp_pending TEXT,
    last_totp_step INTEGER NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    PRIMARY KEY (user_id, code_hash)
);
CREATE TABLE IF NOT EXISTS login_attempts (
    subject TEXT NOT NULL,
    attempted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS login_attempts_subject ON login_attempts(subject, attempted_at);
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    weekly_minutes INTEGER NOT NULL DEFAULT 2400 CHECK (weekly_minutes BETWEEN 0 AND 6000),
    workdays TEXT NOT NULL DEFAULT '01234',
    break_rule INTEGER NOT NULL DEFAULT 1,
    region TEXT NOT NULL DEFAULT '',
    vacation_days REAL NOT NULL DEFAULT 30,
    balance_start TEXT,
    balance_offset INTEGER NOT NULL DEFAULT 0,
    device_host TEXT NOT NULL DEFAULT '',
    idle_minutes INTEGER NOT NULL DEFAULT 10 CHECK (idle_minutes BETWEEN 2 AND 240),
    last_signal INTEGER,
    auto_suppressed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    start_at INTEGER NOT NULL,
    end_at INTEGER,
    last_seen INTEGER,
    source TEXT NOT NULL DEFAULT 'web',
    note TEXT NOT NULL DEFAULT '',
    CHECK (end_at IS NULL OR end_at >= start_at)
);
CREATE INDEX IF NOT EXISTS entries_user_start ON entries(user_id, start_at);
CREATE UNIQUE INDEX IF NOT EXISTS entries_one_open ON entries(user_id) WHERE end_at IS NULL;
CREATE TABLE IF NOT EXISTS absences (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('vacation', 'sick', 'other')),
    half INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    rate_cents INTEGER,
    color TEXT NOT NULL DEFAULT 'blue',
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, name COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            self._migrate(connection)

    @staticmethod
    def _migrate(connection):
        def columns(table):
            return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "project_id" not in columns("entries"):
            connection.execute("ALTER TABLE entries ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL")
        connection.execute("CREATE INDEX IF NOT EXISTS entries_project ON entries(project_id)")
        profile_columns = columns("profiles")
        if "track_balance" not in profile_columns:
            connection.execute("ALTER TABLE profiles ADD COLUMN track_balance INTEGER NOT NULL DEFAULT 1")
        if "default_project_id" not in profile_columns:
            connection.execute("ALTER TABLE profiles ADD COLUMN default_project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def profile(self, user_id):
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO profiles (user_id) VALUES (?)", (user_id,))
            return dict(connection.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone())

    def update_profile(self, user_id, **values):
        self.profile(user_id)
        columns = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as connection:
            connection.execute(f"UPDATE profiles SET {columns} WHERE user_id = ?", (*values.values(), user_id))

    ENTRY_COLUMNS = ("SELECT entries.*, projects.name AS project_name, projects.client AS project_client, "
                     "projects.color AS project_color, projects.rate_cents AS project_rate "
                     "FROM entries LEFT JOIN projects ON projects.id = entries.project_id ")

    def open_entry(self, user_id):
        with self.connect() as connection:
            row = connection.execute(self.ENTRY_COLUMNS + "WHERE entries.user_id = ? AND end_at IS NULL", (user_id,)).fetchone()
        return dict(row) if row else None

    def entry(self, user_id, entry_id):
        with self.connect() as connection:
            row = connection.execute(self.ENTRY_COLUMNS + "WHERE entries.user_id = ? AND entries.id = ?", (user_id, entry_id)).fetchone()
        return dict(row) if row else None

    def entries_between(self, user_id, start, end, project_id=None):
        query = self.ENTRY_COLUMNS + "WHERE entries.user_id = ? AND start_at < ? AND COALESCE(end_at, ?) > ?"
        parameters = [user_id, end, 2 ** 40, start]
        if project_id == 0:
            query += " AND entries.project_id IS NULL"
        elif project_id:
            query += " AND entries.project_id = ?"
            parameters.append(project_id)
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY start_at", parameters).fetchall()
        return [dict(row) for row in rows]

    def projects(self, user_id, include_archived=True):
        query = "SELECT * FROM projects WHERE user_id = ?" + ("" if include_archived else " AND archived = 0")
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY archived, name COLLATE NOCASE", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def project(self, user_id, project_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM projects WHERE user_id = ? AND id = ?", (user_id, project_id)).fetchone()
        return dict(row) if row else None

    def find_project(self, user_id, reference):
        """Projekt per ID oder Name (ohne Groß-/Kleinschreibung)."""
        reference = str(reference).strip()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE user_id = ? AND (CAST(id AS TEXT) = ? OR name = ? COLLATE NOCASE) ORDER BY archived LIMIT 1",
                (user_id, reference, reference),
            ).fetchone()
        return dict(row) if row else None

    def last_project_id(self, user_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT entries.project_id FROM entries JOIN projects ON projects.id = entries.project_id "
                "WHERE entries.user_id = ? AND projects.archived = 0 ORDER BY start_at DESC LIMIT 1", (user_id,)).fetchone()
        return row[0] if row else None

    def first_entry_start(self, user_id):
        with self.connect() as connection:
            return connection.execute("SELECT MIN(start_at) FROM entries WHERE user_id = ?", (user_id,)).fetchone()[0]

    def absences_between(self, user_id, first_day, last_day):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT day, kind, half FROM absences WHERE user_id = ? AND day BETWEEN ? AND ? ORDER BY day",
                (user_id, first_day, last_day),
            ).fetchall()
        return {row["day"]: dict(row) for row in rows}

    def overlaps(self, user_id, start, end, exclude_id=None):
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM entries WHERE user_id = ? AND id IS NOT ? AND start_at < ? AND COALESCE(end_at, ?) > ? LIMIT 1",
                (user_id, exclude_id, end if end is not None else 2 ** 40, 2 ** 40, start),
            ).fetchone() is not None
