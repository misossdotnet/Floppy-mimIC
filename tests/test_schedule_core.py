"""Calendrier CoFFY déterministe, sans services externes."""

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.schedule_core import due_window, next_due, parse_cron, validate_rule

UTC = timezone.utc


def version(mode, expression=None, zone=None, anchor=None):
    return SimpleNamespace(schedule_mode=mode, schedule_expression=expression,
                           timezone_name=zone, created_at=anchor or datetime(2026, 1, 1, tzinfo=UTC))


class ScheduleCoreTests(unittest.TestCase):
    def test_once_et_manuel(self):
        once = version("once", "2026-10-11T12:00:00+02:00")
        due = datetime(2026, 10, 11, 10, tzinfo=UTC)
        self.assertEqual(next_due(once, due - timedelta(seconds=1)), due)
        self.assertIsNone(next_due(once, due))
        self.assertIsNone(next_due(version("manual"), due))

    def test_intervalle_ancre_sans_glissement_et_rattrapage(self):
        item = version("interval", "3600", anchor=datetime(2026, 1, 1, 0, 5, tzinfo=UTC))
        first = datetime(2026, 1, 1, 1, 5, tzinfo=UTC)
        self.assertEqual(next_due(item, datetime(2026, 1, 1, 0, 59, tzinfo=UTC)), first)
        self.assertEqual(due_window(item, first, datetime(2026, 1, 1, 4, 44, tzinfo=UTC)),
                         (datetime(2026, 1, 1, 4, 5, tzinfo=UTC), 3, False))

    def test_cron_heure_inexistante_ignoree(self):
        item = version("cron", "30 2 * * *", "Europe/Paris")
        before = datetime(2026, 3, 28, 2, 30, tzinfo=ZoneInfo("Europe/Paris")).astimezone(UTC)
        after = next_due(item, before)
        self.assertEqual(after.astimezone(ZoneInfo("Europe/Paris")).date().isoformat(), "2026-03-30")

    def test_cron_heure_ambigue_une_seule_fois(self):
        item = version("cron", "30 2 * * *", "Europe/Paris")
        before = datetime(2026, 10, 24, 2, 30, tzinfo=ZoneInfo("Europe/Paris")).astimezone(UTC)
        first = next_due(item, before)
        expected = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Paris"), fold=0).astimezone(UTC)
        self.assertEqual(first, expected)
        self.assertEqual(next_due(item, first).astimezone(ZoneInfo("Europe/Paris")).date().isoformat(), "2026-10-26")

    def test_validation_et_jours_or(self):
        self.assertEqual(len(parse_cron("0 9 1 * 1")[0]), 1)
        item = version("cron", "0 9 1 * 1", "Europe/Paris")
        due = next_due(item, datetime(2026, 10, 11, tzinfo=UTC))
        self.assertEqual(due.astimezone(ZoneInfo("Europe/Paris")).weekday(), 0)
        equivalent_wildcard = version("cron", "0 9 */1 * 1", "Europe/Paris")
        self.assertEqual(next_due(equivalent_wildcard, datetime(2026, 10, 11, tzinfo=UTC)), due)
        for mode, expression, zone in (("interval", "0", None), ("cron", "61 * * * *", "UTC"),
                                        ("cron", "0 9 * * *", "Fuseau/Inconnu"),
                                        ("once", "2026-10-11T10:00:00", None),
                                        ("once", None, None)):
            with self.subTest(mode=mode, expression=expression), self.assertRaises(ValueError):
                validate_rule(mode, expression, zone)

    def test_rattrapage_cron_borne_et_une_seule_occurrence(self):
        item = version("cron", "0 9 * * *", "UTC")
        first = datetime(2026, 10, 1, 9, tzinfo=UTC)
        latest, missed, truncated = due_window(item, first, datetime(2026, 10, 11, 10, tzinfo=UTC), cap=2)
        self.assertEqual(latest, datetime(2026, 10, 11, 9, tzinfo=UTC))
        self.assertEqual(missed, 2)
        self.assertTrue(truncated)


if __name__ == "__main__":
    unittest.main()
