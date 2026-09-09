# SYMMETRY PATCH — Defence Gate for All Predictors

## Core Principle

> **What helps "plus goals" is the inverse of what helps "minus goals".**

| Market | Team A needs... | Team B needs... |
|--------|----------------|----------------|
| **Team Goals O0.5** | Scoring streak | Opponent concedes frequently |
| **BTTS Yes** | Scoring streak + opponent leaks | Scoring streak + opponent leaks |
| **O2.5** | High scoring + opponent leaks | High scoring + opponent leaks |
| **BTTS No** | Clean sheets + opponent toothless | Clean sheets + opponent toothless |
| **U2.5** | Low scoring + opponent tight | Low scoring + opponent tight |
| **Home Win** | Strong home attack | Away defence leaks on road |

The defence gate is **symmetric**:
- **Leaky mode** (`mode="leaky"`) → for plus-goal markets (O0.5, BTTS Yes, O2.5, Home Win)
- **Tight mode** (`mode="tight"`) → for minus-goal markets (BTTS No, U2.5)

---

## How to Apply to BTTS (btts_soccerbase.py)

### BTTS Yes — BOTH opponents must be leaky

```python
from defence_gate import check_both_teams_concession

# In process_single_match(), after fetching forms:
btts_yes_gate, btts_details = check_both_teams_concession(
    away_form_6, away_overall_10,   # opponent of home team = away team
    home_form_6, home_overall_10,   # opponent of away team = home team
    min_venue="4/5",
    min_overall="8/10"
)

if not btts_yes_gate:
    # VETO BTTS Yes — at least one opponent is too defensively solid
    btts_yes_qualifies = False
```

### BTTS No — BOTH opponents must be tight

```python
from defence_gate import check_both_teams_defence

btts_no_gate, btts_no_details = check_both_teams_defence(
    away_form_6, away_overall_10,
    home_form_6, home_overall_10,
    min_venue="3/5",      # opponent kept CS in >= 3 of last 5 venue
    min_overall="3/10"    # opponent kept CS in >= 3 of last 10 overall
)

if not btts_no_gate:
    # VETO BTTS No — at least one opponent leaks too much
    btts_no_qualifies = False
```

---

## How to Apply to Over/Under 2.5 (over25_soccerbase.py)

### Over 2.5 — Combined opponent concession must be high

```python
from defence_gate import check_combined_concession

o25_gate, o25_details = check_combined_concession(
    away_form_6, away_overall_10,   # home team's opponent
    home_form_6, home_overall_10,   # away team's opponent
    min_total_venue=7,      # combined 7+ concessions in last 5 venue each
    min_total_overall=14    # combined 14+ concessions in last 10 overall each
)

if not o25_gate:
    # VETO Over 2.5 — defences are too solid combined
    over25_qualifies = False
```

### Under 2.5 — Combined opponent clean sheets must be high

```python
from defence_gate import check_combined_defence

u25_gate, u25_details = check_combined_defence(
    away_form_6, away_overall_10,
    home_form_6, home_overall_10,
    min_total_venue=3,      # combined 3+ clean sheets in last 5 venue each
    min_total_overall=6     # combined 6+ clean sheets in last 10 overall each
)

if not u25_gate:
    # VETO Under 2.5 — at least one defence leaks too much
    under25_qualifies = False
```

---

## How to Apply to Home Win (home_win_soccerbase.py)

### Home Win — Away opponent (home team) must leak at home

```python
from defence_gate import DefenceGate

gate = DefenceGate(min_venue="4/5", min_overall="8/10", mode="leaky")
# Home team is the "opponent" of the away team
away_perspective = gate.passes(home_form_6, home_overall_10)

if not away_perspective:
    # Home team doesn't concede enough at home → away team can't exploit
    # But home win is about HOME scoring, not away scoring...
    # Actually for Home Win, we need:
    #   Home team scores + Away team concedes on the road
    home_gate = DefenceGate(min_venue="4/5", min_overall="8/10", mode="leaky")
    away_defence_leaky = home_gate.passes(away_form_6, away_overall_10)

    if not away_defence_leaky:
        # Away team doesn't concede on the road → home win harder
        home_win_confidence_penalty = 0.85
```

---

## Threshold Reference Table

| Market | Gate Type | Venue Threshold | Overall Threshold | Mode |
|--------|-----------|-----------------|-------------------|------|
| Team Goals O0.5 | Single opponent | 4/5 conceded | 8/10 conceded | leaky |
| BTTS Yes | Both opponents | 4/5 each | 8/10 each | leaky |
| BTTS No | Both opponents | 3/5 CS each | 3/10 CS each | tight |
| O2.5 | Combined | 7/10 total | 14/20 total | leaky |
| U2.5 | Combined | 3/10 CS total | 6/20 CS total | tight |
| Home Win | Away opponent | 4/5 away CS | 8/10 away CS | leaky |

---

## Why This Works

**Before (no defence gate):**
- Team A: scored in 10 straight games → PICK
- **Problem**: Team B kept 4 clean sheets in last 5 → Team A likely shut out → LOSS

**After (with defence gate):**
- Team A: scored in 10 straight games → candidate
- Team B: conceded in 4/5 venue games → defence gate PASSES → PICK
- Team B: kept 4 clean sheets in last 5 → defence gate FAILS → VETO

The defence gate filters out **false positives** where a scoring streak meets a defensive wall.

---

## Files

- **[defence_gate.py](sandbox:///mnt/agents/output/defence_gate.py)** — Reusable module
- **[oo05_soccerbase_v2_2.py](sandbox:///mnt/agents/output/oo05_soccerbase_v2_2.py)** — Already integrated
