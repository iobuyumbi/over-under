# Research: How Forebet, Statarea, PredictZ & Over25Tips Build Their Tips

Researched via web search on 2026-08-27 (I can't fetch these sites' live pages
directly — Soccerbase-style scraping isn't set up for them, and general web
access in this environment is search-snippet-based, not full page loads — so
this is built from their own public "how it works" pages, third-party reviews,
and one leaked page of over25tips.com's actual rule text). None of these sites
publish full source code or a formal backtest, so treat this as directional,
not a spec to copy verbatim.

## What each site actually does, as far as it's public

**Forebet** — probability engine outputs Home/Draw/Away percentages that sum
to 100%, built from historical results, expected goals (xG), and home/away
form splits, with the model recalibrated over time (they describe it as
adapting via machine learning, though the exact architecture isn't
disclosed). BTTS is driven by each team's scoring/conceding frequency and
"goal timing" patterns. Over/Under is driven by scoring averages, pace of
play, and finishing/conversion rates rather than raw goal totals alone. They
publish a visible green/red track record so users can audit hit rate over
time — a transparency feature this project's `prediction_tracker.py` /
`generate_weekly_report.py` already does internally.

**Statarea** — explicitly folds **bookmaker odds ("coefficients")** in as an
input alongside form, H2H, and league standings. Their "Custom Prediction"
tool lets a user pick which of these factors to weight. This is the clearest
signal across all four sites that market prices are treated as a legitimate
input, not just a staking afterthought.

**PredictZ** — for BTTS specifically, they state a named heuristic: teams
concede more away from home on average, so **match-winner favoritism
matters for BTTS, not just scoring form** — an away side that's favored to
win is also more likely to leak goals, making BTTS more probable in games
where the "stronger" side isn't at home. This is a distinct signal from raw
scoring/conceding averages.

**Over25tips.com** — this is the interesting one: their exact published
ruleset for Over 2.5 is genuinely public (found via a cached page):

```
H1 - Home team must have had 7+ goals in last 3 home matches
H2 - 2 or 3 of the last 3 home matches ended over 2.5
A1 - Away team must have had 7+ goals in last 3 away matches
A2 - The away team's previous match had 2+ total goals
A3 - Away team scored in 2 or 3 of last 3 matches
A4 - 2 or 3 of the last 3 away matches ended over 2.5
```

**This project already deliberately mirrors this** — the `_o25tips_*`
prefixed functions and `R11`-`R14` rule labels in `btts_soccerbase.py`
(favourite-context scoring off the home/away lambda ratio) are clearly
built with over25tips.com's methodology as a direct reference, and
`apply_over_algorithm()` in `over25_soccerbase.py` implements essentially
the same H1/H2/A1-A4 checks under different names. That's a good sign —
you're not starting from scratch against these sites, you're already
running a close variant of one of them.

One notable finding: **over25tips.com's own public rules have the exact
same asymmetry** I found and fixed last session — A2 checks the *away*
team's previous match for 2+ goals, with no published H-side equivalent.
So the "Home last match goals" fix added to `over25_soccerbase.py` isn't
just closing a copy-paste gap in this codebase — it's a genuine improvement
*over* the public rules this project is modeled on, not just a restoration
of parity with them.

A separate site, MyGameOdds, was also useful for one design detail even
though it's not one of the four requested: they require a tip to clear
**two independent checks** — their model must show ≥57.5% probability
*and* a real bookmaker must price the outcome at 1.20+ odds (i.e. the
market must not already be pricing in the same certainty). That's a
market-price sanity check layered on top of a model probability, distinct
from either alone.

## The one clear gap this research surfaces

**None of this project's three engines use live, per-match bookmaker odds
as an input signal or a validation gate.** Checked directly in the code:
the `--odds` / `--odds-over` / `--odds-under` CLI flags in all three files
are a single flat number applied to every pick that day, used only for
Kelly stake-sizing math — never scraped per-fixture, never used to
sanity-check whether a pick is already fully priced in by the market.

Every site researched here treats market price as at least a secondary
signal: Statarea folds it directly into its model, PredictZ uses
market-implied favoritism as a BTTS heuristic, MyGameOdds uses it as a
hard confirmation gate. This project currently has no equivalent at all —
it's a pure form/H2H rule system with zero market cross-check, which is a
meaningfully different design point, not necessarily a *worse* one (rule
systems avoid the "was the market actually wrong or just unlucky"
ambiguity), but it does mean the project currently can't tell the
difference between "our model found a real edge" and "our model agrees
with what the market already knows."

### What I added — inert, opt-in, not wired up blind

I added `implied_probability()` and `value_gate_passes()` to `utils.py`.
`value_gate_passes(model_prob_pct, decimal_odds, min_edge_pct)` mirrors
MyGameOdds' two-check pattern: pass a model probability and a real market
price, and it tells you whether the model actually clears the market's
implied probability by your required margin. **It's a no-op today** —
returns `True` (doesn't block anything) whenever `decimal_odds` is `None`,
which is always, since nothing currently supplies live per-match odds. I
did not wire this into any of the three engines' qualification logic,
and did not build an odds scraper, because:

1. I have no network access in this sandbox to find, evaluate, or test
   against a real odds source (Soccerbase itself may or may not expose
   match odds on the fixture pages already being scraped — worth checking
   directly rather than guessing).
2. Wiring in an odds-based gate untested, against synthetic data, risks
   silently changing which picks qualify in ways I can't verify here.

**If you want to pursue this**, the natural next step is checking whether
the fixture pages `fetch_soccerbase_fixtures()` already pulls from
(`soccerbase.com/matches/results.sd`) include odds columns you're not
currently parsing — that would mean no new source is needed, just
extending the existing scraper in `scraping.py` to capture an odds field
per fixture, then passing it through to `value_gate_passes()` in each
engine as an additional (currently-absent) qualification check.

## Smaller techniques worth considering, lower priority

- **Forebet's "pace of play" framing for Over/Under** — this project's
  goal-total averages implicitly capture pace, but not directly. Low
  priority; the existing rule set already correlates with this.
- **PredictZ's favorite-side-leaks-more-away framing for BTTS** — this is
  close to what `_o25tips_match_points()`'s R11-R14 lambda-ratio rules
  already do (rewarding a moderately-favored away side for BTTS), so this
  project already has a version of this specific insight.
- **Forebet's public track record** — already covered by this project's
  own `prediction_tracker.py` / weekly-report tooling; no action needed.

## Bottom line

This project isn't behind these sites methodologically — it's already
running a documented variant of over25tips.com's public rule set, with
more veto layers (H2H bogey checks, drought/wall vetoes, the two new
shock vetoes from the last session) than what over25tips.com discloses
publicly. The one structural gap that stood out across all four sites is
the absence of any market-price signal. That's a bigger, riskier change
than another rule tweak, which is why I've only added the (currently
inert) building block for it rather than guessing my way into wiring it
live.
