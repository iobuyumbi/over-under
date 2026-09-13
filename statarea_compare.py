#!/usr/bin/env python3
"""Fetch, cache, parse, and compare our picks against statarea.com/predictions.

This module is intentionally read-only w.r.t. our prediction pipeline: it never
feeds back into the engines by default — callers (run_local.bat, a GHA step, or
an ad-hoc script) choose what to do with the AGREE/DIVERGE/NOT_FOUND signals.

Architecture
------------
1. ``fetch_card`` / ``load_card``  — one HTTP GET per day, cached to
   ``.statarea_cache/YYYY-MM-DD.md`` for up to ``CACHE_TTL_SECONDS``.  Direct
   GET is tried first; if the page layout is unparseable we fall back to a
   reader-style proxy.  In CI this guarantees exactly one statarea hit per day
   even if 4 markets all enrich their picks.
2. ``parse_card``                 — token-driven layout parser that knows the
   exact column order and correctly skips ``[score]`` integer / ``-`` slots
   that appear for live matches, which is what trips up naive regexes.
3. ``compare_picks``              — team-name normalisation (strip FC/AFC/SC
   prefixes, downcase, unidecode), an editable ``ALIASES`` dict for hard
   abbreviations, fuzzy-match fallback, then one of::

       AGREE_OVER   (market=over  AND statarea over25 >= 70)
       AGREE_UNDER  (market=under AND statarea over25 <= 40)
       AGREE_HOME   (market=home_win AND statarea home_win >= 55)
       DIVERGE      (team found but threshold not met)
       NOT_FOUND    (team absent from today's statarea card)

CLI entry points
----------------
* ``statarea_compare.py --fetch``                     — warm the cache once.
* ``statarea_compare.py --list-today``                — print over/under/home
  buckets for the whole card (over >=70, under <=40, home >=55).
* ``statarea_compare.py --picks picks.csv --out cmp.json``  — enrich our picks.

Thresholds are module-level constants so that downstream callers that want
"hard gate" behaviour can just ``import THRESH_OVER`` instead of re-typing
magic numbers.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Tunables — edit these to match the engine's live calibration
# ---------------------------------------------------------------------------

THRESH_OVER: int = 70      # statarea over25 >= this => AGREE_OVER
THRESH_UNDER: int = 40     # statarea over25 <= this => AGREE_UNDER
THRESH_HOME: int = 55      # statarea home_win >= this => AGREE_HOME

CACHE_DIR: Path = Path(".statarea_cache")
CACHE_TTL_SECONDS: int = 12 * 3600   # 12h — one fetch per day in practice

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)

# Known-abbreviation / canonical-name map.  Keys are the NORMALISED form
# (see ``_norm`` below) of how our engines write the team; values are the
# NORMALISED form that appears on statarea's card.  Add entries as the
# "NO MATCH" lines in --list-today/--picks output demand them.
ALIASES: Dict[str, str] = {
    "go a eagles": "go ahead eagles",
    "bristol c": "bristol city",
    "st gallen": "fc st gallen",
    "saint gallen": "fc st gallen",
    "fylde": "afc fylde",
    "afc fylde": "afc fylde",
    "peterborough": "peterborough united",
    "man utd": "manchester united",
    "man city": "manchester city",
    "tottenham": "tottenham hotspur",
    "spurs": "tottenham hotspur",
    "leicester": "leicester city",
    "notts forest": "nottingham forest",
    "nottm forest": "nottingham forest",
    "brighton": "brighton hove albion",
    "newcastle": "newcastle united",
    "west ham": "west ham united",
    "wolves": "wolverhampton wanderers",
    "wolves wanderers": "wolverhampton wanderers",
    "sheffield utd": "sheffield united",
    "leeds": "leeds united",
    "hull": "hull city",
    "coventry": "coventry city",
    "middlesbrough": "middlesbrough",
    "boro": "middlesbrough",
    "swansea": "swansea city",
    "cardiff": "cardiff city",
    "norwich": "norwich city",
    "watford": "watford",
    "millwall": "millwall",
    "birmingham": "birmingham city",
    "sheffield wed": "sheffield wednesday",
    "sheffield wednesday": "sheffield wednesday",
    "preston": "preston north end",
    "preston ne": "preston north end",
    "blackburn": "blackburn rovers",
    "plymouth": "plymouth argyle",
    "stoke": "stoke city",
    "wba": "west bromwich albion",
    "west brom": "west bromwich albion",
    "sunderland": "sunderland",
    "rotherham": "rotherham united",
    "qpr": "queens park rangers",
    "ipswich": "ipswich town",
    "bristol r": "bristol rovers",
    "charlton": "charlton athletic",
    "portsmouth": "portsmouth",
    "exeter": "exeter city",
    "oxford": "oxford united",
    "cambridge": "cambridge united",
    "bolton": "bolton wanderers",
    "derby": "derby county",
    "reading": "reading",
    "barnsley": "barnsley",
    "wigan": "wigan athletic",
    "shrewsbury": "shrewsbury town",
    "lincoln": "lincoln city",
    "mk dons": "milton keynes dons",
    "morecambe": "morecambe",
    "mansfield": "mansfield town",
    "crawley": "crawley town",
    "gillingham": "gillingham",
    "harrogate": "harrogate town",
    "salford": "salford city",
    "stockport": "stockport county",
    "wrexham": "wrexham",
    "accrington": "accrington stanley",
    "barrow": "barrow",
    "colchester": "colchester united",
    "doncaster": "doncaster rovers",
    "grimsby": "grimsby town",
    "notts county": "notts county",
    "sutton": "sutton united",
    "tramere": "tranmere rovers",
    "tranmere": "tranmere rovers",
    "walsall": "walsall",
    "bradford": "bradford city",
    "crewe": "crewe alexandra",
    "foresti": "nottingham forest",
}


# ---------------------------------------------------------------------------
# Cache + fetch
# ---------------------------------------------------------------------------

def _today() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


def _cache_path(date: Optional[str] = None) -> Path:
    d = date or _today()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{d}.md"


def _cache_age_secs(path: Path) -> Optional[float]:
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


def _reader_proxy(url: str) -> str:
    """Best-effort reader-style fallback if the main statarea GET reflows.

    Uses a public no-JS reader endpoint.  When it also fails the caller
    (``fetch_card``) bubbles the last network error so the operator can
    retry --fetch later.
    """
    proxies = [
        f"https://text.npr.org/parse?url={requests.utils.quote(url)}",
        f"https://12ft.io/api/proxy?q={requests.utils.quote(url)}",
    ]
    last_err: Optional[Exception] = None
    for p in proxies:
        try:
            r = requests.get(p, headers={"User-Agent": USER_AGENT}, timeout=30)
            if r.status_code == 200 and len(r.text) > 2000:
                return r.text
        except requests.RequestException as e:
            last_err = e
    if last_err is not None:
        raise last_err
    raise RuntimeError("reader-proxy exhausted and nothing returned a card.")


def fetch_card(date: Optional[str] = None, *, force: bool = False) -> str:
    """Download statarea.com/predictions; cache per ``date`` (12h TTL)."""
    date = date or _today()
    cpath = _cache_path(date)
    age = _cache_age_secs(cpath)
    if not force and age is not None and age < CACHE_TTL_SECONDS:
        return cpath.read_text(encoding="utf-8", errors="replace")

    url = "https://statarea.com/predictions"
    last_err: Optional[Exception] = None
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
        if r.status_code == 200 and len(r.text) > 5000:
            text = r.text
        else:
            raise RuntimeError(f"statarea GET {r.status_code} ({len(r.text)}b)")
    except Exception as e:  # noqa: BLE001 — bubble only as last_err
        last_err = e
        try:
            text = _reader_proxy(url)
        except Exception as e2:  # noqa: BLE001
            raise RuntimeError(
                f"Could not fetch statarea card: direct={e!r}, proxy fallback={e2!r}"
            ) from e2

    cpath.write_text(text, encoding="utf-8")
    return text


def load_card(date: Optional[str] = None, *, force: bool = False) -> str:
    """Cached-read convenience wrapper.  Raises if nothing on disk and offline."""
    return fetch_card(date=date, force=force)


# ---------------------------------------------------------------------------
# Parser — BeautifulSoup DOM walker
# ---------------------------------------------------------------------------
#
# Statarea's ``/predictions`` HTML layout (confirmed live probe 2026-09-13):
#   div.predictions
#     div.competition              (one per league)
#       div.ownheader             (league banner / date banner — skip)
#       div.body                  (one league body, contains several matches)
#         div.cmatch  (1 per match)
#           div.time  "HH:MM"
#           div.tip   highlighted TIP cell value (1, X, 2, O25, …)
#           div.teams "HOME - AWAY"  (separator is one of " - ", " – ", " — ")
#         (sibling coefboxes / odds cells interleaved in DOM by position)
#
# The percentage-value cells (.coefbox class) appear in the page *outside*
# div.cmatch — they are interleaved as following siblings / cousin-divs in DOM
# order, 12–26 per match.  The ratio across the full page is stable:
# len(find_all('.coefbox')) / len(find_all('.cmatch')) == 26.11 for 2026-09-13.
# That ratio holds because statarea publishes one 12-cell block of the MAIN
# market (1/X/2 HT1/HTX/HT2 O1.5/O2.5/O3.5 BTS/OTS) and then a duplicate
# 12–14 cell block of DOUBLE-CHANCE / 1HALF / handicap odds we don't need.
# So the parser below:
#   1. Finds every div in the page in document order.
#   2. On div.cmatch → opens a new MatchRow + reads .time/.tip/.teams.
#   3. On div.coefbox → appends .text to the open MatchRow.
#   4. On the NEXT div.cmatch → closes previous row, pads its 12-value
#      main-market slots by index (0-11 per the header: 1/X/2/HT1/HTX/HT2/
#      1.5/2.5/3.5/BTS/OTS) into MatchRow.{home,draw,away}_vote and HT* + O*
#      slots.  If the row only gathered <12 coefs, trailing ones become -1.
#
# The FIRST 12 coefboxes in the whole document are the COLUMN HEADER labels
# ("1","X","2","HT1","HTX","HT2","1.5","2.5","3.5","BTS","OTS").  They are
# NOT match odds, so the parser drops them (skips 12, then starts assigning
# to open row's coefs).

_PERCENT_RE = re.compile(r"^(100|[1-9]?\d)$")
_KO_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass
class MatchRow:
    kickoff: str                # "HH:MM" (statarea local time, usually CET)
    votes: int                  # total votes listed on the row, 0 if n/a
    tip: str                    # the highlighted TIP cell value, e.g. "2", "X", "O25", "U25", "BTTS", "1"
    home: str
    away: str
    # numeric columns in % — parsed but stored as ints; -1 when missing
    home_vote: int = -1
    draw_vote: int = -1
    away_vote: int = -1
    ht1: int = -1
    htx: int = -1
    ht2: int = -1
    o15: int = -1
    o25: int = -1
    o35: int = -1
    bts: int = -1
    ots: int = -1
    # How many coefboxes were captured for this match (diagnostic only):
    _coef_count: int = 0

    @property
    def over25(self) -> int:
        return self.o25

    @property
    def home_win(self) -> int:
        return self.home_vote


def _split_teams(s: str) -> Tuple[str, str]:
    """Split "HOME - AWAY" style text, tolerating several dash variants."""
    if not s:
        return "", ""
    s = s.strip()
    # Try the 3 most common separators in order
    for sep in (" - ", " – ", " — ", " vs ", " VS ", " v ", " V "):
        if sep in s:
            parts = s.split(sep, 1)
            return parts[0].strip(), parts[1].strip()
    # Fallback: if no multi-space separator try single en-dash etc.
    for sep in ("-", "–", "—"):
        if sep in s and len(s.split(sep, 1)) == 2:
            a, b = s.split(sep, 1)
            return a.strip(), b.strip()
    return s, ""


def _to_pct(token: str) -> int:
    token = (token or "").strip().rstrip("%").strip()
    return int(token) if _PERCENT_RE.match(token) else -1


def parse_card(text: str) -> List[MatchRow]:
    """Parse statarea /predictions HTML (cached or freshly fetched)."""
    soup = BeautifulSoup(text, "html.parser")
    rows: List[MatchRow] = []

    # The GLOBAL PAGE HEADER is 11 coefboxes at the very top of the document:
    #   1 X 2 HT1 HTX HT2 1.5 2.5 3.5 BTS OTS
    # Then for EACH MATCH individually, statarea repeats a second mini-header
    # of 11 LABEL coefboxes (1 X 2 H1 HX H2 1.5 2.5 3.5 BTS OTS — slight
    # wording differences but still 11 label tokens), followed immediately by
    # 11 VALUE coefboxes (the actual 0–100 % numbers we need).  After that
    # there are usually 4–6 extra handicap / double-chance coefs we ignore.
    #
    # Accounting with the page we probed (2026-09-13):
    #     11 global header
    #   + 265 matches × (11 mini-label + 11 value ≈ 22.0 coefs/match)
    #   = 11 + 265×26.07 ≈ 6919 coefboxes total ✓
    #
    # State machine:
    #   state = "GLOBAL_HEADER_DROP"  (drop 11)
    #   state = "MATCH_LABEL_DROP"    (we just saw a cmatch; drop next 11 coefs = its labels)
    #   state = "MATCH_VALUES"        (next 11 coefs → actual %s for open row)
    GLOBAL_HEADER = 11
    MINI_HEADER   = 11
    VALUE_COLS    = 11
    state = "GLOBAL_HEADER_DROP"
    drop_remaining = GLOBAL_HEADER
    value_remaining = 0
    open_row: Optional[MatchRow] = None
    open_coefs: List[str] = []

    def finalise(row: Optional[MatchRow], coefs: List[str]) -> None:
        if row is None:
            coefs.clear()
            return
        # 11-value layout is the well-known header order:
        #   0  1  2   3    4    5    6    7    8    9    10
        #   1  X  2   HT1  HTX  HT2  1.5  2.5  3.5  BTS  OTS
        vals = [_to_pct(c) for c in (coefs[:VALUE_COLS] + [""] * (VALUE_COLS - len(coefs[:VALUE_COLS])))]
        row.home_vote, row.draw_vote, row.away_vote = vals[0], vals[1], vals[2]
        row.ht1, row.htx, row.ht2 = vals[3], vals[4], vals[5]
        row.o15, row.o25, row.o35 = vals[6], vals[7], vals[8]
        row.bts, row.ots = vals[9], vals[10]
        row._coef_count = len(coefs)
        rows.append(row)
        coefs.clear()

    # Walk divs in global document order — the cmatch/coef interleaving is
    # correct only with soup-wide order, not scoped to a subtree.
    for div in soup.find_all("div"):
        classes = div.get("class") or []
        if "cmatch" in classes:
            finalise(open_row, open_coefs)
            open_row = MatchRow(
                kickoff="",
                votes=0,
                tip="",
                home="",
                away="",
            )
            t_el = div.find("div", class_="time")
            if t_el is not None:
                txt = t_el.get_text(" ", strip=True)
                ko = _KO_RE.search(txt)
                if ko:
                    open_row.kickoff = ko.group(0)
            tip_el = div.find("div", class_="tip")
            if tip_el is not None:
                open_row.tip = tip_el.get_text(" ", strip=True)
            teams_el = div.find("div", class_="teams")
            if teams_el is not None:
                h, a = _split_teams(teams_el.get_text(" ", strip=True))
                open_row.home = h
                open_row.away = a
            # After cmatch comes the per-match mini-header: drop 11 coefs.
            state = "MATCH_LABEL_DROP"
            drop_remaining = MINI_HEADER
            value_remaining = VALUE_COLS
            open_coefs.clear()
            continue

        if "coefbox" not in classes:
            continue
        # ---- div is a coefbox from here on ----
        if state == "GLOBAL_HEADER_DROP":
            if drop_remaining > 0:
                drop_remaining -= 1
                if drop_remaining == 0:
                    # Between the global header and the first real cmatch we
                    # can see stray coefs or a mini-header for match#1 before
                    # match#1's cmatch even appears.  Stay in GLOBAL_HEADER_DROP
                    # until cmatch fires and transitions us out.
                    state = "MATCH_LABEL_DROP"  # ignored until first cmatch
            continue
        if state == "MATCH_LABEL_DROP":
            if open_row is None:
                continue
            if drop_remaining > 0:
                drop_remaining -= 1
                if drop_remaining == 0:
                    state = "MATCH_VALUES"
            continue
        if state == "MATCH_VALUES" and open_row is not None:
            open_coefs.append(div.get_text(" ", strip=True))
            value_remaining -= 1
            if value_remaining <= 0:
                # 11 values captured. Remaining coefboxes before next cmatch
                # are the extra double-chance / handicap columns we ignore.
                # Leave open_row intact so the next cmatch finalises it.
                state = "TRAILING_COEF_IGNORE"
            continue
        # TRALING_COEF_IGNORE or any unknown state: drop coefbox

    finalise(open_row, open_coefs)

    # Filter out rows with no kickoff + no team names — those are header-only.
    cleaned: List[MatchRow] = []
    for r in rows:
        if r.kickoff or (r.home and r.away):
            cleaned.append(r)
    return cleaned



# ---------------------------------------------------------------------------
# Team-name normalisation + matching
# ---------------------------------------------------------------------------

_PREFIXES = (
    "afc ", "fc ", "sc ", "sk ", "sp ", "sl ", "sb ", "sv ",
    "rc ", "ac ", "as ", "cd ", "cf ", "cp ", "fk ", "ifk ",
    "bk ", "sk ", "rb ", "bsc ", "tsg ", "kf ", "tf ", "ofk ",
    "klub ", "fk ", "pfc ", "cska ", "dynamo ", "dinamo ",
    "spartak ", "lokomotiv ", "cfr ", "fcsb ", "rapid ",
)


def _norm(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # strip trailing "cf", "fc", "afc" if space-separated
    words = s.split()
    if len(words) > 1 and words[-1] in ("fc", "afc", "sc", "cf", "ac", "as", "cd", "sk", "ifk", "fk", "bsc"):
        words = words[:-1]
        s = " ".join(words)
    # strip leading prefixes
    for p in _PREFIXES:
        if s.startswith(p):
            s = s[len(p):].strip()
            break
    return s.strip()


def _key(home: str, away: str) -> Tuple[str, str]:
    return (_norm(home), _norm(away))


def _resolve_alias(norm: str) -> str:
    if norm in ALIASES:
        return ALIASES[norm]
    # Word-set match — alias key words subset of norm's words, or vice versa
    nset = set(norm.split())
    for k, v in ALIASES.items():
        kset = set(k.split())
        if kset and (kset <= nset or nset <= kset):
            return v
    return norm


def build_lookup(rows: Iterable[MatchRow]) -> Dict[Tuple[str, str], MatchRow]:
    """Map (normalised home, normalised away) → row.

    Both directions are registered so that a callerside "away vs home" typo
    still hits (DIVERGE then wins; the caller is responsible for market
    direction checks on home_win).
    """
    lut: Dict[Tuple[str, str], MatchRow] = {}
    for r in rows:
        nh, na = _resolve_alias(_norm(r.home)), _resolve_alias(_norm(r.away))
        lut[(nh, na)] = r
        # reverse key — compare_picks will flip home_vote to away_vote if used.
        # We store under a marker; lookup logic handles it below.
    return lut


def fuzzy_find(
    rows: Sequence[MatchRow],
    home: str,
    away: str,
    *,
    cutoff: float = 0.78,
) -> Optional[MatchRow]:
    h = _resolve_alias(_norm(home))
    a = _resolve_alias(_norm(away))
    h_words = h.split()
    a_words = a.split()
    best: Optional[Tuple[float, MatchRow]] = None
    for r in rows:
        rh = _norm(r.home)
        ra = _norm(r.away)
        s1 = difflib.SequenceMatcher(None, h, rh).ratio()
        s2 = difflib.SequenceMatcher(None, a, ra).ratio()
        combined = (s1 + s2) / 2
        # bonus if any of our content words matches exactly (covers "city", "united", "rovers")
        if h_words and set(h_words) & set(rh.split()):
            combined += 0.06
        if a_words and set(a_words) & set(ra.split()):
            combined += 0.06
        # cross-check: swapped names
        sx1 = difflib.SequenceMatcher(None, h, ra).ratio()
        sx2 = difflib.SequenceMatcher(None, a, rh).ratio()
        combined = max(combined, (sx1 + sx2) / 2 - 0.03)
        if combined >= cutoff and (best is None or combined > best[0]):
            best = (combined, r)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Compare picks API
# ---------------------------------------------------------------------------

@dataclass
class ComparedPick:
    home: str
    away: str
    market: str = "over"            # over | under | home_win | btts | oo05
    confidence: Optional[float] = None
    statarea: Optional[Dict[str, Any]] = None   # match_row dict when found
    statarea_note: str = "NOT_FOUND"            # AGREE_OVER|AGREE_UNDER|AGREE_HOME|DIVERGE|NOT_FOUND
    note_extra: str = ""


def _note_for(
    pick_market: str,
    row: Optional[MatchRow],
) -> str:
    if row is None:
        return "NOT_FOUND"
    if pick_market in ("over", "o25"):
        return "AGREE_OVER" if row.o25 >= THRESH_OVER else "DIVERGE"
    if pick_market in ("under", "u25"):
        return "AGREE_UNDER" if row.o25 >= 0 and row.o25 <= THRESH_UNDER else "DIVERGE"
    if pick_market in ("home", "home_win", "hw", "1"):
        return "AGREE_HOME" if row.home_vote >= THRESH_HOME else "DIVERGE"
    # Markets we don't have explicit rule for: still say DIVERGE to avoid a
    # bogus AGREE_* call-site reading.  Callers can extend as needed.
    return "DIVERGE"


def compare_picks(
    picks: Iterable[Dict[str, Any]],
    rows: Sequence[MatchRow],
) -> List[ComparedPick]:
    """Match each of *our* picks (``home``, ``away`` required keys) to a row.

    ``picks`` is intentionally permissive: list-of-dicts, each with keys like
    ``{"home": str, "away": str, "market": "over"|"under"|"home_win"|"btts"|"oo05",
    "confidence": 0.71}`` — confidence and market default if absent.
    """
    lut = build_lookup(rows)
    out: List[ComparedPick] = []
    for p in picks:
        home = str(p.get("home", "")).strip()
        away = str(p.get("away", "")).strip()
        market = str(p.get("market", "over") or "over").lower()
        try:
            conf = float(p["confidence"]) if "confidence" in p and p["confidence"] not in (None, "") else None
        except (TypeError, ValueError):
            conf = None
        nh, na = _resolve_alias(_norm(home)), _resolve_alias(_norm(away))
        row: Optional[MatchRow] = lut.get((nh, na))
        extra = ""
        if row is None:
            # Swap attempt — caller might have typed home/away backwards (rare)
            row = lut.get((na, nh))
            if row is not None:
                extra = "teams_swapped"
        if row is None:
            row = fuzzy_find(rows, home, away)
            if row is not None:
                extra = f"fuzzy:{_norm(row.home)} vs {_norm(row.away)}"
        note = _note_for(market, row)
        out.append(ComparedPick(
            home=home, away=away, market=market, confidence=conf,
            statarea=asdict(row) if row else None,
            statarea_note=note,
            note_extra=extra,
        ))
    return out


# ---------------------------------------------------------------------------
# Today buckets (--list-today)
# ---------------------------------------------------------------------------

def list_today_buckets(rows: Sequence[MatchRow]) -> Dict[str, List[str]]:
    overs: List[str] = []
    unders: List[str] = []
    homes: List[str] = []
    for r in rows:
        label = f"{r.home} vs {r.away}"
        if r.o25 >= THRESH_OVER:
            overs.append(f"{label} ({r.o25}%)")
        if 0 <= r.o25 <= THRESH_UNDER:
            unders.append(f"{label} (U {r.o25}%)")
        if r.home_vote >= THRESH_HOME:
            homes.append(f"{label} (1 {r.home_vote}%)")
    return {
        "over_games": overs,
        "under_games": unders,
        "home_games": homes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _read_picks_csv(path: str) -> List[Dict[str, Any]]:
    picks: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            r = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()}
            if not r.get("home") or not r.get("away"):
                continue
            picks.append(r)
    return picks


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="statarea_compare", description=__doc__)
    p.add_argument("--fetch", action="store_true", help="Download today's card (or refresh if TTL expired) and exit.")
    p.add_argument("--force", action="store_true", help="Refetch even if cache is still fresh.")
    p.add_argument("--date", default=None, help="YYYY-MM-DD cache bucket (default: today local).")
    p.add_argument("--list-today", action="store_true", help="Print over/under/home buckets to stdout as JSON.")
    p.add_argument("--picks", metavar="CSV", help="CSV with columns home,away[,market[,confidence]] — enrich each pick.")
    p.add_argument("--out", metavar="FILE", help="Write enriched JSON (--picks) or bucket JSON (--list-today) here.")
    p.add_argument("--parsed-count-only", action="store_true", help="After parse, print count of rows parsed and exit 0 if >0 else 2.")
    args = p.parse_args(list(argv) if argv else None)

    try:
        stdout_reconfigure = sys.stdout.reconfigure  # type: ignore[attr-defined]
        stdout_reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    text = fetch_card(date=args.date, force=args.force)
    rows = parse_card(text)

    if args.parsed_count_only:
        print(f"parsed {len(rows)} matches")
        return 0 if rows else 2

    if args.fetch:
        print(f"cached {len(rows)} matches → {_cache_path(args.date)}")
        return 0 if rows else 2

    if args.list_today:
        buckets = list_today_buckets(rows)
        blob = json.dumps(buckets, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(blob + "\n", encoding="utf-8")
        else:
            print(blob)
        # Summary line to stderr so pipe-to-jq still works
        print(
            f"over>={THRESH_OVER}: {len(buckets['over_games'])}   "
            f"under<={THRESH_UNDER}: {len(buckets['under_games'])}   "
            f"home>={THRESH_HOME}: {len(buckets['home_games'])}   "
            f"(card rows: {len(rows)})",
            file=sys.stderr,
        )
        return 0

    if args.picks:
        if not os.path.exists(args.picks):
            print(f"ERROR: picks CSV not found: {args.picks}", file=sys.stderr)
            return 2
        picks = _read_picks_csv(args.picks)
        compared = compare_picks(picks, rows)
        serialised = [asdict(c) for c in compared]
        blob = json.dumps({"rows": serialised, "thresholds": {
            "over": THRESH_OVER, "under": THRESH_UNDER, "home_win": THRESH_HOME,
        }}, indent=2, ensure_ascii=False)
        if args.out:
            Path(args.out).write_text(blob + "\n", encoding="utf-8")
        else:
            print(blob)
        # Human-readable line per pick to stderr for the operator
        counts = {"AGREE_OVER": 0, "AGREE_UNDER": 0, "AGREE_HOME": 0, "DIVERGE": 0, "NOT_FOUND": 0}
        for c in compared:
            counts[c.statarea_note] = counts.get(c.statarea_note, 0) + 1
            tag = c.statarea_note
            if c.note_extra:
                tag += f" [{c.note_extra}]"
            print(f"  {tag:20s}  {c.home} vs {c.away}  ({c.market})", file=sys.stderr)
        print(
            "totals -> " + "  ".join(f"{k}={v}" for k, v in counts.items()),
            file=sys.stderr,
        )
        return 0

    p.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
