import tempfile
import unittest
from datetime import date

from stempeln.store import Store
from stempeln.timecalc import day_bounds, day_report, parse_local, required_break_net, target_seconds

H, M = 3600, 60
PROFILE = {"weekly_minutes": 2400, "workdays": "01234", "break_rule": 1, "region": "BY"}


class BreakRuleTests(unittest.TestCase):
    def test_statutory_breaks_without_gaps(self):
        self.assertEqual(required_break_net(6 * H, 0), 6 * H)
        self.assertEqual(required_break_net(6 * H + 10 * M, 0), 6 * H)
        self.assertEqual(required_break_net(8 * H, 0), 7 * H + 30 * M)
        self.assertEqual(required_break_net(9 * H + 40 * M, 0), 9 * H)
        self.assertEqual(required_break_net(10 * H, 0), 9 * H + 15 * M)

    def test_real_breaks_are_credited(self):
        self.assertEqual(required_break_net(8 * H, 30 * M), 8 * H)
        self.assertEqual(required_break_net(8 * H, 20 * M), 7 * H + 50 * M)
        self.assertEqual(required_break_net(10 * H, 30 * M), 9 * H + 45 * M)


class DayReportTests(unittest.TestCase):
    def test_targets_respect_weekends_holidays_and_half_days(self):
        self.assertEqual(target_seconds(PROFILE, date(2026, 11, 16)), 8 * H)  # Montag
        self.assertEqual(target_seconds(PROFILE, date(2026, 11, 15)), 0)  # Sonntag
        self.assertEqual(target_seconds(PROFILE, date(2026, 12, 25)), 0)  # 1. Weihnachtstag
        self.assertEqual(target_seconds(PROFILE, date(2026, 1, 6)), 0)  # Heilige Drei Könige (BY)
        self.assertEqual(target_seconds({**PROFILE, "region": "BE"}, date(2026, 1, 6)), 8 * H)
        self.assertEqual(target_seconds(PROFILE, date(2026, 11, 16), {"half": 1}), 4 * H)

    def test_entry_over_midnight_is_split(self):
        entry = {"start_at": parse_local("2026-11-16", "22:00"), "end_at": parse_local("2026-11-17", "02:00")}
        first = day_report(PROFILE, date(2026, 11, 16), [entry])
        second = day_report(PROFILE, date(2026, 11, 17), [entry])
        self.assertEqual((first["worked"], second["worked"]), (2 * H, 2 * H))

    def test_gaps_shorter_than_fifteen_minutes_do_not_count(self):
        day = "2026-11-16"
        entries = [
            {"start_at": parse_local(day, "08:00"), "end_at": parse_local(day, "12:00")},
            {"start_at": parse_local(day, "12:10"), "end_at": parse_local(day, "16:00")},
        ]
        report = day_report(PROFILE, date(2026, 11, 16), entries)
        self.assertEqual(report["breaks"], 0)
        self.assertEqual(report["net"], 7 * H + 20 * M)
        self.assertEqual(report["diff"], -40 * M)

    def test_open_entry_counts_until_now(self):
        start, _ = day_bounds(date(2026, 11, 16))
        report = day_report(PROFILE, date(2026, 11, 16), [{"start_at": start + 8 * H, "end_at": None}], now=start + 9 * H)
        self.assertTrue(report["open"])
        self.assertEqual(report["worked"], H)


class StoreTests(unittest.TestCase):
    def test_only_one_open_entry_per_user(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(f"{directory}/test.db")
            with store.connect() as connection:
                connection.execute("INSERT INTO users (username, password_hash, role) VALUES ('a', 'x', 'user')")
                connection.execute("INSERT INTO entries (user_id, start_at) VALUES (1, 100)")
            with self.assertRaises(Exception):
                with store.connect() as connection:
                    connection.execute("INSERT INTO entries (user_id, start_at) VALUES (1, 200)")


if __name__ == "__main__":
    unittest.main()
