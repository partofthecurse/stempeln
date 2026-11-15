import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from stempeln import i18n
from stempeln.security import hasher
from stempeln.web import create_app

ROOT = Path(__file__).resolve().parent.parent


class I18nTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "ENABLE_MONITOR": False,
                               "SESSION_COOKIE_SECURE": False, "WTF_CSRF_ENABLED": False})
        self.store = self.app.extensions["stempeln_store"]
        with self.store.connect() as connection:
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')", (hasher.hash("admin-password-123"),))
        self.client = self.app.test_client()

    def test_formats(self):
        self.assertEqual(i18n.fmt_money(123456, "de"), "1.234,56 €")
        self.assertEqual(i18n.fmt_money(-123456, "en"), "-€1,234.56")
        self.assertEqual(i18n.fmt_date("2026-11-05", "en"), "5 Nov 2026")
        self.assertEqual(i18n.fmt_date("2026-11-05", "de"), "05.11.2026")
        self.assertEqual(i18n.negotiate("fr-FR,en-US;q=0.8,de;q=0.5"), "en")
        self.assertEqual(i18n.gettext("Ohne Projekt", "en"), "No project")
        self.assertEqual(i18n.gettext("Unbekannter Text", "en"), "Unbekannter Text")

    def test_language_from_browser_cookie_and_profile(self):
        self.assertIn(b"Sign in", self.client.get("/login", headers={"Accept-Language": "en-GB,en;q=0.9"}).data)
        self.assertIn(b"Anmelden", self.client.get("/login", headers={"Accept-Language": "de-DE"}).data)
        self.client.post("/sprache", data={"lang": "en", "next": "/login"})
        page = self.client.get("/login", headers={"Accept-Language": "de-DE"}).data.decode()
        self.assertIn('lang="en"', page)
        self.client.post("/login", data={"username": "admin", "password": "admin-password-123"})
        self.client.post("/sprache", data={"lang": "en"})
        self.assertEqual(self.store.profile(1)["language"], "en")
        self.client.post("/account", data={"action": "invoicing", "enabled": "1"})
        for url in ("/", "/zeiten", "/projekte", "/abwesenheit", "/automatik", "/account", "/users", "/rechnungen",
                    "/kunden", "/rechnungen/einstellungen"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            text = response.data.decode()
            for german in ("Speichern", "Abmelden", "Rechnungen</a>", "Zeiten</a>"):
                self.assertNotIn(german, text, f"{url}: {german}")
        self.assertEqual(self.client.post("/sprache", data={"lang": "fr"}).status_code, 400)

    def test_api_text_uses_owner_language(self):
        self.client.post("/login", data={"username": "admin", "password": "admin-password-123"})
        self.client.post("/sprache", data={"lang": "en"})
        self.client.post("/automatik", data={"action": "token", "name": "NFC"})
        import re
        token = re.search(rb"stp_[A-Za-z0-9_\-]+", self.client.get("/automatik").data).group().decode()
        reply = self.client.post("/api/v1/clock?format=text", json={"action": "in"}, headers={"Authorization": f"Bearer {token}"})
        self.assertIn("Clocked in at", reply.get_data(as_text=True))

    def test_catalog_complete(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "i18n_extract.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        catalog = json.loads((ROOT / "stempeln" / "translations" / "en.json").read_text(encoding="utf-8"))
        for key, value in catalog.items():
            self.assertEqual(sorted(__import__("re").findall(r"%\(\w+\)s", key)), sorted(__import__("re").findall(r"%\(\w+\)s", value)), key)
