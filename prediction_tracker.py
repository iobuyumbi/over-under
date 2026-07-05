#!/usr/bin/env python3 
""" 
PREDICTION TRACKER - PRODUCTION VERSION 
======================================= 
Tracks performance of Over 2.5 and Home Win predictions with auto-updating stats. 
""" 
 
import json
import os
import csv
import logging
from datetime import datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO) 
 
HISTORY_FILE = "prediction_history.json" 
SETTLED_RESULTS = frozenset({"win", "loss", "push"})
MEDIUM = "MEDIUM"

# User-facing label helpers
PICK_TIER_PREMIUM = "Premium picks"
PICK_TIER_STRONG = "Solid picks"
PICK_TIER_VALUE = "Watchlist"


def format_confidence_label(confidence):
    """Turn model confidence into readable report text."""
    mapping = {
        "HIGH": "High confidence",
        "MEDIUM": "Medium confidence",
        "LOW": "Low confidence",
        "perfect": "Premium",
        "qualified": "Solid",
        "close": "Watchlist",
    }
    text = str(confidence or "N/A")
    return mapping.get(text, mapping.get(text.upper(), text))


def format_result_badge(result):
    """Short result label for settled picks."""
    normalized = str(result or "").lower()
    return {"win": "Win", "loss": "Loss", "push": "Push"}.get(normalized, str(result or "Pending"))


def format_result_tag(result):
    """Bracket tag for results summaries."""
    normalized = str(result or "").lower()
    return {"win": "WIN", "loss": "LOSS", "push": "PUSH"}.get(normalized, "PENDING")

# Flagged regions with match-integrity concerns.
# - New predictions are blocked (fixtures filtered before analysis).
# - Historical results in prediction_history.json stay for audit.
# To add a region: copy the Ireland block and set league_keywords + team_keywords.
BLOCKED_REGIONS = (
    {
        "name": "Ireland",
        "league_keywords": (
            "ireland",
            "fai cup",
            "irish",
        ),
        "team_keywords": (
            "finn harps", "drogheda", "bray", "derry city", "waterford",
            "bohemians", "cork city", "dundalk", "athlone", "wexford",
            "longford", "ucd", "shelbourne", "shamrock", "sligo rovers",
            "galway united", "limerick", "treaty united", "cobh ramblers",
            "kerry fc", "st patrick", "st patricks",
        ),
    },
)


def _normalize_label(value):
    return str(value or "").strip().lower()


def is_blocked_league(league):
    """True when a league belongs to a flagged region."""
    normalized = _normalize_label(league)
    if not normalized:
        return False
    for region in BLOCKED_REGIONS:
        if any(keyword in normalized for keyword in region["league_keywords"]):
            return True
    return False


def is_blocked_team(team_name):
    """True when a club name matches a flagged region."""
    normalized = _normalize_label(team_name)
    if not normalized:
        return False
    for region in BLOCKED_REGIONS:
        if any(keyword in normalized for keyword in region["team_keywords"]):
            return True
    return False


def is_blocked_match(home, away, league=""):
    """True when a fixture should be excluded from new predictions only."""
    if is_blocked_league(league):
        return True
    return is_blocked_team(home) or is_blocked_team(away)


def is_blocked_pick(pick):
    return is_blocked_match(
        pick.get("home_team", pick.get("home", "")),
        pick.get("away_team", pick.get("away", "")),
        pick.get("league", ""),
    )


def is_blocked_fixture(fixture):
    return is_blocked_match(
        fixture.get("home", fixture.get("home_team", "")),
        fixture.get("away", fixture.get("away_team", "")),
        fixture.get("league", ""),
    )
 
 
def home_win_key(pick):
    return (pick["date"], pick.get("home_team", pick.get("home")), 
            pick.get("away_team", pick.get("away")), pick["confidence"])


def over_under_key(pick):
    return (
        pick["date"],
        pick.get("home_team", pick.get("home")),
        pick.get("away_team", pick.get("away")),
        pick["prediction"],
        pick["confidence"],
    )


def _pick_better(existing, new):
    """Prefer settled results; otherwise keep the most recent entry."""
    existing_settled = existing.get("result") in SETTLED_RESULTS
    new_settled = new.get("result") in SETTLED_RESULTS
    if existing_settled and not new_settled:
        return existing
    if new_settled and not existing_settled:
        return new
    if existing.get("recorded_at", "") >= new.get("recorded_at", ""):
        return existing
    return new


def dedupe_predictions(picks, key_fn):
    best = {}
    for pick in picks:
        key = key_fn(pick)
        best[key] = _pick_better(best[key], pick) if key in best else pick
    return list(best.values())


def dedupe_history(history=None, save=True):
    """Remove duplicate prediction rows from history."""
    history = history or load_history()
    before_hw = len(history["home_win"])
    before_ou = len(history["over_under"])

    history["home_win"] = dedupe_predictions(history["home_win"], home_win_key)
    history["over_under"] = dedupe_predictions(history["over_under"], over_under_key)

    stats = {
        "home_win_removed": before_hw - len(history["home_win"]),
        "over_under_removed": before_ou - len(history["over_under"]),
        "home_win_remaining": len(history["home_win"]),
        "over_under_remaining": len(history["over_under"]),
    }

    if save:
        save_history(history)
    return history, stats

 
def add_to_manual_results_csv(date_str, home_team, away_team):
    """Add a match to manual_results.csv if it doesn't already exist."""
    csv_path = "manual_results.csv"
    existing_rows = []
    
    # Read existing CSV
    if os.path.exists(csv_path):
        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                existing_rows = list(reader)
        except Exception as e:
            logger.warning(f"Could not read existing manual_results.csv: {e}")
    
    # Check if match already exists
    match_exists = False
    for row in existing_rows:
        if (row.get("date") == date_str and
            row.get("home_team") == home_team and
            row.get("away_team") == away_team):
            match_exists = True
            break
    
    # Add if not exists
    if not match_exists:
        new_row = {
            "date": date_str,
            "home_team": home_team,
            "away_team": away_team,
            "score": ""
        }
        existing_rows.append(new_row)
        
        # Write back to CSV
        try:
            with open(csv_path, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["date", "home_team", "away_team", "score"])
                writer.writeheader()
                for row in existing_rows:
                    # Ensure all fields exist
                    for field in ["date", "home_team", "away_team", "score"]:
                        if field not in row:
                            row[field] = ""
                    writer.writerow(row)
            logger.info(f"Added to manual_results.csv: {date_str} - {home_team} vs {away_team}")
        except Exception as e:
            logger.error(f"Could not write to manual_results.csv: {e}")


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"home_win": [], "over_under": [], "stats": {}} 
 
 
def save_history(history): 
    with open(HISTORY_FILE, "w") as f: 
        json.dump(history, f, indent=2, default=str) 
 
 
def record_predictions(date_str, home_win_picks=None, over_under_picks=None): 
    """Record new predictions (called after running predictors)""" 
    history = load_history() 
    existing_hw = {home_win_key(p) for p in history["home_win"]}
    existing_ou = {over_under_key(p) for p in history["over_under"]}
    added = 0 
    skipped = 0

    if home_win_picks: 
        for pick in home_win_picks:
            if is_blocked_match(
                pick.get("home", pick.get("home_team", "")),
                pick.get("away", pick.get("away_team", "")),
                pick.get("league", ""),
            ):
                skipped += 1
                continue
            entry = { 
                "date": pick.get("date", date_str), 
                "type": "home_win", 
                "league": pick.get("league"), 
                "home_team": pick.get("home"), 
                "away_team": pick.get("away"), 
                "confidence": pick.get("confidence", MEDIUM), 
                "result": "pending", 
                "recorded_at": datetime.now().isoformat() 
            } 
            key = home_win_key(entry)
            if key in existing_hw:
                skipped += 1
                continue
            history["home_win"].append(entry) 
            existing_hw.add(key)
            added += 1 
            # Add to manual_results.csv
            add_to_manual_results_csv(entry["date"], entry["home_team"], entry["away_team"])

    if over_under_picks: 
        for pick in over_under_picks:
            if is_blocked_match(
                pick.get("home", pick.get("home_team", "")),
                pick.get("away", pick.get("away_team", "")),
                pick.get("league", ""),
            ):
                skipped += 1
                continue
            entry = { 
                "date": pick.get("date", date_str), 
                "type": "over_under", 
                "league": pick.get("league"), 
                "home_team": pick.get("home"), 
                "away_team": pick.get("away"), 
                "prediction": pick.get("prediction", "over"), 
                "prob": pick.get("prob", pick.get("over25_prob")), 
                "confidence": pick.get("confidence", MEDIUM), 
                "result": "pending", 
                "recorded_at": datetime.now().isoformat() 
            } 
            key = over_under_key(entry)
            if key in existing_ou:
                skipped += 1
                continue
            history["over_under"].append(entry) 
            existing_ou.add(key)
            added += 1 
            # Add to manual_results.csv
            add_to_manual_results_csv(entry["date"], entry["home_team"], entry["away_team"])

    if added > 0: 
        save_history(history) 
        print(f"[OK] Recorded {added} new predictions for {date_str}") 

    return {"added": added, "skipped": skipped}
 
 
def update_result(date_str, home_team, away_team, result, prediction_type="over_under"): 
    """Manually update result after match ends: 'win', 'loss', 'push'""" 
    history = load_history() 
    updated = 0 
 
    picks = history["over_under"] if prediction_type == "over_under" else history["home_win"] 
 
    for pick in picks: 
        pick_home = pick.get("home_team", pick.get("home"))
        pick_away = pick.get("away_team", pick.get("away"))
        if (pick.get("date") == date_str and 
            pick_home == home_team and 
            pick_away == away_team): 
            pick["result"] = result 
            pick["updated_at"] = datetime.now().isoformat() 
            updated += 1 
 
    if updated > 0: 
        save_history(history) 
        print(f"[OK] Updated {updated} match result(s) to '{result}'") 
    else: 
        print("[WARN] No matching prediction found to update.") 
 
    return updated 
 
 
def get_performance_summary(days=30): 
    """Return performance stats""" 
    history = load_history() 
    cutoff = datetime.now() - timedelta(days=days) 
    
    stats = {"home_win": {}, "over_under": {}} 
 
    for ptype in ["home_win", "over_under"]: 
        picks = history.get(ptype, []) 
        wins = losses = pushes = pending = 0 
        recent = [p for p in picks if datetime.fromisoformat(p["date"]) >= cutoff] 
 
        for p in recent: 
            res = resolve_pick_result(p)
            if res == "win": 
                wins += 1 
            elif res == "loss": 
                losses += 1 
            elif res == "push": 
                pushes += 1 
            elif count_report_pending(p):
                pending += 1 
 
        total_decided = wins + losses + pushes 
        win_rate = (wins / total_decided * 100) if total_decided > 0 else 0 
 
        stats[ptype] = { 
            "total": len(recent), 
            "wins": wins, 
            "losses": losses, 
            "pushes": pushes, 
            "pending": pending, 
            "win_rate": round(win_rate, 1), 
            "roi_estimate": round((wins * 0.9 - losses) / max(1, (wins+losses)), 2) 
        } 
 
    return stats 
 
 
def print_summary(): 
    stats = get_performance_summary(days=30) 
    print("\n📊 PERFORMANCE SUMMARY (Last 30 Days)") 
    print("=" * 50) 
    for ptype, s in stats.items(): 
        print(f"\n{ptype.upper().replace('_', ' ')}:") 
        print(f"  Total: {s['total']} | Win Rate: {s['win_rate']}%") 
        print(f"  Wins: {s['wins']} | Losses: {s['losses']} | Pending: {s['pending']}") 
 
 
def get_pending_predictions(days_old=None, due_only=True):
    """Get pending predictions (no result recorded yet).

    due_only: when True, skip today and future fixtures (only overdue past dates).
    """
    history = load_history()
    pending = []
    today = datetime.now().date()

    for p_type in ["home_win", "over_under"]:
        for idx, pick in enumerate(history[p_type]):
            if pick["result"] != "pending":
                continue
            date_str = pick["date"][:10]
            try:
                pick_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if due_only and pick_date >= today:
                continue
            pick = dict(pick)
            pick["type"] = p_type
            pick["index"] = idx
            if days_old:
                if (datetime.now() - datetime.combine(pick_date, datetime.min.time())).days <= days_old:
                    pending.append(pick)
            else:
                pending.append(pick)

    return pending


def pick_is_due(pick):
    """True when the fixture date is today or earlier (eligible for settlement)."""
    try:
        pick_date = datetime.strptime(pick["date"][:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return pick_date <= datetime.now().date()


def pick_is_overdue(pick):
    """True when the fixture date is before today (should have a final score)."""
    try:
        pick_date = datetime.strptime(pick["date"][:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return pick_date < datetime.now().date()


def count_report_pending(pick):
    """Pending for performance reports: only unsettled past fixtures."""
    return pick.get("result") == "pending" and pick_is_overdue(pick)


def format_yesterday_line(pick, market_label):
    """Compact one-line summary for daily prediction reports."""
    home = pick.get("home_team", pick.get("home"))
    away = pick.get("away_team", pick.get("away"))
    result = resolve_pick_result(pick)
    score = pick.get("final_score", "")

    if result in SETTLED_RESULTS:
        line = f"{home} vs {away} · {market_label} · {format_result_badge(result)}"
        if score:
            line += f" ({score})"
        return line
    if pick.get("result") == "pending" and pick_is_overdue(pick):
        return f"{home} vs {away} · {market_label} · Pending"
    return None


def format_pick_result_lines(pick, market_label):
    """Detailed result lines for one pick (matches selected-results style)."""
    home = pick.get("home_team", pick.get("home"))
    away = pick.get("away_team", pick.get("away"))
    result = resolve_pick_result(pick)
    score = pick.get("final_score", "")

    if result in SETTLED_RESULTS and score:
        tag = format_result_tag(result)
        lines = [
            f"[{tag}] {home} vs {away}",
            f"   {market_label} — {format_result_badge(result)} ({score})",
        ]
        safer = format_safer_result_line(market_label, score)
        if safer:
            lines.append(safer)
        lines.append("")
        return lines

    if pick.get("result") == "pending" and pick_is_overdue(pick):
        return [f"[PENDING] {home} vs {away} · {market_label}", ""]

    return []


def get_yesterday_results(prediction_type=None, detailed=False):
    """Get yesterday's results for daily reports.

    Blocked regions are still included here so past results stay visible.
    """
    history = load_history()
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    results = []
    summary = {"wins": 0, "losses": 0, "pushes": 0, "pending": 0}

    def tally(result):
        if result == "win":
            summary["wins"] += 1
        elif result == "loss":
            summary["losses"] += 1
        elif result == "push":
            summary["pushes"] += 1

    def append_pick(pick, market_label):
        result = resolve_pick_result(pick)
        if result in SETTLED_RESULTS:
            tally(result)
        elif pick.get("result") == "pending" and pick_is_overdue(pick):
            summary["pending"] += 1
        else:
            return

        if detailed:
            results.extend(format_pick_result_lines(pick, market_label))
        else:
            line = format_yesterday_line(pick, market_label)
            if line:
                results.append(line)

    if prediction_type in (None, "home_win"):
        for pick in history["home_win"]:
            if pick.get("date", "")[:10] != yesterday:
                continue
            append_pick(pick, "Home Win" if detailed else "HOME WIN")

    if prediction_type in (None, "over_under"):
        for pick in history["over_under"]:
            if pick.get("date", "")[:10] != yesterday:
                continue
            market = (
                "Over 2.5"
                if pick.get("prediction", "over").lower() in ("over", "over 2.5")
                else "Under 2.5"
            )
            if not detailed:
                market = market.upper()
            append_pick(pick, market)

    while results and results[-1] == "":
        results.pop()

    return yesterday, results, summary


def append_yesterday_section(lines, prediction_type, detailed=False):
    """Add a spaced yesterday block before today's picks."""
    yesterday_date, yesterday_results, yesterday_summary = get_yesterday_results(
        prediction_type, detailed=detailed
    )
    if not yesterday_results:
        return

    lines.append("YESTERDAY")
    lines.append(f"Date: {yesterday_date}")
    lines.append("")
    header = format_yesterday_header(yesterday_summary)
    if header:
        lines.append(f"  {header}")
        lines.append("")

    if detailed:
        for line in yesterday_results:
            lines.append(line)
    else:
        for line in yesterday_results:
            lines.append(f"  {line}")
            lines.append("")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("TODAY")
    lines.append("")


def format_yesterday_header(summary): 
    """Build a one-line record summary for daily reports.""" 
    settled = summary["wins"] + summary["losses"] + summary["pushes"] 
    if settled: 
        line = f"Yesterday: {summary['wins']}W-{summary['losses']}L-{summary['pushes']}P" 
        if summary["pending"]: 
            line += f" ({summary['pending']} pending)" 
        return line 
    if summary["pending"]: 
        return f"Pending: {summary['pending']}" 
    return None


def parse_final_score(score_str):
    """Parse a score string like '2-1' into (home_goals, away_goals) or None."""
    if not score_str or "-" not in str(score_str):
        return None
    try:
        home, away = map(int, str(score_str).split("-", 1))
        return home, away
    except ValueError:
        return None


def resolve_pick_result(pick):
    """Normalize stored results. Home-win draws are losses, not pushes."""
    result = pick.get("result")
    if result == "push":
        parsed = parse_final_score(pick.get("final_score"))
        if parsed and parsed[0] == parsed[1]:
            return "loss"
    return result


def compute_safer_result(market, score_str):
    """
    Derive safer-market result from a final score.
    market: 'double_chance', 'over_1_5', or 'under_3_5'
    Returns 'win', 'loss', or None if score is unavailable.
    """
    parsed = parse_final_score(score_str)
    if parsed is None:
        return None
    home, away = parsed
    total = home + away
    if market == "double_chance":
        return "win" if home >= away else "loss"
    if market == "over_1_5":
        return "win" if total >= 2 else "loss"
    if market == "under_3_5":
        return "win" if total <= 3 else "loss"
    return None


def safer_market_label(primary_market):
    """Map a primary prediction label to its safer companion market."""
    market = (primary_market or "").lower()
    if market in ("home win", "home_win"):
        return "double_chance", "Double Chance (Home or Draw)"
    if market in ("over", "over 2.5"):
        return "over_1_5", "Over 1.5 Goals"
    if market in ("under", "under 2.5"):
        return "under_3_5", "Under 3.5 Goals"
    return None, None


def format_safer_result_line(primary_market, score_str):
    """Format one safer-pick result line for reports."""
    market_key, label = safer_market_label(primary_market)
    if not market_key:
        return None
    result = compute_safer_result(market_key, score_str)
    if result is None:
        return None
    return f"   Safer pick · {label} — {format_result_badge(result)} ({score_str})"


def format_vip_rule_summary(details, score, max_score):
    """Compact human-readable rule summary for VIP pick lines."""
    failed = []
    for name in sorted(details):
        value = str(details[name])
        if value.startswith("FAIL"):
            detail = value[4:].strip(" ()")
            failed.append(f"{name} ({detail})" if detail else name)

    lines = [f"Profile: {score}/{max_score} checks passed"]
    if failed:
        note = failed[0]
        if len(failed) > 1:
            note += f"; {failed[1]}"
        if len(failed) > 2:
            note += f" (+{len(failed) - 2} more)"
        lines.append(f"Missed: {note}")
    return lines


def format_vip_extra_lines(stake_pct, odds, score, max_score, *,
                           home_strength=None, away_strength=None,
                           home_lambda=None, away_lambda=None):
    """VIP extras on top of the free-channel pick summary."""
    lines = [f"Suggested stake: {stake_pct:.1f}% @ {odds}"]
    if home_strength is not None and away_strength is not None:
        lines.append(f"Team strength — Home {home_strength} · Away {away_strength}")
    if home_lambda is not None and away_lambda is not None:
        lines.append(f"xG forecast — {home_lambda}–{away_lambda}")
    lines.append(f"Form checks passed: {score}/{max_score}")
    return lines


def format_pick_block(idx, home, away, date, summary, extra_lines=None):
    """One pick with consistent spacing for free and VIP reports."""
    lines = [
        f"  {idx}. {home} vs {away} ({date})",
        f"     {summary}",
    ]
    if extra_lines:
        for line in extra_lines:
            lines.append(f"     {line}")
    lines.append("")
    return lines


def calculate_performance(history_list, days=30): 
    """Calculate performance metrics for last N days.""" 
    cutoff = datetime.now() - timedelta(days=days) 
    stats = { 
        "wins": 0, 
        "losses": 0, 
        "pushes": 0, 
        "pending": 0, 
        "win_rate": 0.0, 
        "total_decisions": 0 
    } 
    
    for pick in history_list: 
        pick_date = datetime.fromisoformat(pick["date"]) 
        if pick_date >= cutoff: 
            if pick["result"] == "win": 
                stats["wins"] += 1 
                stats["total_decisions"] += 1 
            elif pick["result"] == "loss": 
                stats["losses"] += 1 
                stats["total_decisions"] += 1 
            elif pick["result"] == "push": 
                stats["pushes"] += 1 
            else: 
                stats["pending"] += 1 
    
    if stats["total_decisions"] > 0: 
        stats["win_rate"] = (stats["wins"] / stats["total_decisions"]) * 100 
    
    return stats


def calculate_performance_for_month(picks, month_start, month_end): 
    """Calculate performance for a specific month.""" 
    stats = { 
        "total": 0, 
        "wins": 0, 
        "losses": 0, 
        "pushes": 0, 
        "pending": 0, 
        "decisions": 0, 
        "win_rate": 0.0 
    } 
    
    for pick in picks: 
        # Handle both date-only (YYYY-MM-DD) and full ISO strings
        date_str = pick["date"]
        if len(date_str) == 10:
            pick_date = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            pick_date = datetime.fromisoformat(date_str)
        if month_start <= pick_date < month_end: 
            stats["total"] += 1 
            result = resolve_pick_result(pick)
            if result == "win": 
                stats["wins"] += 1 
                stats["decisions"] += 1 
            elif result == "loss": 
                stats["losses"] += 1 
                stats["decisions"] += 1 
            elif result == "push": 
                stats["pushes"] += 1 
            elif count_report_pending(pick):
                stats["pending"] += 1 
    
    if stats["decisions"] > 0: 
        stats["win_rate"] = (stats["wins"] / stats["decisions"]) * 100 
    
    return stats


def calculate_safer_pick_stats(picks, date_filter_fn):
    """Calculate stats for safer picks (Double Chance, Over 1.5, Under 3.5)"""
    stats = {
        "double_chance": {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "decisions": 0,
            "win_rate": 0.0
        },
        "over_1_5": {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "decisions": 0,
            "win_rate": 0.0
        },
        "under_3_5": {
            "total": 0,
            "wins": 0,
            "losses": 0,
            "pending": 0,
            "decisions": 0,
            "win_rate": 0.0
        }
    }
    
    # Process Double Chance (for Home Win picks)
    for pick in picks.get("home_win", []):
        if date_filter_fn(pick):
            stats["double_chance"]["total"] += 1
            if pick["result"] == "pending":
                if not pick_is_overdue(pick):
                    continue
                stats["double_chance"]["pending"] += 1
                continue

            parsed = parse_final_score(pick.get("final_score"))
            if parsed is None:
                if pick_is_overdue(pick):
                    stats["double_chance"]["pending"] += 1
                continue

            home_score, away_score = parsed
            if home_score >= away_score:
                stats["double_chance"]["wins"] += 1
                stats["double_chance"]["decisions"] += 1
            else:
                stats["double_chance"]["losses"] += 1
                stats["double_chance"]["decisions"] += 1
    
    # Process Over 1.5 and Under 3.5 (for Over/Under picks)
    for pick in picks.get("over_under", []):
        if date_filter_fn(pick):
            prediction = pick.get("prediction", "over").lower()
            if prediction == "over":
                stats["over_1_5"]["total"] += 1
            else:
                stats["under_3_5"]["total"] += 1
            
            if pick["result"] == "pending":
                if not pick_is_overdue(pick):
                    continue
                if prediction == "over":
                    stats["over_1_5"]["pending"] += 1
                else:
                    stats["under_3_5"]["pending"] += 1
                continue

            parsed = parse_final_score(pick.get("final_score"))
            if parsed is None:
                if not pick_is_overdue(pick):
                    continue
                if prediction == "over":
                    stats["over_1_5"]["pending"] += 1
                else:
                    stats["under_3_5"]["pending"] += 1
                continue

            home_score, away_score = parsed
            total_goals = home_score + away_score
            
            if prediction == "over":
                if total_goals >= 2:
                    stats["over_1_5"]["wins"] += 1
                    stats["over_1_5"]["decisions"] += 1
                else:
                    stats["over_1_5"]["losses"] += 1
                    stats["over_1_5"]["decisions"] += 1
            else:
                if total_goals <= 3:
                    stats["under_3_5"]["wins"] += 1
                    stats["under_3_5"]["decisions"] += 1
                else:
                    stats["under_3_5"]["losses"] += 1
                    stats["under_3_5"]["decisions"] += 1
    
    # Calculate win rates
    for key in stats:
        if stats[key]["decisions"] > 0:
            stats[key]["win_rate"] = (stats[key]["wins"] / stats[key]["decisions"]) * 100
    
    return stats


def generate_monthly_report(year, month): 
    """Generate detailed monthly performance report with win/loss tracking.""" 
    history = load_history() 
    month_start = datetime(year, month, 1) 
    
    if month == 12: 
        month_end = datetime(year + 1, 1, 1) 
    else: 
        month_end = datetime(year, month + 1, 1) 
    
    # Calculate home win stats 
    hw_stats = calculate_performance_for_month(history["home_win"], month_start, month_end) 
    
    # Separate over and under picks 
    over_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "over"]
    under_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "under"]
    
    # Calculate separate over and under stats
    over_stats = calculate_performance_for_month(over_picks, month_start, month_end) 
    under_stats = calculate_performance_for_month(under_picks, month_start, month_end) 
    
    # Calculate safer pick stats
    def month_filter(pick):
        date_str = pick["date"]
        if len(date_str) == 10:
            pick_date = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            pick_date = datetime.fromisoformat(date_str)
        return month_start <= pick_date < month_end
    
    safer_stats = calculate_safer_pick_stats(history, month_filter)
    
    # Generate report text 
    month_name = month_start.strftime("%B %Y") 
    report = [] 
    report.append("MONTHLY PERFORMANCE REPORT") 
    report.append(f"{month_name}") 
    report.append("") 
    report.append("Home win picks") 
    report.append(f"Total picks: {hw_stats['total']}") 
    report.append(f"Wins: {hw_stats['wins']}") 
    report.append(f"Losses: {hw_stats['losses']}") 
    report.append(f"Pushes: {hw_stats['pushes']}") 
    report.append(f"Pending: {hw_stats['pending']}") 
    if hw_stats['decisions'] > 0: 
        report.append(f"Win Rate: {hw_stats['win_rate']:.1f}%") 
    report.append("") 
    report.append("DOUBLE CHANCE (HOME OR DRAW)") 
    report.append(f"Total picks: {safer_stats['double_chance']['total']}") 
    report.append(f"Wins: {safer_stats['double_chance']['wins']}") 
    report.append(f"Losses: {safer_stats['double_chance']['losses']}") 
    report.append(f"Pending: {safer_stats['double_chance']['pending']}") 
    if safer_stats['double_chance']['decisions'] > 0: 
        report.append(f"Win Rate: {safer_stats['double_chance']['win_rate']:.1f}%") 
    report.append("") 
    report.append("OVER 2.5 GOALS") 
    report.append(f"Total picks: {over_stats['total']}") 
    report.append(f"Wins: {over_stats['wins']}") 
    report.append(f"Losses: {over_stats['losses']}") 
    report.append(f"Pushes: {over_stats['pushes']}") 
    report.append(f"Pending: {over_stats['pending']}") 
    if over_stats['decisions'] > 0: 
        report.append(f"Win Rate: {over_stats['win_rate']:.1f}%") 
    report.append("") 
    report.append("OVER 1.5 GOALS") 
    report.append(f"Total picks: {safer_stats['over_1_5']['total']}") 
    report.append(f"Wins: {safer_stats['over_1_5']['wins']}") 
    report.append(f"Losses: {safer_stats['over_1_5']['losses']}") 
    report.append(f"Pending: {safer_stats['over_1_5']['pending']}") 
    if safer_stats['over_1_5']['decisions'] > 0: 
        report.append(f"Win Rate: {safer_stats['over_1_5']['win_rate']:.1f}%") 
    report.append("") 
    report.append("UNDER 2.5 GOALS") 
    report.append(f"Total picks: {under_stats['total']}") 
    report.append(f"Wins: {under_stats['wins']}") 
    report.append(f"Losses: {under_stats['losses']}") 
    report.append(f"Pushes: {under_stats['pushes']}") 
    report.append(f"Pending: {under_stats['pending']}") 
    if under_stats['decisions'] > 0: 
        report.append(f"Win Rate: {under_stats['win_rate']:.1f}%") 
    report.append("") 
    report.append("UNDER 3.5 GOALS") 
    report.append(f"Total picks: {safer_stats['under_3_5']['total']}") 
    report.append(f"Wins: {safer_stats['under_3_5']['wins']}") 
    report.append(f"Losses: {safer_stats['under_3_5']['losses']}") 
    report.append(f"Pending: {safer_stats['under_3_5']['pending']}") 
    if safer_stats['under_3_5']['decisions'] > 0: 
        report.append(f"Win Rate: {safer_stats['under_3_5']['win_rate']:.1f}%") 
    report.append("") 
    report.append("---") 
    report.append("For informational purposes only") 
    report.append("Gamble responsibly") 
    report.append("") 
    
    return "\n".join(report), { 
        "month": f"{year}-{month:02d}", 
        "home_win": hw_stats, 
        "double_chance": safer_stats["double_chance"],
        "over": over_stats,
        "over_1_5": safer_stats["over_1_5"],
        "under": under_stats,
        "under_3_5": safer_stats["under_3_5"]
    }


def generate_weekly_report(): 
    """Generate a weekly performance report for the last 7 days.""" 
    history = load_history() 
    week_ago = datetime.now() - timedelta(days=7) 
    today = datetime.now() 
    
    # Function to calculate stats for a date range 
    def calc_stats(picks): 
        stats = { 
            "total": 0, 
            "wins": 0, 
            "losses": 0, 
            "pushes": 0, 
            "pending": 0, 
            "decisions": 0, 
            "win_rate": 0.0, 
            "by_confidence": {} 
        } 
        
        for pick in picks: 
            # Handle both date-only (YYYY-MM-DD) and full ISO strings
            date_str = pick["date"]
            if len(date_str) == 10:
                pick_date = datetime.strptime(date_str, "%Y-%m-%d")
            else:
                pick_date = datetime.fromisoformat(date_str)
            # Normalize to date-only comparison (ignore time)
            if week_ago.date() <= pick_date.date() <= today.date(): 
                stats["total"] += 1 
                conf = pick.get("confidence", "N/A") 
                if conf not in stats["by_confidence"]: 
                    stats["by_confidence"][conf] = {"wins":0, "losses":0, "pushes":0, "pending":0, "decisions":0} 
                
                result = resolve_pick_result(pick)
                if result == "win": 
                    stats["wins"] += 1 
                    stats["decisions"] += 1 
                    stats["by_confidence"][conf]["wins"] +=1 
                    stats["by_confidence"][conf]["decisions"] +=1 
                elif result == "loss": 
                    stats["losses"] += 1 
                    stats["decisions"] +=1 
                    stats["by_confidence"][conf]["losses"] +=1 
                    stats["by_confidence"][conf]["decisions"] +=1 
                elif result == "push": 
                    stats["pushes"] +=1 
                    stats["by_confidence"][conf]["pushes"] +=1 
                elif count_report_pending(pick): 
                    stats["pending"] +=1 
                    stats["by_confidence"][conf]["pending"] +=1 
        
        if stats["decisions"] >0: 
            stats["win_rate"] = (stats["wins"] / stats["decisions"])*100 
        
        # Calculate win rate by confidence 
        for conf in stats["by_confidence"]: 
            if stats["by_confidence"][conf]["decisions"] >0: 
                stats["by_confidence"][conf]["win_rate"] = (stats["by_confidence"][conf]["wins"] / stats["by_confidence"][conf]["decisions"])*100 
        
        return stats 
    
    hw_stats = calc_stats(history["home_win"]) 
    
    # Separate over and under picks
    over_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "over"]
    under_picks = [p for p in history["over_under"] if p.get("prediction", "over").lower() == "under"]
    
    over_stats = calc_stats(over_picks) 
    under_stats = calc_stats(under_picks) 
    
    # Calculate safer pick stats
    def week_filter(pick):
        date_str = pick["date"]
        if len(date_str) == 10:
            pick_date = datetime.strptime(date_str, "%Y-%m-%d")
        else:
            pick_date = datetime.fromisoformat(date_str)
        return week_ago.date() <= pick_date.date() <= today.date()
    
    safer_stats = calculate_safer_pick_stats(history, week_filter)
    
    report = [] 
    report.append("WEEKLY PERFORMANCE REPORT") 
    report.append(f"{week_ago.strftime('%Y-%m-%d')} to {today.strftime('%Y-%m-%d')}") 
    report.append("") 
    report.append("Home win picks") 
    report.append(f"Total picks: {hw_stats['total']}") 
    report.append(f"Wins: {hw_stats['wins']}") 
    report.append(f"Losses: {hw_stats['losses']}") 
    report.append(f"Pushes: {hw_stats['pushes']}") 
    report.append(f"Pending: {hw_stats['pending']}") 
    if hw_stats['decisions']>0: 
        report.append(f"Win rate: {hw_stats['win_rate']:.1f}%") 
    
    settled_hw = [ 
        (conf, cs) for conf, cs in sorted(hw_stats['by_confidence'].items()) 
        if cs['decisions'] > 0 
    ] 
    if settled_hw: 
        report.append("") 
        report.append("Performance by confidence:") 
        for conf, cs in settled_hw: 
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)") 
    
    report.append("") 
    report.append("DOUBLE CHANCE (HOME OR DRAW)") 
    report.append(f"Total picks: {safer_stats['double_chance']['total']}") 
    report.append(f"Wins: {safer_stats['double_chance']['wins']}") 
    report.append(f"Losses: {safer_stats['double_chance']['losses']}") 
    report.append(f"Pending: {safer_stats['double_chance']['pending']}") 
    if safer_stats['double_chance']['decisions']>0: 
        report.append(f"Win rate: {safer_stats['double_chance']['win_rate']:.1f}%") 
    
    report.append("") 
    report.append("OVER 2.5 GOALS") 
    report.append(f"Total picks: {over_stats['total']}") 
    report.append(f"Wins: {over_stats['wins']}") 
    report.append(f"Losses: {over_stats['losses']}") 
    report.append(f"Pushes: {over_stats['pushes']}") 
    report.append(f"Pending: {over_stats['pending']}") 
    if over_stats['decisions']>0: 
        report.append(f"Win rate: {over_stats['win_rate']:.1f}%") 
    
    settled_over = [ 
        (conf, cs) for conf, cs in sorted(over_stats['by_confidence'].items()) 
        if cs['decisions'] > 0 
    ] 
    if settled_over: 
        report.append("") 
        report.append("Performance by confidence:") 
        for conf, cs in settled_over: 
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)") 
    
    report.append("") 
    report.append("OVER 1.5 GOALS") 
    report.append(f"Total picks: {safer_stats['over_1_5']['total']}") 
    report.append(f"Wins: {safer_stats['over_1_5']['wins']}") 
    report.append(f"Losses: {safer_stats['over_1_5']['losses']}") 
    report.append(f"Pending: {safer_stats['over_1_5']['pending']}") 
    if safer_stats['over_1_5']['decisions']>0: 
        report.append(f"Win rate: {safer_stats['over_1_5']['win_rate']:.1f}%") 
    
    report.append("") 
    report.append("UNDER 2.5 GOALS") 
    report.append(f"Total picks: {under_stats['total']}") 
    report.append(f"Wins: {under_stats['wins']}") 
    report.append(f"Losses: {under_stats['losses']}") 
    report.append(f"Pushes: {under_stats['pushes']}") 
    report.append(f"Pending: {under_stats['pending']}") 
    if under_stats['decisions']>0: 
        report.append(f"Win rate: {under_stats['win_rate']:.1f}%") 
    
    settled_under = [ 
        (conf, cs) for conf, cs in sorted(under_stats['by_confidence'].items()) 
        if cs['decisions'] > 0 
    ] 
    if settled_under: 
        report.append("") 
        report.append("Performance by confidence:") 
        for conf, cs in settled_under: 
            report.append(f"  {conf}: {cs['wins']}/{cs['decisions']} wins ({cs['win_rate']:.1f}%)") 
    
    report.append("") 
    report.append("UNDER 3.5 GOALS") 
    report.append(f"Total picks: {safer_stats['under_3_5']['total']}") 
    report.append(f"Wins: {safer_stats['under_3_5']['wins']}") 
    report.append(f"Losses: {safer_stats['under_3_5']['losses']}") 
    report.append(f"Pending: {safer_stats['under_3_5']['pending']}") 
    if safer_stats['under_3_5']['decisions']>0: 
        report.append(f"Win rate: {safer_stats['under_3_5']['win_rate']:.1f}%") 
    
    report.append("") 
    report.append("---") 
    report.append("For informational purposes only") 
    report.append("Gamble responsibly") 
    
    return "\n".join(report), {
        "home_win": hw_stats, 
        "double_chance": safer_stats["double_chance"],
        "over": over_stats, 
        "over_1_5": safer_stats["over_1_5"],
        "under": under_stats,
        "under_3_5": safer_stats["under_3_5"]
    }

 
def main(): 
    print_summary()

if __name__ == "__main__": 
    main()
