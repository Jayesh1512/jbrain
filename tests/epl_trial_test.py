#!/usr/bin/env python3
"""Check football source validation and the deterministic statistics used in OKF."""
import csv
import importlib.util
import io
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("epl_trial", ROOT / "scripts/epl_trial.py")
epl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(epl)


def full_season(year=2024):
    teams = [f"Club {i}" for i in range(22 if year < 1995 else 20)]
    rows = [("E0", f"01/08/{year}", a, b, 1, 0, "H") for a in teams for b in teams if a != b]
    return rows


def encoded(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Div", "Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"])
    writer.writerows(rows)
    return output.getvalue().encode()


class EPLTrialTests(unittest.TestCase):
    def test_full_modern_and_legacy_seasons_are_valid(self):
        modern = epl.parse_csv(encoded(full_season()) + b",,,,,,\r\n", 2024)
        self.assertEqual(len(modern), 380)
        self.assertEqual(modern[0]["date"], "2024-08-01")
        legacy = [tuple("01/08/93" if i == 1 else v for i, v in enumerate(r)) for r in full_season(1993)]
        self.assertEqual(len(epl.parse_csv(encoded(legacy), 1993)), 462)

    def test_incomplete_and_duplicate_seasons_are_rejected(self):
        rows = full_season()
        with self.assertRaisesRegex(ValueError, "Incomplete season"):
            epl.parse_csv(encoded(rows[:-1]), 2024)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            epl.parse_csv(encoded(rows + rows[:1]), 2024)

    def test_inconsistent_scores_and_dates_are_rejected(self):
        rows = full_season()
        bad = list(rows[0])
        bad[-1] = "A"
        with self.assertRaisesRegex(ValueError, "Inconsistent"):
            epl.parse_csv(encoded([bad] + rows[1:]), 2024)
        bad[1] = "01/08/2023"
        with self.assertRaisesRegex(ValueError, "Invalid season date"):
            epl.parse_csv(encoded([bad] + rows[1:]), 2024)

    def test_home_away_statistics_and_points(self):
        games = [{"home": "A", "away": "B", "hg": 2, "ag": 0},
                 {"home": "B", "away": "A", "hg": 3, "ag": 1},
                 {"home": "A", "away": "B", "hg": 2, "ag": 2}]
        self.assertEqual(epl.team_stats(games, "A"), {"played": 3, "won": 1, "drawn": 1, "lost": 1,
                         "goals_for": 5, "goals_against": 5, "points": 4, "goal_difference": 0})

    def test_unbeaten_streak_crosses_seasons_and_breaks_on_loss(self):
        games = [{"id": str(i), "date": f"2024-08-{i + 1:02d}", "home": "A", "away": "B", "hg": gf, "ag": ga}
                 for i, (gf, ga) in enumerate([(1, 0), (0, 0), (0, 1), (2, 0), (3, 0), (0, 0)])]
        for match, date in zip(games, ["2023-08-12", "2023-08-19", "2023-08-26", "2024-05-19", "2024-08-16", "2024-08-25"]):
            match["date"] = date
        self.assertEqual([m["id"] for m in epl.unbeaten_run(games[::-1], "A")], ["3", "4", "5"])

    def test_pandemic_season_accepts_its_late_finish(self):
        rows = [tuple("26/07/2020" if i == 1 else v for i, v in enumerate(r)) for r in full_season(2019)]
        self.assertEqual(len(epl.parse_csv(encoded(rows), 2019)), 380)

    def test_generated_graph_is_closed_and_points_are_qualified(self):
        games = epl.parse_csv(encoded(full_season()), 2024)
        docs = epl.build_documents(games, [{"start_year": 2024, "url": "https://example.com/results.csv"}])
        ids = {d["id"] for d in docs}
        self.assertEqual(len(ids), len(docs))
        targets = {t for d in docs for t in re.findall(r"\]\(/([^()]+)\.md\)", d["markdown"])}
        self.assertTrue(targets.issubset(ids))
        season = next(d for d in docs if d["id"] == "epl/seasons/2024-25")
        self.assertIn("NOT official final league standings", season["markdown"])
        for d in docs:
            self.assertEqual(d["revision"], epl.digest(d["markdown"].encode()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
