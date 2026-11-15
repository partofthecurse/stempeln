import re
import tempfile
import unittest

from stempeln.security import hasher
from stempeln.web import create_app


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "ENABLE_MONITOR": False,
                               "SESSION_COOKIE_SECURE": False, "WTF_CSRF_ENABLED": False})
        self.store = self.app.extensions["stempeln_store"]
        with self.store.connect() as connection:
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('admin', ?, 'admin')", (hasher.hash("admin-password-123"),))
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('daniel', ?, 'user')", (hasher.hash("daniel-password-12"),))
        self.admin = self.app.test_client()
        self.admin.post("/login", data={"username": "admin", "password": "admin-password-123"})
        self.admin.post("/projekte", data={"name": "FireBug", "client": "Kantensprung", "rate": "112,50", "color": "blue"})
        self.admin.post("/projekte", data={"name": "Intern", "color": "grey"})
        self.ids = {p["name"]: p["id"] for p in self.store.projects(1)}
        self.daniel = self.app.test_client()
        self.daniel.post("/login", data={"username": "daniel", "password": "daniel-password-12"})

    def share(self, *names):
        return self.admin.post("/users/2/freigaben", data={"project_id": [str(self.ids[n]) for n in names]}, follow_redirects=True)

    def test_not_visible_until_shared(self):
        self.assertEqual(self.store.projects(2), [])
        self.assertEqual(self.daniel.post("/stamp", data={"action": "in", "project_id": self.ids["FireBug"]}).status_code, 400)

    def test_share_project_includes_customer_and_allows_use(self):
        page = self.share("FireBug").data.decode()
        self.assertIn("1 Projekt(e), 1 Kunde(n)", page)
        self.assertEqual([p["name"] for p in self.store.projects(2)], ["FireBug"])
        self.assertEqual([c["name"] for c in self.store.customers(2)], ["Kantensprung"])
        self.assertIn("von admin", self.daniel.get("/projekte").data.decode())
        self.daniel.post("/stamp", data={"action": "in", "project_id": self.ids["FireBug"]})
        self.assertEqual(self.store.open_entry(2)["project_name"], "FireBug")
        self.assertIsNone(self.store.open_entry(1))
        # API per Name
        self.daniel.post("/automatik", data={"action": "token", "name": "x"})
        token = re.search(rb"stp_[A-Za-z0-9_\-]+", self.daniel.get("/automatik").data).group().decode()
        reply = self.daniel.post("/api/v1/clock", json={"action": "in", "project": "firebug"}, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(reply.status_code, 200)
        # Bearbeiten erlaubt, Löschen nicht
        self.daniel.post("/projekte", data={"project_id": self.ids["FireBug"], "name": "FireBug", "client": "Kantensprung", "rate": "120", "color": "green"})
        self.assertEqual(self.store.project(1, self.ids["FireBug"])["rate_cents"], 12000)
        response = self.daniel.post(f"/projekte/{self.ids['FireBug']}/delete", follow_redirects=True)
        self.assertIn("kann nur admin".encode(), response.data)
        self.assertIsNotNone(self.store.project(1, self.ids["FireBug"]))

    def test_unshare_and_invoicing_with_shared_customer(self):
        self.share("FireBug", "Intern")
        self.assertEqual(len(self.store.projects(2)), 2)
        self.daniel.post("/entries", data={"day": "2026-10-05", "start": "09:00", "end": "11:00", "project_id": self.ids["FireBug"]})
        self.daniel.post("/account", data={"action": "invoicing", "enabled": "1"})
        customer = self.store.customer_by_name(2, "Kantensprung")
        response = self.daniel.post("/rechnungen/neu", data={"customer_id": customer, "from": "2026-10-01", "to": "2026-10-31", "mode": "project"})
        invoice_id = int(response.headers["Location"].rsplit("/", 1)[1])
        self.assertEqual(self.store.invoice(2, invoice_id)["user_id"], 2)
        self.assertEqual(len(self.store.invoice_entries(invoice_id)), 1)
        self.assertIsNone(self.store.invoice(1, invoice_id))
        self.share()
        self.assertEqual(self.store.projects(2), [])
        self.assertEqual(self.store.customers(2), [])
        self.assertEqual(self.daniel.get("/users").status_code, 403)
        self.assertEqual(self.daniel.post("/users/2/freigaben", data={"project_id": [str(self.ids["Intern"])]}).status_code, 403)

    def test_admin_page_renders(self):
        self.share("FireBug")
        page = self.admin.get("/users?freigaben=2").data.decode()
        self.assertIn('name="project_id" value="%d" checked' % self.ids["FireBug"], page)
