import tempfile
import unittest
from unittest.mock import patch

from stempel import clock
from stempel.store import Store


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(f"{self.temp.name}/test.db")
        with self.store.connect() as connection:
            connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('a', 'x', 'user')")
        self.user = 1
        self.store.update_profile(self.user, idle_minutes=10)

    def entries(self):
        with self.store.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM entries ORDER BY id")]

    def test_heartbeats_start_session_and_reaper_ends_it_at_last_signal(self):
        t = 1_800_000_000
        self.assertEqual(clock.signal(self.store, self.user, now=t), "started")
        for minute in range(1, 31):
            self.assertEqual(clock.signal(self.store, self.user, now=t + minute * 60), "running")
        self.assertEqual(clock.reap(self.store, now=t + 35 * 60), 0)
        self.assertEqual(clock.reap(self.store, now=t + 41 * 60), 1)
        entry = self.entries()[0]
        self.assertEqual((entry["start_at"], entry["end_at"]), (t, t + 30 * 60))

    def test_manual_clock_out_suppresses_auto_restart_until_idle_gap(self):
        t = 1_800_000_000
        clock.signal(self.store, self.user, now=t)
        clock.signal(self.store, self.user, now=t + 3600)
        self.assertTrue(clock.clock_out(self.store, self.user, now=t + 3600))
        self.assertEqual(clock.signal(self.store, self.user, now=t + 3660), "suppressed")
        self.assertIsNone(self.store.open_entry(self.user))
        self.assertEqual(clock.signal(self.store, self.user, now=t + 3660 + 11 * 60), "started")

    def test_manual_entries_are_not_closed_by_reaper_and_tiny_sessions_vanish(self):
        t = 1_800_000_000
        clock.clock_in(self.store, self.user, "web", now=t)
        self.assertEqual(clock.reap(self.store, now=t + 86400), 0)
        clock.clock_out(self.store, self.user, now=t + 30)
        self.assertEqual(self.entries(), [])

    def test_toggle(self):
        self.assertEqual(clock.toggle(self.store, self.user, "api", now=1000), "in")
        self.assertEqual(clock.toggle(self.store, self.user, "api", now=5000), "out")
        self.assertEqual(self.entries()[0]["end_at"], 5000)

    def test_switch_and_default_project_for_automatic_sessions(self):
        with self.store.connect() as connection:
            connection.execute("INSERT INTO projects (user_id, name) VALUES (1, 'A')")
            connection.execute("INSERT INTO projects (user_id, name) VALUES (1, 'B')")
        self.store.update_profile(self.user, default_project_id=1)
        t = 1_800_000_000
        clock.signal(self.store, self.user, now=t)
        self.assertEqual(self.store.open_entry(self.user)["project_id"], 1)
        self.assertTrue(clock.switch(self.store, self.user, 2, now=t + 600))
        entries = self.entries()
        self.assertEqual([(e["project_id"], e["end_at"], e["source"]) for e in entries], [(1, t + 600, "heartbeat"), (2, None, "heartbeat")])
        self.assertFalse(clock.switch(self.store, self.user, 2, now=t + 700))
        self.assertEqual(clock.toggle(self.store, self.user, now=t + 900, project_id=1), "switched")
        self.assertEqual(clock.toggle(self.store, self.user, now=t + 1200, project_id=1), "out")

    def test_probe_target_parsing(self):
        self.assertEqual(clock.parse_target("laptop.fritz.box:445"), ("laptop.fritz.box", 445))
        self.assertEqual(clock.parse_target("192.168.1.5"), ("192.168.1.5", None))
        self.assertEqual(clock.parse_target("fe80::1"), ("fe80::1", None))

    def test_monitor_turns_online_device_into_signal(self):
        self.store.update_profile(self.user, device_host="10.0.0.9")
        monitor = clock.Monitor(self.store)
        with patch("stempel.clock.probe", return_value=True):
            monitor.tick(now=1_800_000_000)
        self.assertEqual(self.store.open_entry(self.user)["source"], "device")
        self.assertTrue(monitor.snapshot(self.user)["online"])


if __name__ == "__main__":
    unittest.main()
