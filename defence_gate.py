#!/usr/bin/env python3
"""
DEFENCE GATE — Reusable Opponent Concession Checker
=====================================================
Core principle: A team's scoring ability is only valuable if their
opponent actually concedes goals. Symmetrically, a team's defensive
strength is only valuable if their opponent actually struggles to score.

This module provides HARD gates (not soft checks) for opponent
concession patterns. It can be reused across ALL predictors:
  • Team Goals O0.5 — opponent must concede frequently
  • BTTS Yes — BOTH opponents must concede frequently
  • BTTS No — BOTH opponents must NOT concede frequently
  • Over 2.5 — combined opponent concession must be high
  • Under 2.5 — combined opponent concession must be low
  • Home Win — away opponent must concede on the road

Usage:
    from defence_gate import check_opponent_concession, DefenceGate

    # For Team Goals O0.5
    gate = DefenceGate(min_venue="4/5", min_overall="8/10")
    if not gate.passes(opp_venue_6, opp_overall_10):
        veto = True

    # For BTTS Yes (both sides)
    gate = DefenceGate(min_venue="4/5", min_overall="8/10")
    home_opp_ok = gate.passes(away_venue_6, away_overall_10)
    away_opp_ok = gate.passes(home_venue_6, home_overall_10)
    btts_yes_qualifies = home_opp_ok and away_opp_ok

    # For U2.5 (inverse — opponents must NOT concede)
    gate = DefenceGate(min_venue="1/5", min_overall="2/10", mode="tight")
    if gate.passes(opp_venue_6, opp_overall_10):
        # Opponent is defensively solid — supports U2.5
        pass
"""

from dataclasses import dataclass
from typing import List, Tuple, Optional


@dataclass
class DefenceGate:
    """Configurable opponent concession gate.

    min_venue: str like "4/5" meaning conceded in >= 4 of last 5 venue games
    min_overall: str like "8/10" meaning conceded in >= 8 of last 10 overall games
    mode: "leaky" (for plus-goal markets) or "tight" (for minus-goal markets)
    """
    min_venue: str = "4/5"
    min_overall: str = "8/10"
    mode: str = "leaky"  # "leaky" = opponent must concede lots, "tight" = opponent must NOT concede lots

    def __post_init__(self):
        self.venue_need, self.venue_of = self._parse_fraction(self.min_venue)
        self.overall_need, self.overall_of = self._parse_fraction(self.min_overall)

    @staticmethod
    def _parse_fraction(frac: str) -> Tuple[int, int]:
        need, of = frac.split("/")
        return int(need.strip()), int(of.strip())

    def _check_sample(self, form, need: int, of: int, check_func) -> Tuple[bool, int, int]:
        """Check if need/of condition is met in form."""
        sample = (form or [])[:of]
        if len(sample) < min(3, of):
            return False, 0, len(sample)
        count = sum(1 for item in sample if check_func(item))
        return count >= need, count, len(sample)

    def passes(self, opponent_venue_form, opponent_overall_form) -> bool:
        """Return True if opponent meets the concession gate."""
        if self.mode == "leaky":
            # For plus-goal markets: opponent must concede frequently
            venue_pass, v_count, v_n = self._check_sample(
                opponent_venue_form, self.venue_need, self.venue_of,
                lambda item: item[1] >= 1  # conceded >= 1 goal
            )
            overall_pass, o_count, o_n = self._check_sample(
                opponent_overall_form, self.overall_need, self.overall_of,
                lambda item: item[1] >= 1  # conceded >= 1 goal
            )
            # Pass if EITHER venue OR overall gate is met (venue is primary)
            return venue_pass or overall_pass

        elif self.mode == "tight":
            # For minus-goal markets: opponent must NOT concede frequently
            venue_pass, v_count, v_n = self._check_sample(
                opponent_venue_form, self.venue_need, self.venue_of,
                lambda item: item[1] == 0  # clean sheet
            )
            overall_pass, o_count, o_n = self._check_sample(
                opponent_overall_form, self.overall_need, self.overall_of,
                lambda item: item[1] == 0  # clean sheet
            )
            # Pass if BOTH venue AND overall gates are met (strict for defence)
            return venue_pass and overall_pass

        return False

    def describe(self, opponent_venue_form, opponent_overall_form) -> dict:
        """Return detailed breakdown of the gate check."""
        if self.mode == "leaky":
            v_pass, v_count, v_n = self._check_sample(
                opponent_venue_form, self.venue_need, self.venue_of,
                lambda item: item[1] >= 1
            )
            o_pass, o_count, o_n = self._check_sample(
                opponent_overall_form, self.overall_need, self.overall_of,
                lambda item: item[1] >= 1
            )
            return {
                "passed": v_pass or o_pass,
                "venue": {"need": self.venue_need, "of": self.venue_of, "actual": v_count, "sample": v_n, "pass": v_pass},
                "overall": {"need": self.overall_need, "of": self.overall_of, "actual": o_count, "sample": o_n, "pass": o_pass},
                "mode": self.mode,
            }
        else:
            v_pass, v_count, v_n = self._check_sample(
                opponent_venue_form, self.venue_need, self.venue_of,
                lambda item: item[1] == 0
            )
            o_pass, o_count, o_n = self._check_sample(
                opponent_overall_form, self.overall_need, self.overall_of,
                lambda item: item[1] == 0
            )
            return {
                "passed": v_pass and o_pass,
                "venue": {"need": self.venue_need, "of": self.venue_of, "actual": v_count, "sample": v_n, "pass": v_pass},
                "overall": {"need": self.overall_need, "of": self.overall_of, "actual": o_count, "sample": o_n, "pass": o_pass},
                "mode": self.mode,
            }


# Pre-configured gates for common markets
LEAKY_GATE_45_810 = DefenceGate(min_venue="4/5", min_overall="8/10", mode="leaky")
LEAKY_GATE_55_1010 = DefenceGate(min_venue="5/5", min_overall="10/10", mode="leaky")
TIGHT_GATE_35_310 = DefenceGate(min_venue="3/5", min_overall="3/10", mode="tight")
TIGHT_GATE_45_510 = DefenceGate(min_venue="4/5", min_overall="5/10", mode="tight")


def check_opponent_concession(opponent_venue_form, opponent_overall_form,
                               min_venue="4/5", min_overall="8/10", mode="leaky") -> bool:
    """One-shot function for quick checks."""
    gate = DefenceGate(min_venue=min_venue, min_overall=min_overall, mode=mode)
    return gate.passes(opponent_venue_form, opponent_overall_form)


def check_both_teams_concession(home_opp_venue, home_opp_overall,
                                 away_opp_venue, away_opp_overall,
                                 min_venue="4/5", min_overall="8/10") -> Tuple[bool, dict]:
    """For BTTS Yes: both opponents must be leaky."""
    gate = DefenceGate(min_venue=min_venue, min_overall=min_overall, mode="leaky")
    home_ok = gate.passes(home_opp_venue, home_opp_overall)
    away_ok = gate.passes(away_opp_venue, away_opp_overall)

    details = {
        "home_opponent": gate.describe(home_opp_venue, home_opp_overall),
        "away_opponent": gate.describe(away_opp_venue, away_opp_overall),
    }
    return home_ok and away_ok, details


def check_both_teams_defence(home_opp_venue, home_opp_overall,
                              away_opp_venue, away_opp_overall,
                              min_venue="3/5", min_overall="3/10") -> Tuple[bool, dict]:
    """For BTTS No / U2.5: both opponents must be tight (not conceding)."""
    gate = DefenceGate(min_venue=min_venue, min_overall=min_overall, mode="tight")
    home_ok = gate.passes(home_opp_venue, home_opp_overall)
    away_ok = gate.passes(away_opp_venue, away_opp_overall)

    details = {
        "home_opponent": gate.describe(home_opp_venue, home_opp_overall),
        "away_opponent": gate.describe(away_opp_venue, away_opp_overall),
    }
    return home_ok and away_ok, details


def check_combined_concession(home_opp_venue, home_opp_overall,
                               away_opp_venue, away_opp_overall,
                               min_total_venue=7, min_total_overall=14) -> Tuple[bool, dict]:
    """For O2.5: combined opponent concession across both teams must be high.

    Example: if home opponent conceded in 4/5 and away opponent conceded in 4/5,
    combined = 8/10 venue concession — supports O2.5.
    """
    def _count_conceded(form, n):
        sample = (form or [])[:n]
        return sum(1 for _, ga in sample if ga >= 1), len(sample)

    hv_c, hv_n = _count_conceded(home_opp_venue, 5)
    av_c, av_n = _count_conceded(away_opp_venue, 5)
    ho_c, ho_n = _count_conceded(home_opp_overall, 10)
    ao_c, ao_n = _count_conceded(away_opp_overall, 10)

    venue_total = hv_c + av_c
    venue_sample = min(hv_n + av_n, 10)  # cap at 10
    overall_total = ho_c + ao_c
    overall_sample = min(ho_n + ao_n, 20)  # cap at 20

    venue_pass = venue_sample >= 6 and venue_total >= min_total_venue
    overall_pass = overall_sample >= 10 and overall_total >= min_total_overall

    return venue_pass or overall_pass, {
        "venue": {"combined": venue_total, "sample": venue_sample, "need": min_total_venue, "pass": venue_pass},
        "overall": {"combined": overall_total, "sample": overall_sample, "need": min_total_overall, "pass": overall_pass},
    }


def check_combined_defence(home_opp_venue, home_opp_overall,
                            away_opp_venue, away_opp_overall,
                            min_total_venue=3, min_total_overall=6) -> Tuple[bool, dict]:
    """For U2.5: combined opponent clean sheets across both teams must be high."""
    def _count_cs(form, n):
        sample = (form or [])[:n]
        return sum(1 for _, ga in sample if ga == 0), len(sample)

    hv_c, hv_n = _count_cs(home_opp_venue, 5)
    av_c, av_n = _count_cs(away_opp_venue, 5)
    ho_c, ho_n = _count_cs(home_opp_overall, 10)
    ao_c, ao_n = _count_cs(away_opp_overall, 10)

    venue_total = hv_c + av_c
    venue_sample = min(hv_n + av_n, 10)
    overall_total = ho_c + ao_c
    overall_sample = min(ho_n + ao_n, 20)

    venue_pass = venue_sample >= 6 and venue_total >= min_total_venue
    overall_pass = overall_sample >= 10 and overall_total >= min_total_overall

    return venue_pass and overall_pass, {
        "venue": {"combined_cs": venue_total, "sample": venue_sample, "need": min_total_venue, "pass": venue_pass},
        "overall": {"combined_cs": overall_total, "sample": overall_sample, "need": min_total_overall, "pass": overall_pass},
    }
