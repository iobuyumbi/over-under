#!/usr/bin/env python3
import argparse
import json
import re
from collections import defaultdict


SETTLED_RESULTS = frozenset({"win", "loss", "push"})


def _normalize_result(value):
    value = str(value or "").strip().lower()
    return value if value in SETTLED_RESULTS else "pending"


def _new_counter():
    return {"win": 0, "loss": 0, "push": 0, "pending": 0, "total": 0}


def _add(counter, result):
    bucket = _normalize_result(result)
    counter[bucket] += 1
    counter["total"] += 1


def _summarize(entries, key_fn):
    stats = defaultdict(_new_counter)
    for pick in entries:
        _add(stats[key_fn(pick)], pick.get("result"))
    return stats


def _settled(counter):
    return counter["win"] + counter["loss"] + counter["push"]


def _win_rate(counter):
    settled = _settled(counter)
    if not settled:
        return 0.0
    return counter["win"] / settled * 100.0


def _format_row(label, counter):
    settled = _settled(counter)
    return (
        f"{label}: total={counter['total']} settled={settled} "
        f"win={counter['win']} loss={counter['loss']} push={counter['push']} "
        f"pending={counter['pending']} win_rate={_win_rate(counter):.1f}%"
    )


def _print_section(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _print_top(stats, *, header, min_settled=1, limit=20):
    rows = []
    for label, counter in stats.items():
        settled = _settled(counter)
        if settled < min_settled:
            continue
        rows.append((settled, _win_rate(counter), str(label), counter))
    rows.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    print("\n" + header)
    for _, __, label, counter in rows[:limit]:
        print("  " + _format_row(label, counter))


def _print_bottom(stats, *, header, min_settled=1, limit=20):
    rows = []
    for label, counter in stats.items():
        settled = _settled(counter)
        if settled < min_settled:
            continue
        rows.append((settled, _win_rate(counter), str(label), counter))
    rows.sort(key=lambda item: (item[1], -item[0], item[2]))
    print("\n" + header)
    for _, __, label, counter in rows[:limit]:
        print("  " + _format_row(label, counter))


def _filter_stats(stats, pattern):
    regex = re.compile(pattern, re.IGNORECASE)
    return {k: v for k, v in stats.items() if regex.search(str(k))}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", default="prediction_history.json")
    parser.add_argument("--min-settled", type=int, default=5)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    with open(args.history, "r", encoding="utf-8") as f:
        history = json.load(f)

    for section in ("home_win", "over_under"):
        picks = list(history.get(section, []) or [])
        _print_section(f"{section} entries: {len(picks)}")

        overall = _summarize(picks, key_fn=lambda p: "ALL")
        _print_top(overall, header="Overall", min_settled=1, limit=1)

        by_confidence = _summarize(picks, key_fn=lambda p: p.get("confidence", "N/A"))
        _print_top(
            by_confidence,
            header="By confidence (sorted by volume)",
            min_settled=args.min_settled,
            limit=args.top,
        )

        by_league = _summarize(picks, key_fn=lambda p: p.get("league", "N/A"))
        _print_top(
            by_league,
            header="By league (sorted by volume)",
            min_settled=args.min_settled,
            limit=args.top,
        )
        _print_bottom(
            by_league,
            header="Worst leagues (sorted by win rate)",
            min_settled=args.min_settled,
            limit=args.top,
        )

        by_league_confidence = _summarize(
            picks,
            key_fn=lambda p: f"{p.get('league','N/A')}|{p.get('confidence','N/A')}",
        )
        _print_top(
            by_league_confidence,
            header="By league + confidence (sorted by volume)",
            min_settled=args.min_settled,
            limit=args.top,
        )

        argentina = _filter_stats(by_league, r"argentina|primera nacional")
        if argentina:
            _print_top(
                argentina,
                header="Argentina filtered",
                min_settled=1,
                limit=args.top,
            )

        sweden = _filter_stats(by_league, r"swed|superettan|allsvens")
        if sweden:
            _print_top(
                sweden,
                header="Sweden filtered",
                min_settled=1,
                limit=args.top,
            )

        if section == "over_under":
            by_prediction = _summarize(picks, key_fn=lambda p: p.get("prediction", "N/A"))
            _print_top(
                by_prediction,
                header="By market (over vs under)",
                min_settled=args.min_settled,
                limit=args.top,
            )

            by_pred_conf = _summarize(
                picks,
                key_fn=lambda p: f"{p.get('prediction','N/A')}|{p.get('confidence','N/A')}",
            )
            _print_top(
                by_pred_conf,
                header="By market + confidence",
                min_settled=args.min_settled,
                limit=args.top,
            )

            by_league_market = _summarize(
                picks,
                key_fn=lambda p: f"{p.get('league','N/A')}|{p.get('prediction','N/A')}",
            )
            _print_top(
                by_league_market,
                header="By league + market (sorted by volume)",
                min_settled=args.min_settled,
                limit=args.top,
            )

            by_league_market_conf = _summarize(
                picks,
                key_fn=lambda p: (
                    f"{p.get('league','N/A')}|{p.get('prediction','N/A')}|{p.get('confidence','N/A')}"
                ),
            )
            _print_top(
                by_league_market_conf,
                header="By league + market + confidence (sorted by volume)",
                min_settled=args.min_settled,
                limit=args.top,
            )


if __name__ == "__main__":
    main()
