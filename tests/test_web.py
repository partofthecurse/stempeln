import re
import tempfile
import time
import unittest

import pyotp

from stempel.security import hasher
from stempel.web import create_app, parse_hours


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "ENABLE_MONITOR": False,
                               "SESSION_COOKIE_SECURE": False, "WTF_CSRF_ENABLED": False})
        self.store = self.app.extensions["stempel_store"]
        with self.store.connect() as connection:
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')", (hasher.hash("admin-password-123"),))
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('user', ?, 'user')", (hasher.hash("user-password-123"),))
        self.client = self.app.test_client()

    def login(self, username="admin", password="admin-password-123"):
        return self.client.post("/login", data={"username": username, "password": password})

    def test_pages_render_and_require_login(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.login()
        for path in ("/", "/zeiten", "/abwesenheit", "/automatik", "/account", "/users", "/zeiten.csv", "/automatik/agent/windows"):
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_user_cannot_manage_accounts(self):
        self.login("user", "user-password-123")
        self.assertEqual(self.client.get("/users").status_code, 403)

    def test_stamp_in_and_out(self):
        self.login()
        self.client.post("/stamp", data={"action": "in", "note": "Projekt X"})
        self.assertIn(b"Ausstempeln", self.client.get("/").data)
        entry = self.store.open_entry(1)
        self.assertEqual((entry["source"], entry["note"]), ("web", "Projekt X"))
        with self.store.connect() as connection:
            connection.execute("UPDATE entries SET start_at = start_at - 3600")
        self.client.post("/stamp", data={"action": "out"})
        self.assertIsNone(self.store.open_entry(1))

    def test_manual_entry_overlap_and_isolation(self):
        self.login()
        form = {"day": "2026-11-16", "start": "08:00", "end": "16:30", "note": ""}
        self.client.post("/entries", data=form)
        response = self.client.post("/entries", data={**form, "start": "12:00", "end": "13:00"}, follow_redirects=True)
        self.assertIn("überschneidet".encode(), response.data)
        self.assertIn(b"08:00", self.client.get("/zeiten?monat=2026-11").data)
        csv = self.client.get("/zeiten.csv?monat=2026-11").data.decode("utf-8-sig")
        self.assertIn("16.11.2026;Mo;08:00–16:30;8:30;0:30;8:00", csv)
        self.client.post("/logout")
        self.login("user", "user-password-123")
        self.assertEqual(self.client.post("/entries/1/delete").status_code, 404)
        self.assertNotIn(b"08:00", self.client.get("/zeiten?monat=2026-11").data)

    def test_absence_skips_weekend(self):
        self.login()
        self.client.post("/abwesenheit", data={"from": "2026-11-13", "to": "2026-11-17", "kind": "vacation"})
        days = self.store.absences_between(1, "2026-11-01", "2026-11-30")
        self.assertEqual(sorted(days), ["2026-11-13", "2026-11-16", "2026-11-17"])

    def test_api_token_flow(self):
        self.login()
        self.client.post("/automatik", data={"action": "token", "name": "HA"})
        token = re.search(rb"stp_[A-Za-z0-9_\-]+", self.client.get("/automatik").data).group().decode()
        self.client.post("/logout")
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.post("/api/v1/clock", json={"action": "in"}).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/clock", json={"action": "in"}, headers={"Authorization": "Bearer nope"}).status_code, 401)
        response = self.client.post("/api/v1/clock", json={"action": "heartbeat"}, headers=headers)
        self.assertEqual((response.json["result"], response.json["state"], response.json["source"]), ("started", "in", "heartbeat"))
        self.assertEqual(self.client.post("/api/v1/clock", json={"action": "out"}, headers=headers).json["state"], "out")
        self.assertEqual(self.client.get("/api/v1/status", headers=headers).json["state"], "out")
        text = self.client.post("/api/v1/clock?format=text", json={"action": "toggle"}, headers=headers)
        self.assertEqual(text.mimetype, "text/plain")
        self.assertTrue(text.get_data(as_text=True).startswith("✅ Eingestempelt um "))
        with self.store.connect() as connection:
            connection.execute("UPDATE entries SET start_at = start_at - 7200 WHERE end_at IS NULL")
        text = self.client.post("/api/v1/clock?format=text&action=toggle", headers=headers).get_data(as_text=True)
        self.assertRegex(text, r"^👋 Ausgestempelt um \d\d:\d\d · heute 2:0\d h")
        self.assertEqual(self.client.get("/api/v1/status?format=text").get_data(as_text=True), "Ungültiges oder fehlendes Token")

    def test_work_model_and_balance(self):
        self.login()
        self.client.post("/account", data={"action": "model", "weekly_hours": "38,5", "workdays": ["0", "1", "2", "3", "4"],
                                           "region": "BY", "vacation_days": "28", "break_rule": "1",
                                           "balance_start": "2026-11-16", "balance_offset": "1:30"})
        profile = self.store.profile(1)
        self.assertEqual((profile["weekly_minutes"], profile["region"], profile["balance_offset"]), (2310, "BY", 90))

    def test_two_factor_login(self):
        self.login()
        self.client.post("/account", data={"action": "start"})
        secret = re.search(rb'class="secret-code">([A-Z2-7]+)<', self.client.get("/account").data).group(1).decode()
        self.client.post("/account", data={"action": "confirm", "code": pyotp.TOTP(secret).now()})
        self.client.post("/logout")
        self.assertIn("/login/2fa", self.login().headers["Location"])
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_parse_hours(self):
        self.assertEqual(parse_hours("40"), 2400)
        self.assertEqual(parse_hours("38,5"), 2310)
        self.assertEqual(parse_hours("38:30"), 2310)
        self.assertEqual(parse_hours("-3"), -180)
        self.assertIsNone(parse_hours("abc"))


if __name__ == "__main__":
    unittest.main()
