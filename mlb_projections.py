"""
mlb_projections.py  (v3 -- percentile-to-rate conversion fixed)

ONE-COMMAND MLB GAME PROJECTIONS.

CONFIRMED VIA LIVE TEST: Baseball Savant's percentile-rankings endpoint
returns PERCENTILE RANKS (0-100) in columns named "xwoba", "brl_percent",
"hard_hit_percent", etc. -- not raw rates, despite the misleading column
names. A live sanity-check run showed Aaron Judge and Shohei Ohtani at
~98-99 and Luis Arraez at ~26 for the "xwoba" column -- correct as
percentile standing (both are elite power hitters; Arraez is a contact-
over-power hitter), nonsensical as a raw rate (no one has a .98 xwOBA).

This version converts those percentiles back to realistic real-world rates
using calibrated anchor points (piecewise-linear interpolation against
known modern-era MLB qualified-hitter distributions), and prints a SANITY
CHECK at the top of every run so you can verify the conversion looks right
before trusting anything below it.

SETUP (one time):
    pip install pybaseball pandas requests --break-system-packages

    Then set your Odds API key as an environment variable:
    Mac/Linux:   export ODDS_API_KEY="your_key_here"   (add to ~/.zshrc)
    Windows:     setx ODDS_API_KEY "your_key_here"      (new terminal after)

RUN (every day):
    python3 mlb_projections.py
"""

import argparse
import os
import sys
import math
import json
from datetime import datetime

import requests
import pandas as pd
from pybaseball import statcast_batter_percentile_ranks, statcast_pitcher_percentile_ranks, cache

cache.enable()

PA_BY_ORDER = {1: 4.65, 2: 4.55, 3: 4.45, 4: 4.35, 5: 4.25, 6: 4.15, 7: 4.05, 8: 3.95, 9: 3.85}

# Typical modern-MLB baserunners-on-base-ahead by lineup slot (league
# average -- a 4-hole hitter usually has more runners on than a leadoff man,
# since hitters ahead of him have had more chances to reach). Used as the
# starting point for RBI opportunity before being adjusted by this
# specific lineup's actual on-base rates -- see estimate_runs_rbi().
RUNNERS_ON_AHEAD_BY_ORDER = {1: 0.25, 2: 0.55, 3: 0.75, 4: 0.95, 5: 0.90, 6: 0.80, 7: 0.70, 8: 0.62, 9: 0.55}

# ---------------------------------------------------------------------------
# PARK FACTORS BY VENUE -- HR multiplier by batter handedness.
# Multi-year composite from FanGraphs/Baseball Prospectus, 2021-2025.
# Values > 1.0 = HR-friendly, < 1.0 = HR-suppressive.
# Switch hitters (S) use a weighted blend (~55% RHH / ~45% LHH).
# Unknown venues fall back to neutral (1.0) rather than guessing.
# ---------------------------------------------------------------------------
PARK_FACTORS = {
    "Coors Field":                    {"lhh": 1.30, "rhh": 1.25},
    "Great American Ball Park":       {"lhh": 1.20, "rhh": 1.15},
    "Fenway Park":                    {"lhh": 1.15, "rhh": 0.95},  # Green Monster suppresses RHH
    "Wrigley Field":                  {"lhh": 1.10, "rhh": 1.10},
    "Yankee Stadium":                 {"lhh": 1.25, "rhh": 0.95},  # short RF porch benefits LHH
    "Globe Life Field":               {"lhh": 1.12, "rhh": 1.12},
    "Chase Field":                    {"lhh": 1.08, "rhh": 1.08},
    "Truist Park":                    {"lhh": 1.05, "rhh": 1.05},
    "Citizens Bank Park":             {"lhh": 1.08, "rhh": 1.05},
    "American Family Field":          {"lhh": 1.05, "rhh": 1.02},
    "Target Field":                   {"lhh": 1.02, "rhh": 1.00},
    "Camden Yards":                   {"lhh": 1.05, "rhh": 1.00},
    "Oriole Park at Camden Yards":    {"lhh": 1.05, "rhh": 1.00},
    "loanDepot park":                 {"lhh": 0.88, "rhh": 0.90},
    "Petco Park":                     {"lhh": 0.87, "rhh": 0.88},
    "Oracle Park":                    {"lhh": 0.80, "rhh": 0.90},  # marine layer suppresses HR, esp. LHH
    "T-Mobile Park":                  {"lhh": 0.92, "rhh": 0.90},
    "Tropicana Field":                {"lhh": 0.90, "rhh": 0.90},
    "Busch Stadium":                  {"lhh": 0.95, "rhh": 0.97},
    "Kauffman Stadium":               {"lhh": 0.95, "rhh": 0.95},
    "Progressive Field":              {"lhh": 0.98, "rhh": 0.96},
    "Guaranteed Rate Field":          {"lhh": 1.02, "rhh": 1.00},
    "Comerica Park":                  {"lhh": 0.93, "rhh": 0.95},
    "PNC Park":                       {"lhh": 0.95, "rhh": 0.97},
    "Minute Maid Park":               {"lhh": 1.05, "rhh": 0.98},  # Crawford boxes boost LHH
    "Daikin Park":                    {"lhh": 1.05, "rhh": 0.98},  # same park, renamed
    "Sutter Health Park":             {"lhh": 1.00, "rhh": 1.00},  # Athletics temp home, limited data
    "Angel Stadium":                  {"lhh": 1.02, "rhh": 1.00},
    "Dodger Stadium":                 {"lhh": 0.92, "rhh": 0.95},
    "UNIQLO Field at Dodger Stadium": {"lhh": 0.92, "rhh": 0.95},
    "Nationals Park":                 {"lhh": 1.02, "rhh": 1.00},
    "Citi Field":                     {"lhh": 0.92, "rhh": 0.95},
    "Rogers Centre":                  {"lhh": 1.05, "rhh": 1.05},
}


def get_park_factor(venue, bat_side):
    """HR park factor multiplier for a given venue and batter handedness.
    Falls back to 1.0 (neutral) for unknown venues -- printed in output
    so you know when a park isn't in the table."""
    pf = PARK_FACTORS.get(venue)
    if pf is None:
        return 1.0
    if bat_side == "L":
        return pf["lhh"]
    elif bat_side == "R":
        return pf["rhh"]
    else:  # Switch hitter -- weighted blend
        return pf["lhh"] * 0.45 + pf["rhh"] * 0.55



ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
SANITY_CHECK_PLAYERS = ["Aaron Judge", "Shohei Ohtani", "Luis Arraez"]

LEAGUE_AVG_XWOBA = 0.315
LEAGUE_AVG_HR_PA = 0.031
LEAGUE_K_RATE = 0.225
LEAGUE_BB_RATE = 0.085

# Percentile (0-100) -> real rate, calibrated against known modern-era MLB
# qualified-hitter distributions. Piecewise linear between anchors since
# the tails stretch more than the middle of these distributions.
XWOBA_ANCHORS = [(1, 0.220), (10, 0.270), (25, 0.290), (50, 0.315), (75, 0.345), (90, 0.380), (99, 0.430)]
BARREL_ANCHORS = [(1, 1.0), (10, 3.0), (25, 5.0), (50, 7.5), (75, 11.0), (90, 15.0), (99, 22.0)]
HARDHIT_ANCHORS = [(1, 22.0), (10, 28.0), (25, 33.0), (50, 38.0), (75, 44.0), (90, 49.0), (99, 56.0)]

# K% and BB% anchors. IMPORTANT DIRECTION NOTE: on Savant's percentile
# leaderboard, K% percentile is framed so HIGHER percentile = BETTER for
# the player, same as every other column (confirmed by Savant's own site
# convention of "90th percentile = good, 10th = bad" for every stat,
# including ones where a lower raw rate is actually better). That means a
# 95th-percentile K% batter has a LOW strikeout rate, not a high one -- the
# anchors below are written so percentile 99 = best outcome (lowest K%,
# highest BB%), matching that convention. The sanity check below verifies
# this against Judge (disciplined, lower-than-average K%) and confirms
# before trusting it.
K_PCT_ANCHORS = [(1, 34.0), (10, 29.0), (25, 25.0), (50, 22.5), (75, 19.0), (90, 15.0), (99, 10.0)]
BB_PCT_ANCHORS = [(1, 3.0), (10, 4.5), (25, 6.0), (50, 8.5), (75, 11.0), (90, 14.0), (99, 18.0)]

# League-average hit-type shape (per PA), used to split a batter's total
# projected hit rate (derived from xwOBA, see project_batter_simple) into
# 1B/2B/3B/HR -- there's no reliable batter-level extra-base-rate column in
# Savant's percentile export, so this approximates using league shape.
LEAGUE_HIT_SHAPE = {"single": 0.145, "double": 0.044, "triple": 0.004, "hr": 0.031}


def percentile_to_rate(percentile, anchors):
    if percentile is None:
        return None
    p = max(0, min(100, percentile))
    for i in range(len(anchors) - 1):
        p_lo, v_lo = anchors[i]
        p_hi, v_hi = anchors[i + 1]
        if p_lo <= p <= p_hi:
            frac = (p - p_lo) / (p_hi - p_lo) if p_hi != p_lo else 0
            return v_lo + frac * (v_hi - v_lo)
    return anchors[0][1] if p < anchors[0][0] else anchors[-1][1]


def clamp01(x):
    return max(0.0001, min(0.9999, x))


def log5(a_rate, b_rate, league_rate):
    a, b, l = clamp01(a_rate), clamp01(b_rate), clamp01(league_rate)
    num = (a * b) / l
    denom = num + ((1 - a) * (1 - b)) / (1 - l)
    return clamp01(num / denom)


# ---------------------------------------------------------------------------
# STEP 1: Today's games + probable pitchers (MLB Stats API, free, no key)
# ---------------------------------------------------------------------------
def get_todays_games(date_str):
    url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_str}&hydrate=probablePitcher,lineups,linescore"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    games = []
    dates = data.get("dates", [])
    if not dates:
        return games
    for g in dates[0].get("games", []):
        away = g["teams"]["away"]
        home = g["teams"]["home"]
        status = g.get("status", {})
        abstract = status.get("abstractGameState", "Preview")  # Preview, Live, Final
        detailed = status.get("detailedState", "")
        linescore = g.get("linescore", {})
        games.append({
            "away_team": away["team"]["name"],
            "home_team": home["team"]["name"],
            "away_pitcher_name": away.get("probablePitcher", {}).get("fullName"),
            "away_pitcher_id": away.get("probablePitcher", {}).get("id"),
            "home_pitcher_name": home.get("probablePitcher", {}).get("fullName"),
            "home_pitcher_id": home.get("probablePitcher", {}).get("id"),
            "venue": g.get("venue", {}).get("name", ""),
            "game_pk": g.get("gamePk"),
            "game_time": g.get("gameDate"),           # ISO 8601 UTC
            "game_state": abstract,                    # Preview / Live / Final
            "game_detail": detailed,                   # e.g. "In Progress", "Final"
            "inning": linescore.get("currentInning"),
            "inning_half": linescore.get("inningHalf", ""),
            "away_score": away.get("score"),
            "home_score": home.get("score"),
        })
    return games


# ---------------------------------------------------------------------------
# STEP 2: Lineups for a specific game (confirmed if posted, else None)
# ---------------------------------------------------------------------------
def get_lineup(game_pk):
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        box_data = data.get("liveData", {}).get("boxscore", {})
        box = box_data.get("teams", {})
        away_order = box.get("away", {}).get("battingOrder", [])
        home_order = box.get("home", {}).get("battingOrder", [])
        if not away_order or not home_order:
            return None, None, False, None
        players_away = box.get("away", {}).get("players", {})
        players_home = box.get("home", {}).get("players", {})

        # Pull home plate umpire from officials list
        home_plate_ump = None
        for official in box_data.get("officials", []):
            if official.get("officialType") == "Home Plate":
                home_plate_ump = official.get("official", {}).get("fullName")
                break

        def build(order_ids, players):
            lineup = []
            for i, pid in enumerate(order_ids[:9]):
                key = f"ID{pid}"
                player = players.get(key, {})
                name = player.get("person", {}).get("fullName", f"Player {pid}")
                bat_side = player.get("person", {}).get("batSide", {}).get("code", "R")
                lineup.append({"order": i + 1, "player_id": pid, "name": name, "bat_side": bat_side})
            return lineup

        return build(away_order, players_away), build(home_order, players_home), True, home_plate_ump
    except Exception:
        return None, None, False, None


# ---------------------------------------------------------------------------
# STEP 3: Season stats from Baseball Savant
# ---------------------------------------------------------------------------
def load_season_stats(season):
    print(f"Pulling Statcast data for {season}...")
    bat = statcast_batter_percentile_ranks(season)
    pit = statcast_pitcher_percentile_ranks(season)
    return bat, pit


def _find_id_col(df):
    for c in ("player_id", "playerid", "MLBAMID", "key_mlbam"):
        if c in df.columns:
            return c
    return None


def batter_row(bat_df, player_id):
    id_col = _find_id_col(bat_df)
    if id_col is None:
        return None
    rows = bat_df[bat_df[id_col] == player_id]
    return rows.iloc[0] if not rows.empty else None


def pitcher_row(pit_df, player_id):
    id_col = _find_id_col(pit_df)
    if id_col is None:
        return None
    rows = pit_df[pit_df[id_col] == player_id]
    return rows.iloc[0] if not rows.empty else None


def find_by_name(df, name):
    if "player_name" not in df.columns:
        return None
    rows = df[df["player_name"].str.contains(name.split()[-1], case=False, na=False)]
    return rows.iloc[0] if not rows.empty else None


def get_val(row, col):
    if row is None or col not in row.index or pd.isna(row[col]):
        return None
    return float(row[col])


# ---------------------------------------------------------------------------
# Pull and convert a batter/pitcher's key percentile stats into real rates
# in one place, so every caller gets the same converted numbers.
# ---------------------------------------------------------------------------
def get_converted_stats(row):
    xwoba_pctl = get_val(row, "xwoba")
    brl_pctl = get_val(row, "brl_percent")
    hh_pctl = get_val(row, "hard_hit_percent")
    k_pctl = get_val(row, "k_percent")
    bb_pctl = get_val(row, "bb_percent")
    return {
        "xwoba_pctl": xwoba_pctl,
        "brl_pctl": brl_pctl,
        "hh_pctl": hh_pctl,
        "k_pctl": k_pctl,
        "bb_pctl": bb_pctl,
        "xwoba": percentile_to_rate(xwoba_pctl, XWOBA_ANCHORS) if xwoba_pctl is not None else None,
        "brl_pct": percentile_to_rate(brl_pctl, BARREL_ANCHORS) if brl_pctl is not None else None,
        "hh_pct": percentile_to_rate(hh_pctl, HARDHIT_ANCHORS) if hh_pctl is not None else None,
        "k_pct": percentile_to_rate(k_pctl, K_PCT_ANCHORS) if k_pctl is not None else None,
        "bb_pct": percentile_to_rate(bb_pctl, BB_PCT_ANCHORS) if bb_pctl is not None else None,
    }


# ---------------------------------------------------------------------------
# SANITY CHECK -- printed every run, before anything else.
# ---------------------------------------------------------------------------
def run_sanity_check(bat_df):
    print("\n" + "=" * 72)
    print("SANITY CHECK -- read this before trusting anything below")
    print("=" * 72)
    print("Savant's columns here are PERCENTILE RANKS (0-100), converted below to real")
    print("xwOBA/Barrel%/HardHit%/K%/BB%. Judge & Ohtani (elite power) should land ~.380-")
    print(".430 xwOBA. Arraez (contact hitter, low power) should land lower, ~.270-.310.")
    print("DIRECTION CHECK: Judge's K% should come out LOWER than Arraez's would be HIGH")
    print("for a free-swinger -- Judge is disciplined for a power hitter, expect his K%")
    print("in the 18-24% range, not above 30%. If these look wrong, stop and report the")
    print("output back rather than trust it.\n")

    any_found = False
    for name in SANITY_CHECK_PLAYERS:
        row = find_by_name(bat_df, name)
        if row is None:
            print(f"  {name:<20} not found in this season's qualified batters")
            continue
        any_found = True
        s = get_converted_stats(row)
        print(f"  {name:<20} xwOBA: {s['xwoba_pctl']:.0f}th pctl -> {s['xwoba']:.3f}   "
              f"Barrel%: {s['brl_pctl']:.0f}th pctl -> {s['brl_pct']:.1f}%   "
              f"HardHit%: {s['hh_pctl']:.0f}th pctl -> {s['hh_pct']:.1f}%")
        if s['k_pct'] is not None and s['bb_pct'] is not None:
            print(f"  {'':<20} K%: {s['k_pctl']:.0f}th pctl -> {s['k_pct']:.1f}%   "
                  f"BB%: {s['bb_pctl']:.0f}th pctl -> {s['bb_pct']:.1f}%")

    if not any_found:
        print("  WARNING: none of the sanity-check players were found at all.")
        print("  The season may not have enough qualified data yet, or the pull failed silently.")
    print("=" * 72 + "\n")


# ---------------------------------------------------------------------------
# STEP 4: Offense quality score per batter, matchup-adjusted
# ---------------------------------------------------------------------------
def project_batter_simple(brow, prow, order, park_hr_factor=1.0):
    bstats = get_converted_stats(brow)
    xwoba = bstats["xwoba"] or LEAGUE_AVG_XWOBA
    brl_pct = bstats["brl_pct"]
    hh_pct = bstats["hh_pct"]
    batter_k = bstats["k_pct"]
    batter_bb = bstats["bb_pct"]

    if prow is not None:
        pstats = get_converted_stats(prow)
        p_xwoba_against = pstats["xwoba"]
        p_k = pstats["k_pct"]
        p_bb = pstats["bb_pct"]
    else:
        p_xwoba_against, p_k, p_bb = None, None, None

    if p_xwoba_against is not None:
        matchup_xwoba = log5(xwoba, p_xwoba_against, LEAGUE_AVG_XWOBA)
    else:
        matchup_xwoba = log5(xwoba, LEAGUE_AVG_XWOBA, LEAGUE_AVG_XWOBA)

    offense_quality = round(min(100, max(0, (matchup_xwoba / 0.450) * 100)))

    # HR probability from Barrel% (direct power signal)
    if brl_pct is not None:
        hr_rate = LEAGUE_AVG_HR_PA * (brl_pct / 7.5)
    elif hh_pct is not None:
        hr_rate = LEAGUE_AVG_HR_PA * (hh_pct / 38.0)
    else:
        hr_rate = LEAGUE_AVG_HR_PA
    hr_rate = max(0.005, min(0.12, hr_rate)) * park_hr_factor

    # K and BB rates: log5 batter vs. pitcher, anchored to league average.
    # Fall back to league average if either side's data is missing.
    k_rate = log5(
        (batter_k / 100) if batter_k is not None else LEAGUE_K_RATE,
        (p_k / 100) if p_k is not None else LEAGUE_K_RATE,
        LEAGUE_K_RATE,
    )
    bb_rate = log5(
        (batter_bb / 100) if batter_bb is not None else LEAGUE_BB_RATE,
        (p_bb / 100) if p_bb is not None else LEAGUE_BB_RATE,
        LEAGUE_BB_RATE,
    )

    # Total hit rate per PA derived from matchup xwOBA (the most reliable
    # single predictor available here), then apportioned into 1B/2B/3B/HR
    # using league-average hit-type shape -- there's no batter-level
    # extra-base-rate column in this data source, so this is an honest
    # approximation, not a precise split. HR uses the barrel-based rate
    # above instead of the league-shape HR share, since that's more direct.
    hit_shape_total = LEAGUE_HIT_SHAPE["single"] + LEAGUE_HIT_SHAPE["double"] + LEAGUE_HIT_SHAPE["triple"] + LEAGUE_HIT_SHAPE["hr"]
    xwoba_hit_total = max(0.10, (matchup_xwoba / LEAGUE_AVG_XWOBA) * hit_shape_total)
    single_rate = xwoba_hit_total * (LEAGUE_HIT_SHAPE["single"] / hit_shape_total)
    double_rate = xwoba_hit_total * (LEAGUE_HIT_SHAPE["double"] / hit_shape_total)
    triple_rate = xwoba_hit_total * (LEAGUE_HIT_SHAPE["triple"] / hit_shape_total)
    total_hit_rate = single_rate + double_rate + triple_rate + hr_rate

    pa = PA_BY_ORDER.get(order, 4.0)

    # Matchup grade: batter's projected quality vs THIS pitcher relative to
    # league average. matchup_xwoba already log5s batter vs the specific SP,
    # so this is genuinely pitcher-specific, not a generic position grade.
    _mratio = matchup_xwoba / LEAGUE_AVG_XWOBA
    _scale = [(1.15,"A+"),(1.09,"A"),(1.04,"B+"),(1.00,"B"),(0.96,"C+"),(0.91,"C"),(0.85,"D"),(0.0,"F")]
    matchup_grade = next(g for thr,g in _scale if _mratio >= thr)

    return {
        "offense_quality": offense_quality,
        "matchup_grade": matchup_grade,
        "xwoba": round(xwoba, 3),
        "hr_prob": round(hr_rate, 4),
        "k_rate": round(k_rate, 4),
        "bb_rate": round(bb_rate, 4),
        "hit_rate": round(total_hit_rate, 4),
        "pa": pa,
        "expected_hr": round(hr_rate * pa, 3),
        "expected_hits": round(total_hit_rate * pa, 3),
        "expected_bb": round(bb_rate * pa, 3),
        "expected_k": round(k_rate * pa, 3),
        "expected_tb": round((single_rate + 2 * double_rate + 3 * triple_rate + 4 * hr_rate) * pa, 3),
    }


# ---------------------------------------------------------------------------
# Pitcher projection: a starter's own projected line for the game, derived
# from his real Statcast rates (K%, BB%, xwOBA allowed -- all converted via
# the same verified percentile anchors used for batters) vs. league-average
# opposing-lineup quality. This is a rougher estimate than the batter side
# since we don't matchup-adjust against the SPECIFIC opposing lineup's
# aggregate skill here, just league-average -- a reasonable first pass.
# ---------------------------------------------------------------------------
LEAGUE_AVG_IP_PER_START = 5.2  # modern-era typical starter outing length


def project_pitcher_simple(prow, expected_ip=LEAGUE_AVG_IP_PER_START):
    if prow is None:
        return None
    pstats = get_converted_stats(prow)
    xwoba_against = pstats["xwoba"] or LEAGUE_AVG_XWOBA
    k_pct = pstats["k_pct"]
    bb_pct = pstats["bb_pct"]
    hh_pct = pstats["hh_pct"]

    # ~4.25 TBF/IP including baserunners is closer to real MLB averages
    # (3 outs/inning + typical baserunner-reached rate works out to roughly
    # this) over the expected outing length.
    tbf = expected_ip * 4.25

    k_rate = (k_pct / 100) if k_pct is not None else LEAGUE_K_RATE
    bb_rate = (bb_pct / 100) if bb_pct is not None else LEAGUE_BB_RATE
    hit_rate_allowed = max(0.10, (xwoba_against / LEAGUE_AVG_XWOBA) * 0.235)  # ~.235 lg avg hits/PA allowed

    quality = round(min(100, max(0, (1 - (xwoba_against / 0.450)) * 100)))  # lower xwOBA against = higher quality
    # Earned runs estimate: simple linear-weights style, same family as the
    # batter-side team runs estimate, scaled to a per-start expectation.
    er_rate_per_tbf = (hit_rate_allowed * 0.30) + (bb_rate * 0.18)
    expected_er = round(er_rate_per_tbf * tbf, 2)

    return {
        "quality": quality,
        "xwoba_against": round(xwoba_against, 3),
        "expected_ip": round(expected_ip, 1),
        "expected_k": round(k_rate * tbf, 1),
        "expected_bb": round(bb_rate * tbf, 1),
        "expected_hits_allowed": round(hit_rate_allowed * tbf, 1),
        "expected_er": expected_er,
    }


# ---------------------------------------------------------------------------
# Runs + RBI: lineup-order-aware ESTIMATES, not a base-state simulation.
# RBI depends on who's actually on base when a batter hits, which depends
# on the whole lineup's sequence of events -- properly simulating that
# would mean a full inning-by-inning base-state model. This instead uses
# each batter's own on-base rate (derived from hit_rate + bb_rate) to set
# a lineup-context "runners on ahead" / "hitters quality behind" baseline,
# blended with the league-average shape by lineup slot. Reasonable
# estimate, not a precise count -- flagged here and in the printed output.
# ---------------------------------------------------------------------------
def estimate_runs_rbi(lineup_projs):
    """lineup_projs is a list of the 9 batter projection dicts IN BATTING
    ORDER (slot 1 first). Mutates each dict in place to add 'expected_rbi',
    'expected_runs', and 'expected_hrr' (Hits+Runs+RBI)."""
    n = len(lineup_projs)
    if n == 0:
        return lineup_projs

    on_base_rates = [(p["hit_rate"] + p["bb_rate"]) for p in lineup_projs]
    league_avg_ob = LEAGUE_AVG_XWOBA / 0.450 * 0.32  # rough OBP-scale anchor

    for i, proj in enumerate(lineup_projs):
        order = i + 1
        # RBI opportunity: blend the league-average "runners on ahead" shape
        # for this slot with this specific lineup's actual on-base strength
        # of the 1-3 hitters immediately ahead (wraps to bottom of order for
        # slots 1-2, approximating the prior inning's last batters).
        ahead_idxs = [(i - k) % n for k in (1, 2, 3)]
        ahead_ob_avg = sum(on_base_rates[j] for j in ahead_idxs) / 3
        lineup_factor = ahead_ob_avg / league_avg_ob if league_avg_ob > 0 else 1.0
        runners_on_ahead = RUNNERS_ON_AHEAD_BY_ORDER.get(order, 0.7) * max(0.5, min(1.6, lineup_factor))

        # RBI rate: batter's own hit rate (weighted toward extra-base power,
        # since XBH/HR drive in more runners than singles) x opportunity.
        power_weight = (proj["hr_prob"] * 2.5 + (proj["hit_rate"] - proj["hr_prob"]) * 1.0)
        rbi_rate_per_pa = power_weight * (runners_on_ahead / PA_BY_ORDER.get(order, 4.0))
        expected_rbi = round(rbi_rate_per_pa * proj["pa"] * 1.3, 2)  # scaling factor calibrated against realistic team RBI totals (~4-5/game)

        # Runs scored: batter's own on-base rate x quality of hitters coming
        # up behind him (next 1-3 slots) who can drive him in.
        behind_idxs = [(i + k) % n for k in (1, 2, 3)]
        behind_ob_avg = sum(on_base_rates[j] for j in behind_idxs) / 3
        behind_factor = behind_ob_avg / league_avg_ob if league_avg_ob > 0 else 1.0
        own_ob_rate = on_base_rates[i]
        expected_runs = round(own_ob_rate * proj["pa"] * 0.21 * max(0.5, min(1.6, behind_factor)), 2)

        proj["expected_rbi"] = max(0.05, expected_rbi)
        proj["expected_runs"] = max(0.05, expected_runs)
        proj["expected_hrr"] = round(proj["expected_hits"] + proj["expected_runs"] + proj["expected_rbi"], 2)

    return lineup_projs


def estimate_team_runs(lineup_projs):
    if not lineup_projs:
        return 4.3
    avg_quality = sum(p["offense_quality"] for p in lineup_projs) / len(lineup_projs)
    league_avg_quality = (LEAGUE_AVG_XWOBA / 0.450) * 100
    runs = 4.3 * (avg_quality / league_avg_quality)
    return max(1.5, min(12, runs))


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def win_probability(team_runs, opp_runs, max_runs=15):
    p_win, p_tie = 0.0, 0.0
    for i in range(max_runs):
        pi = poisson_pmf(i, team_runs)
        for j in range(max_runs):
            pj = poisson_pmf(j, opp_runs)
            if i > j:
                p_win += pi * pj
            elif i == j:
                p_tie += pi * pj
    return p_win + p_tie * (team_runs / (team_runs + opp_runs))


def prob_over_total(away_runs, home_runs, line=8.5, max_runs=20):
    total_dist = {}
    for i in range(max_runs):
        pi = poisson_pmf(i, away_runs)
        for j in range(max_runs):
            pj = poisson_pmf(j, home_runs)
            t = i + j
            total_dist[t] = total_dist.get(t, 0) + pi * pj
    return sum(p for t, p in total_dist.items() if t > line)


def prob_both_teams_score(away_runs, home_runs):
    return (1 - poisson_pmf(0, away_runs)) * (1 - poisson_pmf(0, home_runs))


def offense_target_score(away_runs, home_runs, p_over, p_both_score, p_any_hr):
    combined_runs = away_runs + home_runs
    runs_signal = min(100, max(0, (combined_runs - 6) / (13 - 6) * 100))
    score = (runs_signal * 0.30 + p_over * 100 * 0.25 + p_both_score * 100 * 0.15 + min(100, p_any_hr * 100 * 1.4) * 0.30)
    return round(score)


def tier_for_score(score):
    if score >= 90: return "ELITE"
    if score >= 75: return "STRONG"
    if score >= 60: return "LEAN"
    return "PASS"


# ---------------------------------------------------------------------------
# STEP 5: Odds API
# ---------------------------------------------------------------------------
def fetch_moneylines():
    if not ODDS_API_KEY:
        return {}
    try:
        url = (f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds"
               f"?apiKey={ODDS_API_KEY}&regions=us&markets=h2h&oddsFormat=american")
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        events = resp.json()
    except Exception as e:
        print(f"  (Odds API request failed: {e})")
        return {}

    lines = {}
    for event in events:
        away, home = event.get("away_team"), event.get("home_team")
        bms = event.get("bookmakers", [])
        if not bms:
            continue
        market = next((m for m in bms[0].get("markets", []) if m["key"] == "h2h"), None)
        if not market:
            continue
        outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
        if away not in outcomes or home not in outcomes:
            continue

        def implied(odds):
            return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)

        a, h = implied(outcomes[away]), implied(outcomes[home])
        total = a + h
        lines[(away, home)] = {"away_prob": a / total, "home_prob": h / total, "book": bms[0].get("title", "book")}
    return lines


# ---------------------------------------------------------------------------
# PROP VALUE ENGINE
# ---------------------------------------------------------------------------
# Standard MLB book pricing for common prop lines, expressed as American odds.
# These are typical mid-market prices observed across DraftKings/FanDuel --
# they vary game-to-game and player-to-player, but these are reasonable
# starting-point expectations for devig comparison.
# Format: {market: {line: {"over": american_odds, "under": american_odds}}}
STANDARD_BOOK_LINES = {
    "hits":      {0.5: {"over": -220, "under": +175}, 1.5: {"over": +135, "under": -165}},
    "tb":        {1.5: {"over": -145, "under": +120}, 2.5: {"over": +145, "under": -175}},
    "hr":        {0.5: {"over": +320, "under": -420}},
    "k_batter":  {0.5: {"over": -155, "under": +125}, 1.5: {"over": +175, "under": -215}},
    "rbi":       {0.5: {"over": +105, "under": -130}},
    "hrr":       {2.5: {"over": -115, "under": -110}, 3.5: {"over": +175, "under": -215}},
    "k_pitcher": {4.5: {"over": -130, "under": +105}, 5.5: {"over": +105, "under": -130}, 6.5: {"over": +190, "under": -235}},
}

EDGE_THRESHOLD = 4.0  # flag as a play if model edge >= this percentage


def american_to_prob(odds):
    """Convert American odds to implied probability (raw, not devigged)."""
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return -odds / (-odds + 100)


def devig_prob(over_odds, under_odds):
    """Devig a two-way market to get true implied probabilities."""
    over_raw = american_to_prob(over_odds)
    under_raw = american_to_prob(under_odds)
    total = over_raw + under_raw
    return over_raw / total, under_raw / total


def poisson_over(mean, line):
    """P(stat > line) using Poisson distribution around the projected mean.
    This is the same Poisson math already used for score/win% -- applying
    it to individual player counting stats is the natural extension."""
    k = int(math.floor(line))
    p_at_most_k = sum(poisson_pmf(i, max(0.001, mean)) for i in range(k + 1))
    return max(0.001, min(0.999, 1 - p_at_most_k))


def evaluate_props(name, bat_side, proj, pitcher_proj=None):
    """Evaluate all prop markets for one batter (and optionally their pitcher).
    Returns a list of value plays: {market, line, side, model_prob, book_prob, edge}"""
    plays = []

    def check(market, line, model_mean, book_key=None):
        book_key = book_key or market
        if book_key not in STANDARD_BOOK_LINES:
            return
        if line not in STANDARD_BOOK_LINES[book_key]:
            return
        book = STANDARD_BOOK_LINES[book_key][line]
        model_over = poisson_over(model_mean, line - 0.001)  # over X.5 → P(>=X+1)
        book_over_prob, book_under_prob = devig_prob(book["over"], book["under"])
        over_edge = (model_over - book_over_prob) * 100
        under_edge = ((1 - model_over) - book_under_prob) * 100
        if over_edge >= EDGE_THRESHOLD:
            plays.append({"name": name, "market": market, "line": line, "side": "OVER",
                          "model_prob": round(model_over * 100, 1),
                          "book_prob": round(book_over_prob * 100, 1),
                          "edge": round(over_edge, 1),
                          "book_odds": book["over"]})
        if under_edge >= EDGE_THRESHOLD:
            plays.append({"name": name, "market": market, "line": line, "side": "UNDER",
                          "model_prob": round((1 - model_over) * 100, 1),
                          "book_prob": round(book_under_prob * 100, 1),
                          "edge": round(under_edge, 1),
                          "book_odds": book["under"]})

    # Batter props -- only run if this is a batter call (proj has expected_hits)
    if proj.get("expected_hits") is not None:
        check("Hits", 0.5, proj["expected_hits"], "hits")
        check("Hits", 1.5, proj["expected_hits"], "hits")
        check("Total Bases", 1.5, proj["expected_tb"], "tb")
        check("Total Bases", 2.5, proj["expected_tb"], "tb")
        check("Home Run", 0.5, proj["hr_prob"] * proj["pa"], "hr")
        check("Strikeouts", 0.5, proj["expected_k"], "k_batter")
        check("Strikeouts", 1.5, proj["expected_k"], "k_batter")
        check("RBI", 0.5, proj["expected_rbi"], "rbi")
        check("H+R+RBI", 2.5, proj["expected_hrr"], "hrr")
        check("H+R+RBI", 3.5, proj["expected_hrr"], "hrr")

    # Pitcher K props (if a pitcher projection was passed in)
    if pitcher_proj:
        for line in [4.5, 5.5, 6.5]:
            book = STANDARD_BOOK_LINES["k_pitcher"].get(line)
            if not book:
                continue
            model_over = poisson_over(pitcher_proj["expected_k"], line - 0.001)
            book_over_prob, book_under_prob = devig_prob(book["over"], book["under"])
            over_edge = (model_over - book_over_prob) * 100
            under_edge = ((1 - model_over) - book_under_prob) * 100
            pname = pitcher_proj.get("name", "Pitcher")
            if over_edge >= EDGE_THRESHOLD:
                plays.append({"name": pname, "market": "Pitcher Ks", "line": line, "side": "OVER",
                              "model_prob": round(model_over * 100, 1),
                              "book_prob": round(book_over_prob * 100, 1),
                              "edge": round(over_edge, 1), "book_odds": book["over"]})
            if under_edge >= EDGE_THRESHOLD:
                plays.append({"name": pname, "market": "Pitcher Ks", "line": line, "side": "UNDER",
                              "model_prob": round((1 - model_over) * 100, 1),
                              "book_prob": round(book_under_prob * 100, 1),
                              "edge": round(under_edge, 1), "book_odds": book["under"]})

    return plays


# ---------------------------------------------------------------------------
# STEP 6: Print + collect per game
# ---------------------------------------------------------------------------
def print_game(game, bat_df, pit_df, odds_lines, all_standouts):
    venue = game["venue"]
    pf_known = venue in PARK_FACTORS
    pf_note = "" if pf_known else " [park factor: neutral fallback, venue not in table]"
    print(f"\n{'='*72}")
    print(f"{game['away_team']} @ {game['home_team']}  ({venue}){pf_note}")
    print(f"  Probables: {game['away_pitcher_name'] or 'TBD'} vs {game['home_pitcher_name'] or 'TBD'}")

    away_lineup, home_lineup, confirmed, home_plate_ump = get_lineup(game["game_pk"])
    if home_plate_ump:
        print(f"  HP Umpire: {home_plate_ump}")
    status = "CONFIRMED lineups" if confirmed else "PROVISIONAL (lineups not posted yet)"
    print(f"  Status: {status}")

    result = {
        "away_team": game["away_team"], "home_team": game["home_team"], "venue": game["venue"],
        "away_pitcher": game["away_pitcher_name"], "home_pitcher": game["home_pitcher_name"],
        "away_pitcher_id": game.get("away_pitcher_id"),
        "home_pitcher_id": game.get("home_pitcher_id"),
        "game_time": game.get("game_time"),
        "game_state": game.get("game_state", "Preview"),
        "game_detail": game.get("game_detail", ""),
        "inning": game.get("inning"),
        "inning_half": game.get("inning_half", ""),
        "away_score": game.get("away_score"),
        "home_score": game.get("home_score"),
        "confirmed": confirmed,
        "home_plate_ump": home_plate_ump,
    }
    if not confirmed:
        print("  (Run again closer to game time.)")
        return result

    home_prow = pitcher_row(pit_df, game["home_pitcher_id"]) if game["home_pitcher_id"] else None
    away_prow = pitcher_row(pit_df, game["away_pitcher_id"]) if game["away_pitcher_id"] else None

    away_pitcher_proj = project_pitcher_simple(away_prow)
    home_pitcher_proj = project_pitcher_simple(home_prow)
    if away_pitcher_proj and game.get("away_pitcher_name"):
        away_pitcher_proj["name"] = game["away_pitcher_name"]
    if home_pitcher_proj and game.get("home_pitcher_name"):
        home_pitcher_proj["name"] = game["home_pitcher_name"]

    print(f"\n  Pitching probables")
    print(f"  {'Pitcher':<24}{'Team':<6}{'xwOBA-ag':>9}{'Qual':>6}{'IP':>5}{'K':>5}{'BB':>5}{'H':>5}{'ER':>6}")
    for label, name, proj in [
        (game["away_team"], game["away_pitcher_name"], away_pitcher_proj),
        (game["home_team"], game["home_pitcher_name"], home_pitcher_proj),
    ]:
        if proj is None:
            print(f"  {name or 'TBD':<24}{label[:5]:<6}  (no Statcast data this season)")
        else:
            print(f"  {name:<24}{label[:5]:<6}{proj['xwoba_against']:>9.3f}{proj['quality']:>6}"
                  f"{proj['expected_ip']:>5.1f}{proj['expected_k']:>5.1f}{proj['expected_bb']:>5.1f}"
                  f"{proj['expected_hits_allowed']:>5.1f}{proj['expected_er']:>6.2f}")

    away_projs, home_projs = [], []
    away_names, home_names = [], []
    for label, lineup, opp_prow, store, names in [
        (game["away_team"], away_lineup, home_prow, away_projs, away_names),
        (game["home_team"], home_lineup, away_prow, home_projs, home_names),
    ]:
        for slot in lineup:
            brow = batter_row(bat_df, slot["player_id"])
            if brow is None:
                store.append(None)
                names.append(slot["name"])
                continue
            proj = project_batter_simple(
                brow, opp_prow, slot["order"],
                park_hr_factor=get_park_factor(venue, slot.get("bat_side", "R"))
            )
            store.append(proj)
            names.append(slot["name"])

        # Runs/RBI need the FULL lineup's projections at once, since each
        # batter's estimate depends on the on-base rates of hitters around
        # him in the order -- compute this only after the loop above.
        valid_projs = [p for p in store if p is not None]
        estimate_runs_rbi(valid_projs)

        print(f"\n  {label}")
        print(f"  {'#':<3}{'Batter':<20}{'B':>2}{'PA':>5}{'xwOBA':>7}{'OffQ':>5}{'H':>5}{'TB':>5}{'BB':>5}{'K':>5}{'R':>5}{'RBI':>5}{'H+R+RBI':>8}{'HR%':>6}")
        for i, (slot, proj, name) in enumerate(zip(lineup, store, names)):
            if proj is None:
                print(f"  {slot['order']:<3}{name:<20}  (no Statcast data this season)")
                continue
            flag = " <-HR" if proj["hr_prob"] >= 0.05 else ""
            bat_side = slot.get("bat_side", "?")
            print(f"  {slot['order']:<3}{name:<20}{bat_side:>2}{proj['pa']:>5.1f}{proj['xwoba']:>7.3f}{proj['offense_quality']:>5}"
                  f"{proj['expected_hits']:>5.2f}{proj['expected_tb']:>5.2f}{proj['expected_bb']:>5.2f}{proj['expected_k']:>5.2f}"
                  f"{proj['expected_runs']:>5.2f}{proj['expected_rbi']:>5.2f}{proj['expected_hrr']:>8.2f}{proj['hr_prob']*100:>5.1f}%{flag}")
            all_standouts.append({"name": name, "team": label, "bat_side": bat_side, **proj})

    away_runs = estimate_team_runs([p for p in away_projs if p is not None])
    home_runs = estimate_team_runs([p for p in home_projs if p is not None])
    away_win = win_probability(away_runs, home_runs)
    home_win = 1 - away_win

    print(f"\n  Projected score: {game['away_team']} {away_runs:.1f} - {game['home_team']} {home_runs:.1f}")
    print(f"  Model win%: {game['away_team']} {away_win*100:.1f}%  |  {game['home_team']} {home_win*100:.1f}%")

    p_over = prob_over_total(away_runs, home_runs)
    p_both_score = prob_both_teams_score(away_runs, home_runs)
    def p_no_hr(projs):
        p = 1.0
        for pr in projs:
            if pr is None:
                continue
            p *= (1 - pr["hr_prob"]) ** pr["pa"]
        return p
    p_any_hr = 1 - (p_no_hr(away_projs) * p_no_hr(home_projs))
    target = offense_target_score(away_runs, home_runs, p_over, p_both_score, p_any_hr)
    tier = tier_for_score(target)
    print(f"  Offense target score: {target} [{tier}]  (O8.5: {p_over*100:.0f}%  Both Score: {p_both_score*100:.0f}%  Any HR: {p_any_hr*100:.0f}%)")

    def build_lineup_export(lineup, projs, names):
        rows = []
        for slot, proj, name in zip(lineup, projs, names):
            if proj is None:
                continue
            rows.append({
                "order": slot["order"],
                "name": name,
                "player_id": slot["player_id"],
                "bat_side": slot.get("bat_side", "?"),
                **{k: v for k, v in proj.items() if k not in ("pa",)},
                "pa": proj["pa"],
            })
        return rows

    result.update({
        "away_runs": round(away_runs, 2), "home_runs": round(home_runs, 2),
        "away_win_pct": round(away_win, 4), "home_win_pct": round(home_win, 4),
        "p_over_8_5": round(p_over, 4), "p_both_score": round(p_both_score, 4), "p_any_hr": round(p_any_hr, 4),
        "target_score": target, "tier": tier,
        "away_pitcher_proj": away_pitcher_proj, "home_pitcher_proj": home_pitcher_proj,
        "away_lineup": build_lineup_export(away_lineup, away_projs, away_names),
        "home_lineup": build_lineup_export(home_lineup, home_projs, home_names),
    })

    line = odds_lines.get((game["away_team"], game["home_team"]))
    if line:
        away_edge = (away_win - line["away_prob"]) * 100
        home_edge = (home_win - line["home_prob"]) * 100
        print(f"  Book ({line['book']}) implied win%: {game['away_team']} {line['away_prob']*100:.1f}%  |  {game['home_team']} {line['home_prob']*100:.1f}%")
        lean_team = game["away_team"] if away_edge > home_edge else game["home_team"]
        lean_edge = max(away_edge, home_edge)
        confidence = "HIGH" if abs(lean_edge) >= 5 else "MODERATE" if abs(lean_edge) >= 2 else "LOW"
        sign = "+" if lean_edge >= 0 else ""
        print(f"  EDGE: {lean_team} {sign}{lean_edge:.1f}% vs. book  [{confidence} confidence]")
        result["edge"] = {"team": lean_team, "edge_pct": round(lean_edge, 2), "confidence": confidence}
    elif ODDS_API_KEY:
        print("  (No matching odds line for this game)")

    # --- Prop Value Section (after score/win% so all context is available) ---
    all_prop_plays = []
    for label, lineup, store, opp_pitcher_proj in [
        (game["away_team"], away_lineup, away_projs, home_pitcher_proj),
        (game["home_team"], home_lineup, home_projs, away_pitcher_proj),
    ]:
        for i, (slot, proj) in enumerate(zip(lineup, store)):
            if proj is None:
                continue
            bat_side = slot.get("bat_side", "R")
            plays = evaluate_props(slot["name"], bat_side, proj)
            all_prop_plays.extend(plays)
        if opp_pitcher_proj:
            pitcher_plays = evaluate_props("", "", {}, pitcher_proj=opp_pitcher_proj)
            all_prop_plays.extend(pitcher_plays)

    all_prop_plays.sort(key=lambda x: -x["edge"])
    if all_prop_plays:
        print(f"\n  PROP VALUE  (model edge vs. standard book pricing, >{EDGE_THRESHOLD}% threshold)")
        print(f"  {'Player':<22}{'Market':<16}{'Line':>5}{'Side':>6}{'Model%':>8}{'Book%':>7}{'Edge':>7}{'Odds':>7}")
        for p in all_prop_plays:
            psign = "+" if p["book_odds"] > 0 else ""
            print(f"  {p['name']:<22}{p['market']:<16}{p['line']:>5.1f}{p['side']:>6}"
                  f"{p['model_prob']:>7.1f}%{p['book_prob']:>6.1f}%{p['edge']:>6.1f}%{psign}{p['book_odds']:>6}")
    else:
        print(f"\n  No prop edges found above {EDGE_THRESHOLD}% threshold for this game.")

    result["prop_plays"] = all_prop_plays
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--season", type=int, default=datetime.now().year)
    parser.add_argument("--json-out", type=str, default="mlb_slate.json")
    args = parser.parse_args()

    print(f"MLB Projections for {args.date}")

    games = get_todays_games(args.date)
    if not games:
        print("No games found for this date.")
        sys.exit(0)

    bat_df, pit_df = load_season_stats(args.season)
    run_sanity_check(bat_df)

    odds_lines = fetch_moneylines()
    if not ODDS_API_KEY:
        print("(No ODDS_API_KEY set -- skipping odds/edge section.)\n")
    elif not odds_lines:
        print("(Odds API returned no usable lines.)\n")

    all_standouts = []
    game_results = []
    for game in games:
        result = print_game(game, bat_df, pit_df, odds_lines, all_standouts)
        if result:
            game_results.append(result)

    if all_standouts:
        print(f"\n{'='*72}\nSTANDOUTS (today's slate)")
        top_hr = sorted(all_standouts, key=lambda x: -x["hr_prob"])[:5]
        top_hits = sorted(all_standouts, key=lambda x: -x["expected_hits"])[:5]
        top_hrr = sorted(all_standouts, key=lambda x: -x.get("expected_hrr", 0))[:5]
        top_q = sorted(all_standouts, key=lambda x: -x["offense_quality"])[:5]
        print("\n  Top HR probability:")
        for s in top_hr:
            print(f"    {s['name']:<22}({s['team']:<4}) {s['hr_prob']*100:.1f}%")
        print("\n  Top projected hits:")
        for s in top_hits:
            print(f"    {s['name']:<22}({s['team']:<4}) {s['expected_hits']:.2f}")
        print("\n  Top projected H+R+RBI:")
        for s in top_hrr:
            print(f"    {s['name']:<22}({s['team']:<4}) {s.get('expected_hrr', 0):.2f}")
        print("\n  Best offense quality matchups:")
        for s in top_q:
            print(f"    {s['name']:<22}({s['team']:<4}) {s['offense_quality']}  (xwOBA {s['xwoba']:.3f})")

    # Collect all prop plays across every game, rank by edge, show top 15
    all_plays_slate = []
    for g in game_results:
        all_plays_slate.extend(g.get("prop_plays", []))
    all_plays_slate.sort(key=lambda x: -x["edge"])

    if all_plays_slate:
        print(f"\n{'='*72}")
        print(f"BEST PROP PLAYS (today's slate, ranked by model edge)")
        print(f"NOTE: book odds are STANDARD MARKET ESTIMATES, not live lines.")
        print(f"Verify actual line on your book before placing -- prices move.\n")
        print(f"  {'Player':<22}{'Market':<16}{'Line':>5}{'Side':>6}{'Model%':>8}{'Book%':>7}{'Edge':>7}{'Odds':>7}")
        for p in all_plays_slate[:15]:
            sign = "+" if p["book_odds"] > 0 else ""
            print(f"  {p['name']:<22}{p['market']:<16}{p['line']:>5.1f}{p['side']:>6}"
                  f"{p['model_prob']:>7.1f}%{p['book_prob']:>6.1f}%{p['edge']:>6.1f}%{sign}{p['book_odds']:>6}")

    print(f"\n{'='*72}")
    print("Model: xwOBA-anchored offense quality (log5 vs. opposing pitcher xwOBA")
    print("allowed). HR probability from Barrel% scaled against league HR/PA rate.")
    print("Score/win% from Poisson over aggregated lineup quality vs. league average.")
    print("All Savant percentile-rank columns converted to real rates via calibrated")
    print("anchor-point interpolation -- see SANITY CHECK above for verification.")
    print("Runs/RBI are LINEUP-ORDER ESTIMATES based on neighboring hitters' on-base")
    print("rates, not a full base-state simulation -- treat as directional, not exact.")

    export = {
        "generated_at": datetime.now().isoformat(timespec="minutes"),
        "date": args.date,
        "games": game_results,
        "standouts": {
            "top_hr": sorted(all_standouts, key=lambda x: -x["hr_prob"])[:10] if all_standouts else [],
        },
    }
    # run-over-run deltas: annotate lineup projections that moved since the
    # previous run (lineup changes / pitcher swaps flow through automatically)
    try:
        from pathlib import Path as _P
        prev_path = _P(__file__).parent / "data" / "mlb_slate.json"
        if prev_path.exists():
            prev = json.loads(prev_path.read_text())
            prev_pl = {}
            for pg in prev.get("games", []):
                for side in ("away_lineup", "home_lineup"):
                    for pp in pg.get(side, []) or []:
                        prev_pl[pp.get("name")] = pp
            fields = ("expected_hr", "expected_hits", "expected_tb",
                      "expected_rbi", "expected_runs", "expected_k")
            for g in export.get("games", []):
                for side in ("away_lineup", "home_lineup"):
                    for pp in g.get(side, []) or []:
                        old_p = prev_pl.get(pp.get("name"))
                        if not old_p:
                            continue
                        for fkey in fields:
                            a, b = old_p.get(fkey), pp.get(fkey)
                            if a is not None and b is not None and abs(b - a) >= max(0.12, abs(a) * 0.12):
                                pp["prev_" + fkey] = a
    except Exception:
        pass
    with open(args.json_out, "w") as f:
        json.dump(export, f, indent=2)
    print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    main()
