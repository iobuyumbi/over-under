# Yesterday's Results (2026-08-24): Analysis & New Rules

## The scoreboard, broken out by market

| Market | Record | Notes |
|---|---|---|
| HOME WIN | 3W–1L | Solid. The 1 loss was a 2-2 draw (home scored twice, didn't lose). |
| OVER 2.5 | 0W–4L | All 4 losing matches finished with **1 total goal** (1-0, 0-1, 0-1) or 2 total (1-1). |
| UNDER 2.5 | 1W–1L | Sample too small (n=2) to draw a real conclusion. |
| BTTS YES | 1W–4L | All 4 losses had **one side blanked** (0-1, 1-0, 3-0, 0-1). |

**5W-10L overall.** Before anything else: 15 picks is a small sample, and a bad day like this is well within normal variance for a model that (per the retrospective-analysis tooling already in the repo) is presumably tuned for a long-run edge, not a 100% daily hit rate. I'm not treating this as "the model is broken" — but there IS a real, specific, identifiable pattern in the losses, not just noise, and it's the same pattern showing up in two different markets.

## The pattern

Three matches were picked for **both** Over 2.5 **and** BTTS Yes on the same day, and all three lost both bets, finishing 1-0, 0-1, and 0-1:

- Botev Vratsa vs Botev Plovdiv (1-0)
- O'Higgins vs Palestino (0-1)
- Loko. Plovdiv vs Arda (0-1)

Both markets were betting on "goals," and both were wrong on the same matches for what's likely the same underlying reason: the model read these teams' recent scoring histories as active/high-tempo, but on the day, exactly one team scored exactly once and the game stayed tight. That's not two independent misses — it's one misread of match tempo showing up twice.

I don't have access to what the actual scraped form data looked like for these specific matches (I can't refetch Soccerbase from this sandbox — no network access), so I can't tell you the exact numbers that let them through. But I could inspect the rule code directly, and found two concrete, provable gaps that this exact failure pattern would slip through:

## What I found and fixed in the code

**1. Over 2.5's "last match" check was asymmetric.** The algorithm already required the *away* team's single most recent match to have ≥2 total goals — but had no equivalent check for the *home* team. A home side coming off a near-goalless match could still qualify on Over 2.5 as long as its other numbers averaged out. Fixed by adding the missing "Home last match goals" and "Home scored (last 3)" checks, mirroring what Away already had.

**2. Both Over 2.5 and BTTS Yes only vetoed on *pairs* of recent games, never a single one.** The existing "drought"/"wall"/"low event" vetoes all require *both* of a team's last 2 matches to look quiet before blocking. That means a team's literal most recent match — a 1-0 or 0-1 scoreline, the exact shape of yesterday's losses — can get diluted out and ignored if the game before it happened to be higher-scoring. A single very recent near-goalless match is a stronger live signal than a 2-game average, and it wasn't able to veto on its own. Added:
  - `_recent_goalless_shock_veto` (Over 2.5): blocks if either team's most recent venue match had ≤1 total goal.
  - `_recent_shutout_shock_veto` (BTTS Yes): blocks if either team's most recent venue match saw them fail to score or keep a clean sheet.

**3. Home Win: a smaller, lower-priority gap.** The one loss was a fair 2-2 draw, and Home Win's existing checks (away losses required, capped away goal *volume*, high away goals-against required) are already fairly thorough. But those checks cap total goal volume, not scoring *consistency* — a team that nets one goal in most of its matches (rather than a big score in one match) can pass a low-volume cap while still being a persistent, live scoring threat that produces draws against a home side that also scores steadily. Added `_draw_risk_veto`: fires only when the away side scored in ≥4 of its last 6 matches AND the home defense isn't already close to airtight (allowing this to not over-trigger against strong home sides). Because Home Win already performs well, this is a narrower, more conservative addition than the other two — I didn't want to over-correct a market that isn't broken.

All three new rules are implemented, tested against synthetic scenarios matching yesterday's exact failure shapes, and verified to not break existing behavior (all three engines still import and run cleanly — see `IMPROVEMENT_REPORT.md` from the earlier pass for the full verification method, since I don't have network access in this sandbox to run them against live Soccerbase data).

## What I'd watch for, honestly

- These rules make the model **stricter**, which means fewer qualifying picks, not just better ones. If pick volume drops sharply after this, that's expected — the trade-off is deliberate.
- I can't backtest these against historical data from here (no network access to pull past Soccerbase results), so I can't give you a "this would have turned yesterday into 8W-7L" number. The honest next step is to run these updated engines for a week or two and see whether the specific failure mode (single-goal, one-sided matches slipping through) actually shows up less often.
- Resist the urge to add another rule after the *next* bad day too. Three tight vetoes targeting one well-evidenced pattern is a reasonable response to yesterday. A rule added after every losing day risks overfitting to noise rather than fixing anything real — the existing 10+ veto functions already in this codebase suggest that temptation has been given in to before.

## Also done this session: further modularization

Beyond the rule changes, split out a second shared module, `scraping.py`, covering the actual Soccerbase HTML scraping/parsing (`fetch_soccerbase_fixtures`, `fetch_soccerbase_team_results`, `get_team_form`, `get_team_overall_form`, `_thin_count`/`_thin_total`) that was still duplicated across all three engines even after last session's `utils.py` consolidation.

One nuance worth knowing: `home_win_soccerbase.py`'s `get_team_form`/`get_team_overall_form` return full match dicts (it reads `match["result"]`), while `over25`/`btts`'s return `(gf, ga)` tuples. I did **not** force these into one shared function — that would've silently changed home_win's behavior. Only the underlying HTML scraping (identical shape either way) was shared for all three.

Final sizes:

| File | Original | After both passes |
|---|---|---|
| `over25_soccerbase.py` | 2265 | 2034 |
| `home_win_soccerbase.py` | 1271 | 1085 |
| `btts_soccerbase.py` | 1662 | 1485 |

All three verified to still import and run correctly after every change (stubbed `fake_useragent` locally since this sandbox has no network access to install it, but everything downstream of that import was exercised directly).
