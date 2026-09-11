"""Regression coverage for result tracking and source-selection fixes."""

import os
import tempfile
import unittest
from unittest.mock import patch

import fetch_results
import prediction_tracker
import scraping
import send_daily_telegram


class ResultTrackingTests(unittest.TestCase):
    def test_oo05_settlement_uses_the_selected_team(self):
        results = [{"home_team": "Home", "away_team": "Away", "score": "1-0"}]
        base_pick = {"home_team": "Home", "away_team": "Away"}

        self.assertEqual(
            fetch_results.determine_oo05_result(
                {**base_pick, "prediction": "home_team_goals"}, results
            ),
            "win",
        )
        self.assertEqual(
            fetch_results.determine_oo05_result(
                {**base_pick, "prediction": "away_team_goals"}, results
            ),
            "loss",
        )

    def test_manual_result_overrides_less_authoritative_sources(self):
        manual = [{"home_team": "Home", "away_team": "Away", "score": "2-0", "source": "manual"}]
        scraped = [{"home_team": "Home", "away_team": "Away", "score": "0-0", "source": "soccerbase"}]
        with patch.object(fetch_results, "fetch_manual_override", return_value=manual), \
             patch.object(fetch_results, "fetch_football_data_org", return_value=[]), \
             patch.object(fetch_results, "fetch_api_football", return_value=[]), \
             patch.object(fetch_results, "fetch_soccerbase_results", return_value=scraped):
            self.assertEqual(fetch_results.fetch_match_results("2026-09-11"), manual)

    def test_distinct_clubs_do_not_match_after_normalization(self):
        self.assertNotEqual(
            fetch_results.normalize_team_name("Manchester City"),
            fetch_results.normalize_team_name("Manchester United"),
        )
        self.assertFalse(fetch_results.team_names_match("Manchester City", "Manchester United"))

    def test_oo05_predictions_are_accepted_by_the_tracker(self):
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                with patch.object(prediction_tracker, "HISTORY_FILE", "history.json"), \
                     patch.object(prediction_tracker, "BLOCKED_REGIONS", []), \
                     patch.object(prediction_tracker, "is_statistical_block_only", return_value=False):
                    stats = prediction_tracker.record_predictions(
                        "2026-09-11",
                        oo05_picks=[{
                            "home": "Home", "away": "Away", "league": "Test League",
                            "prediction": "away_team_goals", "confidence": "qualified",
                        }],
                    )
                    history = prediction_tracker.load_history()
            finally:
                os.chdir(old_cwd)
        self.assertEqual(stats["added"], 1)
        self.assertEqual(history["oo05"][0]["prediction"], "away_team_goals")


class H2HDeduplicationTests(unittest.TestCase):
    def test_mirrored_team_pages_produce_one_h2h_meeting(self):
        home_page = [{
            "date_str": "2026-09-01", "gf": 2, "ga": 1, "is_home": True,
            "result": "W", "opponent_team_id": "away",
        }]
        away_page = [{
            "date_str": "2026-09-01", "gf": 1, "ga": 2, "is_home": False,
            "result": "L", "opponent_team_id": "home",
        }]

        def results_fetcher(team_id):
            return home_page if team_id == "home" else away_page

        meetings = scraping.get_h2h_meetings("home", "away", results_fetcher)
        self.assertEqual(len(meetings), 1)
        self.assertEqual((meetings[0]["gf"], meetings[0]["ga"]), (2, 1))


class TelegramTests(unittest.TestCase):
    def test_http_failure_is_reported_to_the_caller(self):
        response = unittest.mock.Mock()
        response.status_code = 500
        response.raise_for_status.side_effect = RuntimeError("telegram failure")
        with patch.object(send_daily_telegram.requests, "post", return_value=response):
            self.assertFalse(send_daily_telegram.send("token", "chat", "message"))


if __name__ == "__main__":
    unittest.main()
