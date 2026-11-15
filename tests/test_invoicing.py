import json
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

from stempeln import billing, einvoice
from stempeln.security import hasher
from stempeln.store import Store
from stempeln.web import create_app

RAM = "{urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100}"


def project(**values):
    return {"id": 1, "name": "Web", "rate_cents": 9000, "billing_unit": "hour", "day_hours": "8", "day_rounding": "exact", **values}


class BillingTests(unittest.TestCase):
    def entries(self, *spans):
        from stempeln.timecalc import parse_local
        return [{"id": i, "project_id": 1, "start_at": parse_local(day, start), "end_at": parse_local(day, end), "note": f"n{i}"}
                for i, (day, start, end) in enumerate(spans)]

    def test_hour_positions_and_rounding(self):
        entries = self.entries(("2026-11-02", "09:00", "11:10"), ("2026-11-03", "09:00", "10:00"))
        [item] = billing.positions_from_entries(entries, {1: project()}, "project", 0, "19")
        self.assertEqual((item["quantity"], item["unit"], item["line_cents"]), (Decimal("3.17"), "HUR", 28530))
        [item] = billing.positions_from_entries(entries, {1: project()}, "project", 15, "19")
        self.assertEqual(item["quantity"], Decimal("3.25"))
        self.assertEqual(len(billing.positions_from_entries(entries, {1: project()}, "day", 0, "19")), 2)
        self.assertEqual(len(billing.positions_from_entries(entries, {1: project()}, "entry", 0, "19")), 2)

    def test_day_rates(self):
        entries = self.entries(("2026-11-02", "09:00", "15:00"), ("2026-11-03", "09:00", "10:00"), ("2026-11-03", "11:00", "12:00"))
        exact = billing.positions_from_entries(entries, {1: project(billing_unit="day", rate_cents=72000)}, "project", 0, "19")[0]
        self.assertEqual((exact["quantity"], exact["unit"], exact["line_cents"]), (Decimal("1.00"), "DAY", 72000))
        half = billing.positions_from_entries(entries, {1: project(billing_unit="day", day_rounding="half")}, "project", 0, "19")[0]
        self.assertEqual(half["quantity"], Decimal("1.5"))
        full = billing.positions_from_entries(entries, {1: project(billing_unit="day", day_rounding="full")}, "project", 0, "19")[0]
        self.assertEqual(full["quantity"], Decimal("2"))
        self.assertEqual(billing.project_amount(4 * 3600, project(billing_unit="day", rate_cents=80000)), 40000)

    def test_totals_numbers_iban(self):
        items = [{"line_cents": 10000, "vat_rate": Decimal(19)}, {"line_cents": 2500, "vat_rate": Decimal(7)}, {"line_cents": 1, "vat_rate": Decimal(19)}]
        summary = billing.totals(items, "standard")
        self.assertEqual((summary["net"], summary["tax"], summary["gross"]), (12501, 1900 + 175, 12501 + 2075))
        self.assertEqual(billing.totals(items, "small_business")["tax"], 0)
        self.assertEqual(billing.totals(items, "reverse_charge")["groups"][0]["category"], "AE")
        self.assertEqual(billing.format_number("RE-{JJJJ}-{NNN}", 7, date(2026, 1, 5)), "RE-2026-007")
        self.assertEqual(billing.format_number("{JJ}{MM}{NNNN}", 12, date(2026, 3, 1)), "26030012")
        from stempeln.invoicing import valid_iban
        self.assertTrue(valid_iban("DE02120300000000202051"))
        self.assertFalse(valid_iban("DE02120300000000202052"))


class GiroCodeTests(unittest.TestCase):
    def test_epc_payload(self):
        seller = {"company": "across design", "account_holder": "", "iban": "DE57 1001 1001 2627 6699 09", "bic": "NTSBDEB1XXX"}
        payload = billing.epc_payload(seller, 321300, "R-2623-4")
        self.assertEqual(payload.split("\n"), ["BCD", "002", "1", "SCT", "NTSBDEB1XXX", "across design", "DE57100110012627669909",
                                               "EUR3213.00", "", "", "R-2623-4"])
        self.assertIsNone(billing.epc_payload({"company": "x", "iban": ""}, 100, "x"))
        self.assertIsNone(billing.epc_payload(seller, 0, "x"))

    def test_layout_values_are_validated(self):
        layout = billing.layout_values('{"font": "comic", "accent": "red", "logo_height": 99, "girocode": "yes", "fold_marks": true}')
        self.assertEqual((layout["font"], layout["accent"], layout["logo_height"], layout["girocode"], layout["fold_marks"]),
                         ("dmsans", "#2f55c9", 40, True, True))


class InvoiceFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "ENABLE_MONITOR": False,
                               "SESSION_COOKIE_SECURE": False, "WTF_CSRF_ENABLED": False})
        self.store = self.app.extensions["stempeln_store"]
        with self.store.connect() as connection:
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('frei', ?, 'admin')", (hasher.hash("frei-password-123"),))
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('other', ?, 'user')", (hasher.hash("other-password-123"),))
        self.client = self.app.test_client()
        self.client.post("/login", data={"username": "frei", "password": "frei-password-123"})
        self.client.post("/projekte", data={"name": "Relaunch", "client": "ACME GmbH", "rate": "90", "color": "green", "billing_unit": "hour"})
        self.client.post("/projekte", data={"name": "Workshop", "client": "ACME GmbH", "rate": "800", "color": "blue",
                                            "billing_unit": "day", "day_hours": "8", "day_rounding": "full"})
        self.customer = self.store.customer_by_name(1, "ACME GmbH")
        ids = {p["name"]: p["id"] for p in self.store.projects(1)}
        for day, start, end, name in (("2026-10-05", "09:00", "12:30", "Relaunch"), ("2026-10-06", "10:00", "11:00", "Relaunch"),
                                      ("2026-10-07", "09:00", "13:00", "Workshop"), ("2026-11-02", "09:00", "10:00", "Relaunch")):
            self.client.post("/entries", data={"day": day, "start": start, "end": end, "project_id": ids[name], "note": "Arbeit"})

    def enable(self):
        self.client.post("/account", data={"action": "invoicing", "enabled": "1"})
        self.client.post("/rechnungen/einstellungen", data={
            "company": "Frei Webdesign", "contact_name": "Fr Ei", "street": "Weg 1", "zip": "10115", "city": "Berlin", "country": "DE",
            "email": "frei@example.org", "phone": "030 1", "tax_number": "27/123/45678", "vat_mode": "standard", "default_vat_rate": "19",
            "iban": "DE02 1203 0000 0000 2020 51", "number_format": "RE-{JJJJ}-{NNN}", "next_number": "1", "reset_yearly": "1",
            "position_mode": "project", "rounding_minutes": "0", "timesheet": "1", "payment_days": "14",
            "intro": "Hallo", "footer": "Zahlbar bis {faellig}, Nr. {nummer}.", "closing": "Gruß"})
        self.client.post("/kunden", data={"customer_id": self.customer, "name": "ACME GmbH", "street": "Allee 2", "zip": "80331",
                                          "city": "München", "country": "DE", "tax_mode": "domestic", "email": "ap@acme.example"})

    def draft(self):
        response = self.client.post("/rechnungen/neu", data={"customer_id": self.customer, "from": "2026-10-01", "to": "2026-10-31", "mode": "project"})
        return int(response.headers["Location"].rsplit("/", 1)[1])

    def test_disabled_by_default_and_migration_creates_customer(self):
        self.assertEqual(self.client.get("/rechnungen").status_code, 302)
        self.assertNotIn(b"Rechnungen</a>", self.client.get("/").data)
        self.assertIsNotNone(self.customer)

    def test_full_invoice_lifecycle(self):
        self.enable()
        self.assertIn(b"Rechnungen</a>", self.client.get("/").data)
        self.assertIn("NOCH NICHT ABGERECHNET".encode(), self.client.get("/rechnungen").data)
        invoice_id = self.draft()
        items = self.store.invoice_items(invoice_id)
        self.assertEqual([(i["description"], i["quantity"], i["unit"], i["line_cents"]) for i in items],
                         [("Relaunch", "4.50", "HUR", 40500), ("Workshop", "1.00", "DAY", 80000)])
        self.assertEqual(len(self.store.invoice_entries(invoice_id)), 3)
        preview = self.client.get(f"/rechnungen/{invoice_id}/pdf")
        self.assertTrue(preview.data.startswith(b"%PDF"))
        self.assertEqual(self.client.get(f"/rechnungen/{invoice_id}?position={items[0]['id']}").status_code, 200)
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "add", "description": "Hosting", "quantity": "1", "unit": "LS",
                                                           "price": "25,00", "vat_rate": "7"})
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "finalize"})
        invoice = self.store.invoice(1, invoice_id)
        self.assertEqual((invoice["number"], invoice["status"]), ("RE-2026-001", "open"))
        self.assertEqual((invoice["net_cents"], invoice["tax_cents"], invoice["gross_cents"]), (123000, 22895 + 175, 123000 + 23070))
        self.assertEqual(json.loads(invoice["buyer_json"])["city"], "München")
        pdf = self.client.get(f"/rechnungen/{invoice_id}/pdf").data
        self.assertTrue(pdf.startswith(b"%PDF") and b"factur-x.xml" in pdf and b"pdfaid" in pdf)
        archived = Path(self.temp.name, "invoices", "1", "RE-2026-001.pdf")
        self.assertEqual(archived.read_bytes(), pdf)
        xml = self.client.get(f"/rechnungen/{invoice_id}/zugferd.xml").data
        root = ET.fromstring(xml)
        self.assertEqual(root.find(f".//{RAM}GrandTotalAmount").text, "1460.70")
        self.assertEqual(root.find(f".//{RAM}DueDateDateTime")[0].text, (date.fromisoformat(invoice["issue_date"]).toordinal() + 14 and
                                                                          date.fromordinal(date.fromisoformat(invoice["issue_date"]).toordinal() + 14).strftime("%Y%m%d")))
        xr = self.client.get(f"/rechnungen/{invoice_id}/xrechnung.xml").data
        self.assertIn(einvoice.GUIDELINE_XRECHNUNG.encode(), xr)
        # Abgerechnete Einträge sind gesperrt, Entwurfs-Aktionen verboten
        entry = self.store.invoice_entries(invoice_id)[0]
        response = self.client.post(f"/entries/{entry['id']}/delete", follow_redirects=True)
        self.assertIn("gesperrt".encode(), response.data)
        self.assertIsNotNone(self.store.entry(1, entry["id"]))
        self.assertEqual(self.client.post(f"/rechnungen/{invoice_id}", data={"action": "delete-item", "item_id": 1}).status_code, 409)
        # Bezahlt markieren und zurück
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "paid", "paid_on": "2026-11-20"})
        self.assertEqual(self.store.invoice(1, invoice_id)["status"], "paid")
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "paid"})
        self.assertEqual(self.store.invoice(1, invoice_id)["status"], "open")
        # Storno: eigene Nummer, Typ 381, Zeiten wieder frei
        response = self.client.post(f"/rechnungen/{invoice_id}", data={"action": "cancel"})
        storno_id = int(response.headers["Location"].rsplit("/", 1)[1])
        storno = self.store.invoice(1, storno_id)
        self.assertEqual((storno["number"], storno["type_code"], storno["gross_cents"]), ("RE-2026-002", "381", invoice["gross_cents"]))
        self.assertEqual(self.store.invoice(1, invoice_id)["status"], "cancelled")
        self.assertEqual(self.store.invoice_entries(invoice_id), [])
        storno_xml = ET.fromstring(self.client.get(f"/rechnungen/{storno_id}/zugferd.xml").data)
        self.assertEqual(storno_xml.find(f".//{RAM}InvoiceReferencedDocument/{RAM}IssuerAssignedID").text, "RE-2026-001")
        # Alle Seiten rendern
        for url in ("/rechnungen", "/kunden", f"/kunden?edit={self.customer}", "/rechnungen/einstellungen", f"/rechnungen/{invoice_id}",
                    f"/rechnungen/{storno_id}", "/projekte", f"/projekte?edit={entry['project_id']}", f"/zeiten?monat=2026-10&edit={entry['id']}", "/account"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
        self.assertIn("1.460,70 €".encode(), self.client.get(f"/rechnungen/{invoice_id}").data)
        # Neu abrechnen möglich
        again = self.draft()
        self.assertEqual(len(self.store.invoice_entries(again)), 3)

    def test_draft_delete_releases_entries_and_recalc(self):
        self.enable()
        invoice_id = self.draft()
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "recalc", "mode": "entry"})
        self.assertEqual(len(self.store.invoice_items(invoice_id)), 3)
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "delete"})
        self.assertIsNone(self.store.invoice(1, invoice_id))
        with self.store.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM entries WHERE invoice_id IS NOT NULL").fetchone()[0], 0)

    def test_finalize_requires_seller_data_and_isolation(self):
        self.client.post("/account", data={"action": "invoicing", "enabled": "1"})
        invoice_id = self.draft()
        response = self.client.post(f"/rechnungen/{invoice_id}", data={"action": "finalize"}, follow_redirects=True)
        self.assertIn("fehlt noch".encode(), response.data)
        self.assertEqual(self.store.invoice(1, invoice_id)["status"], "draft")
        self.client.post("/logout")
        self.client.post("/login", data={"username": "other", "password": "other-password-123"})
        self.client.post("/account", data={"action": "invoicing", "enabled": "1"})
        self.assertEqual(self.client.get(f"/rechnungen/{invoice_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/rechnungen/{invoice_id}/pdf").status_code, 404)

    def test_english_invoice_pdf(self):
        self.enable()
        with self.store.connect() as connection:
            connection.execute("UPDATE customers SET language = 'en'")
            connection.execute("UPDATE invoice_settings SET intro = NULL, footer = NULL")
        invoice_id = self.draft()
        self.assertIn("by ", self.store.invoice(1, invoice_id)["footer"])
        self.assertTrue(self.store.invoice(1, invoice_id)["intro"].startswith("Thank you"))
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "finalize"})
        pdf = self.client.get(f"/rechnungen/{invoice_id}/pdf").data
        self.assertTrue(pdf.startswith(b"%PDF") and b"en-GB" in pdf)
        xml = self.client.get(f"/rechnungen/{invoice_id}/zugferd.xml").data.decode()
        self.assertIn("Payable by", xml)

    def test_layout_editor_logo_and_preview(self):
        import io
        from PIL import Image
        self.enable()
        self.assertEqual(self.client.get("/rechnungen/layout").status_code, 200)
        form = {"font": "anuphan", "accent": "#d0342c", "logo_position": "left", "logo_height": "20", "fold_marks": "1",
                "girocode": "1", "footer": "1", "footer_owner": "1", "salutation": "1", "signature": "1", "page_numbers": "1"}
        preview = self.client.post("/rechnungen/layout/vorschau", data=form)
        self.assertEqual(preview.mimetype, "application/pdf")
        self.assertEqual(preview.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertNotIn("anuphan", self.store.invoice_settings(1)["layout"].lower())
        self.client.post("/rechnungen/layout", data=form)
        layout = billing.layout_values(self.store.invoice_settings(1)["layout"])
        self.assertEqual((layout["font"], layout["accent"], layout["header_text"], layout["fold_marks"]), ("anuphan", "#d0342c", False, True))
        image = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
        image.paste((30, 30, 30, 255), (50, 50, 350, 150))
        buffer = io.BytesIO(); image.save(buffer, format="PNG"); buffer.seek(0)
        self.client.post("/rechnungen/layout/logo", data={"logo": (buffer, "logo.png")}, content_type="multipart/form-data")
        with Image.open(Path(self.temp.name, "branding", "1", "logo.png")) as stored:
            self.assertEqual((stored.mode, stored.size), ("RGB", (300, 100)))
        bad = self.client.post("/rechnungen/layout/logo", data={"logo": (io.BytesIO(b"<svg/>"), "x.svg")},
                               content_type="multipart/form-data", follow_redirects=True)
        self.assertIn("PNG, JPG".encode(), bad.data)
        with self.store.connect() as connection:
            connection.execute("UPDATE customers SET salutation = 'Sehr geehrter Herr Zwisler,'")
        invoice_id = self.draft()
        self.assertEqual(self.store.invoice(1, invoice_id)["salutation"], "Sehr geehrter Herr Zwisler,")
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "finalize"})
        pdf = self.client.get(f"/rechnungen/{invoice_id}/pdf").data
        self.assertTrue(pdf.startswith(b"%PDF") and b"factur-x.xml" in pdf)
        self.client.post("/rechnungen/layout/logo", data={"action": "delete"})
        self.assertFalse(Path(self.temp.name, "branding", "1", "logo.png").exists())

    def test_small_business_and_reverse_charge(self):
        self.enable()
        settings = self.store.invoice_settings(1)
        self.store.update_invoice_settings(1, vat_mode="small_business")
        invoice_id = self.draft()
        self.client.post(f"/rechnungen/{invoice_id}", data={"action": "finalize"})
        invoice = self.store.invoice(1, invoice_id)
        self.assertEqual((invoice["tax_mode"], invoice["tax_cents"]), ("small_business", 0))
        self.assertIn(b"<ram:CategoryCode>E</ram:CategoryCode>", self.client.get(f"/rechnungen/{invoice_id}/zugferd.xml").data)
        self.store.update_invoice_settings(1, vat_mode=settings["vat_mode"])
        with self.store.connect() as connection:
            connection.execute("UPDATE customers SET tax_mode = 'reverse_charge', vat_id = 'ATU12345678', country = 'AT'")
        self.client.post("/entries", data={"day": "2026-10-20", "start": "09:00", "end": "10:00",
                                           "project_id": self.store.find_project(1, "Relaunch")["id"]})
        second = self.draft()
        self.client.post(f"/rechnungen/{second}", data={"action": "finalize"})
        self.assertEqual(self.store.invoice(1, second)["tax_mode"], "reverse_charge")
        self.assertIn(b"VATEX-EU-AE", self.client.get(f"/rechnungen/{second}/zugferd.xml").data)


class MigrationTests(unittest.TestCase):
    def test_old_client_text_becomes_customer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "stempel.db")
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT, active INTEGER DEFAULT 1,
                    epoch INTEGER DEFAULT 0, totp_secret TEXT, totp_pending TEXT, last_totp_step INTEGER DEFAULT -1, created_at TEXT);
                INSERT INTO users (id, username, password_hash, role) VALUES (1, 'a', 'x', 'admin');
                CREATE TABLE projects (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, client TEXT NOT NULL DEFAULT '',
                    rate_cents INTEGER, color TEXT NOT NULL DEFAULT 'blue', archived INTEGER NOT NULL DEFAULT 0);
                INSERT INTO projects (user_id, name, client) VALUES (1, 'A', 'ACME'), (1, 'B', 'acme'), (1, 'C', '');
            """)
            connection.close()
            store = Store(path)
            customers = store.customers(1)
            self.assertEqual([c["name"] for c in customers], ["ACME"])
            self.assertEqual([p["customer_id"] for p in store.projects(1)], [customers[0]["id"], customers[0]["id"], None])
