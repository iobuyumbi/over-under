"""Regression coverage for result tracking and source-selection fixes."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import fetch_results
import generate_weekly_report
import over25_soccerbase
import prediction_tracker
import scraping
import send_daily_telegram
import send_local_telegram
import build_telegram_daily


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

    def test_yesterday_telegram_block_includes_every_settled_pick_by_default(self):
        results = ["first", "second", "third"]
        with patch.object(
            prediction_tracker,
            "get_yesterday_results",
            return_value=("2026-09-10", results, {"wins": 3, "losses": 0, "pushes": 0, "pending": 0}),
        ):
            block = prediction_tracker.build_telegram_yesterday_block()
        self.assertEqual([line for line in block if line in results], results)


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
        response.text = "telegram failure"
        response.raise_for_status.side_effect = RuntimeError("telegram failure")
        with patch.object(send_daily_telegram.requests, "post", return_value=response):
            self.assertFalse(send_daily_telegram._send_chunks("token", "chat", "message"))

    def test_local_sender_includes_the_oo05_section(self):
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                with open("oo05_telegram.txt", "w", encoding="utf-8") as file:
                    file.write("OO05 pick")
                response = unittest.mock.Mock(status_code=200)
                with patch.dict(os.environ, {
                    "TELEGRAM_BOT_TOKEN": "123456789:TESTTOKEN", "TELEGRAM_CHAT_ID": "chat",
                    "TELEGRAM_VIP_CHAT_ID": "", "DATE": "2026-09-11",
                }, clear=False), patch.object(send_local_telegram.requests, "post", return_value=response) as post:
                    self.assertEqual(send_local_telegram.main(), 0)
            finally:
                os.chdir(old_cwd)
        free_texts = [
            call.kwargs["data"]["text"] for call in post.call_args_list
            if call.kwargs["data"]["chat_id"] == "chat"
        ]
        self.assertTrue(any("OO05 pick" in text for text in free_texts))

    def test_vip_board_separates_markets_sides_and_confidence(self):
        ou = (
            "  1. Alpha vs Beta\n"
            "     League: Test League\n"
            "     Pick: Over 2.5 · High confidence (80.0%)\n"
            "     Category:\n"
            "       • Tier: 🔥 Premium\n"
            "  2. Epsilon vs Zeta\n"
            "     League: Test League\n"
            "     Pick: Under 2.5 · High confidence (72.0%)\n"
            "     Category:\n"
            "       • Tier: ✅ Solid\n"
        )
        btts = (
            "  1. Theta vs Iota\n"
            "     League: Test League\n"
            "     Pick: BTTS No · Medium confidence (66.0%)\n"
            "     Category:\n"
            "       • Tier: ✅ Solid\n"
        )
        hw = (
            "  1. Gamma vs Delta\n"
            "     League: Other League\n"
            "     Pick: Home Win · Medium confidence (60.0%)\n"
            "     Category:\n"
            "       • Tier: ✅ Solid\n"
        )
        oo05 = (
            "  1. Kappa vs Lambda\n"
            "     League: Test League\n"
            "     Pick: Away to Score · Low confidence (50.0%)\n"
            "     Category:\n"
            "       • Tier: 👀 Watchlist\n"
        )
        board = build_telegram_daily.build_vip_board("2026-09-11", ou, btts, hw, oo05)
        self.assertIn("HIGH CONFIDENCE", board)
        self.assertIn("MEDIUM CONFIDENCE", board)
        self.assertIn("LOW CONFIDENCE", board)
        self.assertIn("80.0%", board)
        self.assertIn("Total: 5 picks", board)
        # Each market is its own list, and each pick sits in its market's block.
        self.assertIn("⚽️ OVER / UNDER 2.5", board)
        self.assertIn("🏠 HOME WIN", board)
        self.assertIn("🎯 BTTS (YES / NO)", board)
        self.assertLess(board.index("⚽️ OVER / UNDER 2.5"), board.index("🏠 HOME WIN"))
        self.assertLess(board.index("Alpha vs Beta"), board.index("🏠 HOME WIN"))
        self.assertLess(board.index("🏠 HOME WIN"), board.index("Gamma vs Delta"))
        # Sides are separated: Over before Under, No after Yes, Away after Home.
        self.assertIn("⬆️ OVER 2.5", board)
        self.assertIn("⬇️ UNDER 2.5", board)
        self.assertIn("🚫 BTTS NO", board)
        self.assertIn("✈️ AWAY TO SCORE", board)
        self.assertLess(board.index("⬆️ OVER 2.5"), board.index("⬇️ UNDER 2.5"))
        self.assertLess(board.index("Alpha vs Beta"), board.index("⬇️ UNDER 2.5"))
        self.assertLess(board.index("⬇️ UNDER 2.5"), board.index("Epsilon vs Zeta"))

    def test_vip_board_prepends_a_per_market_side_results_table(self):
        rows = [
            {"label": "Over 2.5", "wins": 26, "losses": 12, "pushes": 1,
             "pending": 2, "win_rate": 68.421},
            {"label": "Under 2.5", "wins": 2, "losses": 2, "pushes": 0,
             "pending": 0, "win_rate": 50.0},
            {"label": "BTTS No", "wins": 1, "losses": 2, "pushes": 0,
             "pending": 0, "win_rate": 33.333},
        ]
        board = build_telegram_daily.build_vip_board(
            "2026-09-11", "", "", "", "", results=("2026-09-10", rows)
        )

        self.assertIn("📊 YESTERDAY · 2026-09-10", board)
        # The results block comes before today's picks.
        self.assertLess(board.index("📊 YESTERDAY"), board.index("⚽️ OVER / UNDER 2.5"))
        # Every side keeps its own row instead of a blended market total.
        for label in ("Over 2.5", "Under 2.5", "BTTS No"):
            self.assertIn(label, board)
        over_row = [line for line in board.split("\n") if line.startswith("Over 2.5")][0]
        self.assertIn("26", over_row)
        self.assertIn("68.4%", over_row)
        self.assertIn("33.3%", board)
        self.assertIn("TOTAL", board)
        # Totals sum every side: 29W-16L of 45 decided.
        total_row = [line for line in board.split("\n") if line.startswith("TOTAL")][0]
        self.assertIn("64.4%", total_row)

    def test_vip_board_says_so_when_yesterday_has_no_settled_picks(self):
        board = build_telegram_daily.build_vip_board(
            "2026-09-11", "", "", "", "", results=("2026-09-10", [])
        )
        self.assertIn("📊 YESTERDAY · 2026-09-10", board)
        self.assertIn("no settled picks yesterday", board)

    def test_vip_board_lists_each_pick_with_its_actual_result_and_score(self):
        rows = [
            {"label": "Over 2.5", "wins": 1, "losses": 1, "pushes": 0,
             "pending": 1, "win_rate": 50.0},
        ]
        detail = [
            {"label": "Over 2.5", "match": "Alpha vs Beta",
             "result": "win", "score": "3-1"},
            {"label": "Over 2.5", "match": "Gamma vs Delta",
             "result": "loss", "score": "0-0"},
            {"label": "Home to Score", "match": "Epsilon vs Zeta",
             "result": "pending", "score": "-"},
        ]
        board = build_telegram_daily.build_vip_board(
            "2026-09-11", "", "", "", "",
            results=("2026-09-10", rows),
            details=("2026-09-10", detail),
        )

        self.assertIn("📋 YESTERDAY PICKS · 2026-09-10", board)
        # The per-pick list sits under the summary table and before today.
        self.assertLess(board.index("📊 YESTERDAY"), board.index("📋 YESTERDAY PICKS"))
        self.assertLess(board.index("📋 YESTERDAY PICKS"), board.index("⚽️ OVER / UNDER 2.5"))
        # Both wins and losses surface, each with its final score.
        self.assertIn("Win", board)
        self.assertIn("Loss", board)
        self.assertIn("Pending", board)
        self.assertIn("3-1", board)
        self.assertIn("0-0", board)
        # Every pick keeps its own market-side label.
        win_row = [line for line in board.split("\n") if "Alpha vs Beta" in line][0]
        self.assertIn("Over 2.5", win_row)
        self.assertIn("Win", win_row)
        self.assertIn("3-1", win_row)
        loss_row = [line for line in board.split("\n") if "Gamma vs Delta" in line][0]
        self.assertIn("Loss", loss_row)
        self.assertIn("0-0", loss_row)

    def test_vip_board_omits_the_per_pick_list_when_yesterday_had_none(self):
        board = build_telegram_daily.build_vip_board(
            "2026-09-11", "", "", "", "",
            results=("2026-09-10", []),
            details=("2026-09-10", []),
        )
        self.assertIn("no settled picks yesterday", board)
        self.assertNotIn("📋 YESTERDAY PICKS", board)


class YesterdayBreakdownTests(unittest.TestCase):
    def test_breakdown_splits_sides_and_keeps_voids_out_of_the_win_rate(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        history = {
            "over_under": [
                {"date": yesterday, "home_team": "A", "away_team": "B",
                 "prediction": "over", "result": "win", "final_score": "3-1"},
                {"date": yesterday, "home_team": "C", "away_team": "D",
                 "prediction": "under", "result": "loss", "final_score": "1-1"},
                {"date": yesterday, "home_team": "E", "away_team": "F",
                 "prediction": "under", "result": "push", "final_score": "0-2"},
            ],
            "btts": [
                {"date": yesterday, "home_team": "G", "away_team": "H",
                 "prediction": "no", "result": "win", "final_score": "1-0"},
            ],
            "home_win": [
                {"date": yesterday, "home_team": "I", "away_team": "J",
                 "prediction": "home_win", "result": "win", "final_score": "2-0"},
            ],
            "oo05": [
                {"date": yesterday, "home_team": "K", "away_team": "L",
                 "prediction": "away_team_goals", "result": "loss", "final_score": "0-0"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                with open("history.json", "w", encoding="utf-8") as file:
                    json.dump(history, file)
                with patch.object(prediction_tracker, "HISTORY_FILE", "history.json"):
                    date, rows = prediction_tracker.get_yesterday_market_breakdown()
            finally:
                os.chdir(old_cwd)

        self.assertEqual(date, yesterday)
        by_label = {row["label"]: row for row in rows}
        self.assertEqual(
            sorted(by_label),
            ["Away to Score", "BTTS No", "Home Win", "Over 2.5", "Under 2.5"],
        )
        self.assertEqual(by_label["Over 2.5"]["wins"], 1)
        self.assertEqual(by_label["Under 2.5"]["losses"], 1)
        self.assertEqual(by_label["Under 2.5"]["pushes"], 1)
        self.assertEqual(by_label["Home Win"]["wins"], 1)
        self.assertEqual(by_label["Away to Score"]["losses"], 1)
        # Win rate is W/(W+L), so the void must not drag Under 2.5 to a third.
        self.assertEqual(by_label["Over 2.5"]["win_rate"], 100.0)
        self.assertEqual(by_label["Under 2.5"]["win_rate"], 0.0)
        # Sides with no picks yesterday are omitted rather than shown as zeros.
        self.assertNotIn("Home to Score", by_label)
        self.assertNotIn("BTTS Yes", by_label)

    def test_breakdown_splits_each_side_by_confidence_tier(self):
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        history = {
            "over_under": [
                {"date": yesterday, "home_team": "A", "away_team": "B",
                 "prediction": "over", "confidence": "perfect",
                 "result": "win", "final_score": "3-1"},
                {"date": yesterday, "home_team": "C", "away_team": "D",
                 "prediction": "over", "confidence": "qualified",
                 "result": "loss", "final_score": "0-0"},
                {"date": yesterday, "home_team": "E", "away_team": "F",
                 "prediction": "over", "confidence": "qualified",
                 "result": "win", "final_score": "4-1"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            old_cwd = os.getcwd()
            try:
                os.chdir(directory)
                with open("history.json", "w", encoding="utf-8") as file:
                    json.dump(history, file)
                with patch.object(prediction_tracker, "HISTORY_FILE", "history.json"):
                    _, rows = prediction_tracker.get_yesterday_market_breakdown()
            finally:
                os.chdir(old_cwd)

        # perfect/qualified collapse onto the Premium/Solid VIP tiers.
        by_tier = {row["tier"]: row for row in rows}
        self.assertEqual(sorted(by_tier), ["Premium", "Solid"])
        self.assertTrue(all(row["label"] == "Over 2.5" for row in rows))
        self.assertEqual(by_tier["Premium"]["wins"], 1)
        self.assertEqual(by_tier["Solid"]["wins"], 1)
        self.assertEqual(by_tier["Solid"]["losses"], 1)
        self.assertEqual(by_tier["Solid"]["win_rate"], 50.0)

    def test_results_block_shows_a_tier_row_per_side_with_a_subtotal(self):
        rows = [
            {"label": "Over 2.5", "tier": "Premium", "wins": 1, "losses": 0,
             "pushes": 0, "pending": 0, "win_rate": 100.0},
            {"label": "Over 2.5", "tier": "Solid", "wins": 3, "losses": 1,
             "pushes": 0, "pending": 2, "win_rate": 75.0},
        ]
        block = "\n".join(build_telegram_daily._render_results_block(rows))
        self.assertIn("Premium", block)
        self.assertIn("Solid", block)
        # A side spanning two tiers gets an ALL subtotal summing both.
        subtotal = [line for line in block.split("\n") if "ALL" in line][0]
        self.assertIn("80.0%", subtotal)  # 4/(4+1)
        total = [line for line in block.split("\n") if line.startswith("TOTAL")][0]
        self.assertIn("80.0%", total)


class WeeklyReportSplitTests(unittest.TestCase):
    def test_negative_sides_are_tracked_separately_in_the_readme_report(self):
        history = {
            "over_under": [
                {"date": "2026-09-01", "prediction": "over", "result": "win"},
                {"date": "2026-09-01", "prediction": "under", "result": "loss"},
                {"date": "2026-09-01", "prediction": "under", "result": "win"},
            ],
            "btts": [
                {"date": "2026-09-01", "prediction": "yes", "result": "win"},
                {"date": "2026-09-01", "prediction": "no", "result": "loss"},
            ],
            "home_win": [{"date": "2026-09-01", "prediction": "home_win", "result": "win"}],
            "oo05": [{"date": "2026-09-01", "prediction": "over05_tg", "result": "win"}],
        }
        stats = generate_weekly_report.calculate_weekly_stats(history, weeks=6)

        self.assertEqual(stats["over_25"][0]["total"], 1)
        self.assertEqual(stats["under_25"][0]["total"], 2)
        self.assertEqual(stats["under_25"][0]["wins"], 1)
        self.assertEqual(stats["btts_yes"][0]["total"], 1)
        self.assertEqual(stats["btts_no"][0]["total"], 1)

        section = generate_weekly_report.generate_readme_section(stats)
        self.assertIn("### Over 2.5 Goals", section)
        self.assertIn("### Under 2.5 Goals", section)
        self.assertIn("### BTTS Yes", section)
        self.assertIn("### BTTS No", section)
        # The old merged headings must be gone, or the sides would hide each other.
        self.assertNotIn("### BTTS\n", section)
        self.assertIn("| 2 | 1 | 1 | 50.0% |", section)


class PublishGuardTests(unittest.TestCase):
    """CI must not re-post a board older than the publish date.

    Only run_local.bat builds a board, so without this guard the nightly
    publish-daily job re-sends the last committed picks under a fresh header.
    """

    def _publish(self, directory, board_date, publish_date):
        old_cwd = os.getcwd()
        try:
            os.chdir(directory)
            with open("telegram.txt", "w", encoding="utf-8") as file:
                file.write(f"📅 Daily Soccer Picks · {board_date}\n\n⚽️ OVER / UNDER 2.5\n")
            response = unittest.mock.Mock(status_code=200)
            with patch.dict(os.environ, {
                "TELEGRAM_BOT_TOKEN": "123456789:TESTTOKEN", "TELEGRAM_CHAT_ID": "chat",
                "TELEGRAM_VIP_CHAT_ID": "", "DATE": publish_date,
            }, clear=False), patch.object(send_daily_telegram.requests, "post", return_value=response) as post:
                code = send_daily_telegram.main()
        finally:
            os.chdir(old_cwd)
        return code, post

    def test_stale_board_is_not_republished(self):
        with tempfile.TemporaryDirectory() as directory:
            code, post = self._publish(directory, "2026-09-10", "2026-09-11")
        self.assertEqual(code, 0)
        post.assert_not_called()

    def test_board_matching_the_publish_date_is_sent(self):
        with tempfile.TemporaryDirectory() as directory:
            code, post = self._publish(directory, "2026-09-11", "2026-09-11")
        self.assertEqual(code, 0)
        self.assertTrue(post.called)


class OverGoalVolumeRuleTests(unittest.TestCase):
    """The one-sided goal-volume rule: the team scored, OR its opponent
    conceded, OVER_SCORED_OR_CONCEDED_MIN_6 over the last 6 — in contrast to
    the combined "total goals >= 18" rule, which needs both legs busy.
    """

    def setUp(self):
        self.hot = [(3, 0), (2, 0), (4, 0), (1, 0), (2, 0), (1, 0)]      # 13 scored
        self.cold = [(1, 0), (0, 1), (1, 0), (0, 0), (1, 0), (0, 1)]     # 3 scored
        self.stingy = [(0, 0), (1, 0), (0, 0), (1, 0), (0, 0), (1, 0)]   # 0 conceded
        self.sieve = [(0, 2), (0, 2), (0, 2), (0, 2), (0, 2), (0, 2)]    # 12 conceded

    def test_hot_attack_passes_without_a_leaky_opponent(self):
        scored, conceded, passed = over25_soccerbase._one_sided_goal_volume(
            self.hot, self.stingy, 6, 6
        )
        self.assertEqual((scored, conceded), (13, 0))
        self.assertTrue(passed)

    def test_leaky_opponent_passes_without_a_hot_attack(self):
        scored, conceded, passed = over25_soccerbase._one_sided_goal_volume(
            self.cold, self.sieve, 6, 6
        )
        self.assertEqual((scored, conceded), (3, 12))
        self.assertTrue(passed)

    def test_both_legs_cold_fails(self):
        scored, conceded, passed = over25_soccerbase._one_sided_goal_volume(
            self.cold, self.stingy, 6, 6
        )
        self.assertEqual((scored, conceded), (3, 0))
        self.assertFalse(passed)

    def test_threshold_scales_down_for_thin_data(self):
        # 3 games available drops the threshold from 10 to 5 (10 * 3 / 6).
        scored, _, passed = over25_soccerbase._one_sided_goal_volume(
            [(2, 0), (1, 1), (2, 0)], [], 3, 0
        )
        self.assertEqual(scored, 5)
        self.assertTrue(passed)

    def test_rulebook_records_the_volume_item_per_side(self):
        passed, failed, details, _ = over25_soccerbase.apply_over_algorithm(
            self.hot[:3], self.cold[:3], self.hot, self.cold
        )
        self.assertIn("Home goal volume (last 6)", details)
        self.assertIn("Home goal volume (last 6)", passed)
        # Away is cold on both legs here, so its item must fail, not vanish.
        self.assertIn("Away goal volume (last 6)", details)
        self.assertIn("Away goal volume (last 6)", failed)


class SterileAttackVetoTests(unittest.TestCase):
    """Over 2.5 losses are dominated by games where one side fails to score:
    81% of settled Over losses finished with at least one team on 0 goals
    (vs 19% of wins). _sterile_attack_veto_over widens the participation lens
    from the last three venue games to the whole six-game window so a side
    that blanks chronically but happened to score in two of its last three
    no longer slides through.
    """

    def setUp(self):
        self.healthy = [(2, 1), (1, 1), (2, 0), (1, 2), (2, 1), (1, 0)]   # scored 6/6
        self.sterile = [(0, 1), (0, 2), (0, 0), (1, 0), (0, 1), (1, 1)]   # scored 2/6

    def test_two_scoring_attacks_pass(self):
        self.assertEqual(
            over25_soccerbase._sterile_attack_veto_over(self.healthy, self.healthy),
            (False, None),
        )

    def test_sterile_home_is_vetoed(self):
        veto, reason = over25_soccerbase._sterile_attack_veto_over(self.sterile, self.healthy)
        self.assertTrue(veto)
        self.assertIn("home_sterile", reason)

    def test_sterile_away_is_vetoed(self):
        veto, reason = over25_soccerbase._sterile_attack_veto_over(self.healthy, self.sterile)
        self.assertTrue(veto)
        self.assertIn("away_sterile", reason)

    def test_thin_sample_below_three_games_is_not_judged(self):
        self.assertEqual(
            over25_soccerbase._sterile_attack_veto_over([(0, 1), (0, 0)], self.healthy),
            (False, None),
        )

    def test_three_game_window_matches_the_existing_two_of_three_gate(self):
        # 2 of 3 scored -> pass (same bar as _over_btts_participation_gate).
        self.assertEqual(
            over25_soccerbase._sterile_attack_veto_over([(1, 0), (0, 1), (2, 0)], self.healthy),
            (False, None),
        )
        # 1 of 3 scored -> blocked.
        veto, reason = over25_soccerbase._sterile_attack_veto_over(
            [(1, 0), (0, 1), (0, 2)], self.healthy
        )
        self.assertTrue(veto)
        self.assertIn("home_sterile_1of3", reason)


class OverLambdaGateTests(unittest.TestCase):
    """The Over gate demands a real model lean: combined lambda must clear
    MIN_COMBINED_LAMBDA_OVER, which maps to a ~60% Poisson Over probability.
    """

    def test_weak_over_lean_is_rejected(self):
        # 2.75 combined ~= 52% Over probability: the old, too-loose bar.
        self.assertFalse(over25_soccerbase.lambda_gate_passes(1.40, 1.35, "over"))

    def test_strong_over_lean_passes(self):
        # 3.20 combined ~= 62% Over probability.
        self.assertTrue(over25_soccerbase.lambda_gate_passes(1.65, 1.55, "over"))

    def test_under_gate_is_unchanged_by_the_over_tightening(self):
        # 2.10 combined clears the Under ceiling (2.15) but not the Over bar.
        self.assertTrue(over25_soccerbase.lambda_gate_passes(1.05, 1.05, "under"))
        self.assertFalse(over25_soccerbase.lambda_gate_passes(1.05, 1.05, "over"))


if __name__ == "__main__":
    unittest.main()
