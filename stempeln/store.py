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
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    number TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    contact TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'DE',
    email TEXT NOT NULL DEFAULT '',
    vat_id TEXT NOT NULL DEFAULT '',
    leitweg_id TEXT NOT NULL DEFAULT '',
    tax_mode TEXT NOT NULL DEFAULT 'domestic' CHECK (tax_mode IN ('domestic', 'reverse_charge', 'non_eu')),
    language TEXT NOT NULL DEFAULT 'de',
    payment_days INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, name COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS invoice_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL DEFAULT 0,
    company TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT 'DE',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    tax_number TEXT NOT NULL DEFAULT '',
    vat_id TEXT NOT NULL DEFAULT '',
    vat_mode TEXT NOT NULL DEFAULT 'standard' CHECK (vat_mode IN ('standard', 'small_business')),
    default_vat_rate TEXT NOT NULL DEFAULT '19',
    bank_name TEXT NOT NULL DEFAULT '',
    iban TEXT NOT NULL DEFAULT '',
    bic TEXT NOT NULL DEFAULT '',
    account_holder TEXT NOT NULL DEFAULT '',
    number_format TEXT NOT NULL DEFAULT 'RE-{JJJJ}-{NNN}',
    next_number INTEGER NOT NULL DEFAULT 1,
    number_year INTEGER,
    reset_yearly INTEGER NOT NULL DEFAULT 1,
    payment_days INTEGER NOT NULL DEFAULT 14,
    position_mode TEXT NOT NULL DEFAULT 'project',
    rounding_minutes INTEGER NOT NULL DEFAULT 0,
    timesheet INTEGER NOT NULL DEFAULT 1,
    intro TEXT,
    footer TEXT,
    closing TEXT NOT NULL DEFAULT 'Mit freundlichen Grüßen'
);
CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    customer_id INTEGER REFERENCES customers(id),
    number TEXT,
    type_code TEXT NOT NULL DEFAULT '380' CHECK (type_code IN ('380', '381')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'paid', 'cancelled')),
    corrects_id INTEGER REFERENCES invoices(id),
    issue_date TEXT NOT NULL,
    service_from TEXT,
    service_to TEXT,
    due_date TEXT,
    buyer_reference TEXT NOT NULL DEFAULT '',
    intro TEXT NOT NULL DEFAULT '',
    footer TEXT NOT NULL DEFAULT '',
    closing TEXT NOT NULL DEFAULT '',
    timesheet INTEGER NOT NULL DEFAULT 1,
    language TEXT NOT NULL DEFAULT 'de',
    tax_mode TEXT,
    seller_json TEXT,
    buyer_json TEXT,
    net_cents INTEGER NOT NULL DEFAULT 0,
    tax_cents INTEGER NOT NULL DEFAULT 0,
    gross_cents INTEGER NOT NULL DEFAULT 0,
    pdf_file TEXT,
    pdf_sha256 TEXT,
    created_at INTEGER NOT NULL,
    finalized_at INTEGER,
    paid_on TEXT,
    UNIQUE (user_id, number)
);
CREATE INDEX IF NOT EXISTS invoices_user ON invoices(user_id, issue_date);
CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    description TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL DEFAULT 'HUR',
    price_cents INTEGER NOT NULL DEFAULT 0,
    vat_rate TEXT NOT NULL DEFAULT '19',
    line_cents INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS invoice_items_invoice ON invoice_items(invoice_id, position);
CREATE TABLE IF NOT EXISTS project_shares (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, user_id)
);
CREATE TABLE IF NOT EXISTS customer_shares (
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (customer_id, user_id)
);
CREATE INDEX IF NOT EXISTS project_shares_user ON project_shares(user_id);
CREATE INDEX IF NOT EXISTS customer_shares_user ON customer_shares(user_id);
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
        if "language" not in profile_columns:
            connection.execute("ALTER TABLE profiles ADD COLUMN language TEXT NOT NULL DEFAULT ''")
        project_columns = columns("projects")
        if "customer_id" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN customer_id INTEGER REFERENCES customers(id) ON DELETE SET NULL")
            # Bisherige Freitext-Kunden werden zu echten Kunden
            for row in connection.execute("SELECT DISTINCT user_id, client FROM projects WHERE client != ''").fetchall():
                connection.execute("INSERT OR IGNORE INTO customers (user_id, name) VALUES (?, ?)", (row["user_id"], row["client"]))
            connection.execute("UPDATE projects SET customer_id = (SELECT id FROM customers WHERE customers.user_id = projects.user_id "
                               "AND customers.name = projects.client COLLATE NOCASE) WHERE client != ''")
        if "billing_unit" not in project_columns:
            connection.execute("ALTER TABLE projects ADD COLUMN billing_unit TEXT NOT NULL DEFAULT 'hour'")
            connection.execute("ALTER TABLE projects ADD COLUMN day_hours TEXT NOT NULL DEFAULT '8'")
            connection.execute("ALTER TABLE projects ADD COLUMN day_rounding TEXT NOT NULL DEFAULT 'exact'")
        if "language" not in columns("customers"):
            connection.execute("ALTER TABLE customers ADD COLUMN language TEXT NOT NULL DEFAULT 'de'")
        if "language" not in columns("invoices"):
            connection.execute("ALTER TABLE invoices ADD COLUMN language TEXT NOT NULL DEFAULT 'de'")
        if "invoice_id" not in columns("entries"):
            connection.execute("ALTER TABLE entries ADD COLUMN invoice_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL")
        connection.execute("CREATE INDEX IF NOT EXISTS entries_invoice ON entries(invoice_id)")

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

    ENTRY_COLUMNS = ("SELECT entries.*, projects.name AS project_name, COALESCE(customers.name, projects.client) AS project_client, "
                     "projects.color AS project_color, projects.rate_cents AS project_rate, projects.billing_unit AS project_unit, "
                     "invoices.number AS invoice_number, invoices.status AS invoice_status "
                     "FROM entries LEFT JOIN projects ON projects.id = entries.project_id "
                     "LEFT JOIN customers ON customers.id = projects.customer_id "
                     "LEFT JOIN invoices ON invoices.id = entries.invoice_id ")

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

    # Zugriff: eigene oder per Admin freigegebene Projekte/Kunden (Platzhalter: user_id, user_id)
    PROJECT_ACCESS = "(projects.user_id = ? OR projects.id IN (SELECT project_id FROM project_shares WHERE user_id = ?))"
    CUSTOMER_ACCESS = "(customers.user_id = ? OR customers.id IN (SELECT customer_id FROM customer_shares WHERE user_id = ?))"
    PROJECT_COLUMNS = ("SELECT projects.*, COALESCE(customers.name, projects.client) AS client_name, owners.username AS owner_name "
                       "FROM projects LEFT JOIN customers ON customers.id = projects.customer_id "
                       "JOIN users AS owners ON owners.id = projects.user_id ")

    def projects(self, user_id, include_archived=True):
        query = self.PROJECT_COLUMNS + "WHERE " + self.PROJECT_ACCESS + ("" if include_archived else " AND projects.archived = 0")
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY projects.archived, projects.name COLLATE NOCASE, projects.id",
                                      (user_id, user_id)).fetchall()
        return [dict(row, shared=row["user_id"] != user_id) for row in rows]

    def project(self, user_id, project_id):
        with self.connect() as connection:
            row = connection.execute(self.PROJECT_COLUMNS + "WHERE " + self.PROJECT_ACCESS + " AND projects.id = ?",
                                     (user_id, user_id, project_id)).fetchone()
        return dict(row, shared=row["user_id"] != user_id) if row else None

    # ---------------------------------------------------------------- Freigaben (Admin)

    def shares(self, user_id):
        with self.connect() as connection:
            projects = {row[0] for row in connection.execute("SELECT project_id FROM project_shares WHERE user_id = ?", (user_id,))}
            customers = {row[0] for row in connection.execute("SELECT customer_id FROM customer_shares WHERE user_id = ?", (user_id,))}
        return projects, customers

    def set_shares(self, user_id, project_ids, customer_ids):
        """Setzt die Freigaben eines Benutzers; Kunden freigegebener Projekte werden mit freigegeben."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project_ids = {row[0] for row in connection.execute(
                f"SELECT id FROM projects WHERE user_id != ? AND id IN ({','.join('?' * len(project_ids)) or 'NULL'})",
                (user_id, *project_ids))}
            customer_ids = set(customer_ids) | {row[0] for row in connection.execute(
                f"SELECT customer_id FROM projects WHERE customer_id IS NOT NULL AND id IN ({','.join('?' * len(project_ids)) or 'NULL'})",
                tuple(project_ids))}
            customer_ids = {row[0] for row in connection.execute(
                f"SELECT id FROM customers WHERE user_id != ? AND id IN ({','.join('?' * len(customer_ids)) or 'NULL'})",
                (user_id, *customer_ids))}
            connection.execute("DELETE FROM project_shares WHERE user_id = ?", (user_id,))
            connection.execute("DELETE FROM customer_shares WHERE user_id = ?", (user_id,))
            connection.executemany("INSERT INTO project_shares (project_id, user_id) VALUES (?, ?)", [(pid, user_id) for pid in project_ids])
            connection.executemany("INSERT INTO customer_shares (customer_id, user_id) VALUES (?, ?)", [(cid, user_id) for cid in customer_ids])
        return project_ids, customer_ids

    # ---------------------------------------------------------------- Kunden & Rechnungen

    CUSTOMER_COLUMNS = ("SELECT customers.*, owners.username AS owner_name FROM customers "
                        "JOIN users AS owners ON owners.id = customers.user_id ")

    def customers(self, user_id, include_archived=True):
        query = self.CUSTOMER_COLUMNS + "WHERE " + self.CUSTOMER_ACCESS + ("" if include_archived else " AND customers.archived = 0")
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY customers.archived, customers.name COLLATE NOCASE, customers.id",
                                      (user_id, user_id)).fetchall()
        return [dict(row, shared=row["user_id"] != user_id) for row in rows]

    def customer(self, user_id, customer_id):
        with self.connect() as connection:
            row = connection.execute(self.CUSTOMER_COLUMNS + "WHERE " + self.CUSTOMER_ACCESS + " AND customers.id = ?",
                                     (user_id, user_id, customer_id)).fetchone()
        return dict(row, shared=row["user_id"] != user_id) if row else None

    def customer_by_name(self, user_id, name, create=False):
        name = " ".join((name or "").split())[:80]
        if not name:
            return None
        with self.connect() as connection:
            row = connection.execute("SELECT id FROM customers WHERE " + self.CUSTOMER_ACCESS + " AND name = ? COLLATE NOCASE "
                                     "ORDER BY user_id != ? LIMIT 1", (user_id, user_id, name, user_id)).fetchone()
            if row:
                return row["id"]
            if create:
                return connection.execute("INSERT INTO customers (user_id, name) VALUES (?, ?)", (user_id, name)).lastrowid
        return None

    def invoice_settings(self, user_id):
        with self.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO invoice_settings (user_id) VALUES (?)", (user_id,))
            return dict(connection.execute("SELECT * FROM invoice_settings WHERE user_id = ?", (user_id,)).fetchone())

    def update_invoice_settings(self, user_id, **values):
        self.invoice_settings(user_id)
        columns = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as connection:
            connection.execute(f"UPDATE invoice_settings SET {columns} WHERE user_id = ?", (*values.values(), user_id))

    def invoice(self, user_id, invoice_id):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM invoices WHERE user_id = ? AND id = ?", (user_id, invoice_id)).fetchone()
        return dict(row) if row else None

    def invoice_items(self, invoice_id):
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY position, id", (invoice_id,)).fetchall()
        return [dict(row) for row in rows]

    def invoice_entries(self, invoice_id):
        with self.connect() as connection:
            rows = connection.execute(self.ENTRY_COLUMNS + "WHERE entries.invoice_id = ? ORDER BY start_at", (invoice_id,)).fetchall()
        return [dict(row) for row in rows]

    def unbilled_entries(self, user_id, customer_id, start, end, connection=None):
        """Abgeschlossene, noch nicht abgerechnete Einträge der Projekte eines Kunden im Zeitraum.
        Mit connection läuft die Abfrage in der laufenden Transaktion des Aufrufers."""
        if connection is None:
            with self.connect() as own:
                return self.unbilled_entries(user_id, customer_id, start, end, own)
        rows = connection.execute(
            self.ENTRY_COLUMNS + "WHERE entries.user_id = ? AND projects.customer_id = ? AND entries.end_at IS NOT NULL "
            "AND entries.invoice_id IS NULL AND entries.start_at >= ? AND entries.start_at < ? ORDER BY entries.start_at",
            (user_id, customer_id, start, end)).fetchall()
        return [dict(row) for row in rows]

    def find_project(self, user_id, reference):
        """Projekt per ID oder Name (ohne Groß-/Kleinschreibung)."""
        reference = str(reference).strip()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE " + self.PROJECT_ACCESS + " AND (CAST(id AS TEXT) = ? OR name = ? COLLATE NOCASE) "
                "ORDER BY archived, user_id != ? LIMIT 1",
                (user_id, user_id, reference, reference, user_id),
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
