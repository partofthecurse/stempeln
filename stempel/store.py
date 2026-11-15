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

    def open_entry(self, user_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM entries WHERE user_id = ? AND end_at IS NULL", (user_id,)).fetchone()
        return dict(row) if row else None

    def entries_between(self, user_id, start, end):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entries WHERE user_id = ? AND start_at < ? AND COALESCE(end_at, ?) > ? ORDER BY start_at",
                (user_id, end, 2 ** 40, start),
            ).fetchall()
        return [dict(row) for row in rows]

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
