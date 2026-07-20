#!/usr/bin/env python3
"""
nfl_projections.py
Bracco / Hammer Betting Network — NFL projections engine (scaffold)

Mirrors the role mlb_projections.py plays for the MLB board:
this script is responsible for producing nfl_slate.json in the exact
shape nfl_target_board.html expects, then the launcher serves it locally.

Right now this is a SCAFFOLD — it ships placeholder projections so the
folder is runnable end-to-end today. Swap the marked sections for real
data pulls (schedule, EPA, injuries, weather, odds) as the model comes
together.

Usage:
    python3 nfl_projections.py
Output:
    nfl_slate.json  (written next to this script)
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

try:
    import requests
except ImportError:
    requests = None
    import urllib.request

OUT_PATH = Path(__file__).parent / "nfl_slate.json"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# ── ODDS API ─────────────────────────────────────────────────────────────
# Same key pattern as your MLB / WC2026 tools. Override via env var if you
# rotate it: export ODDS_API_KEY=...
import os
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")  # set via env var / GitHub secret -- never hardcode
ODDS_API_SPORT = "americanfootball_nfl"
ODDS_API_URL = f"https://api.the-odds-api.com/v4/sports/{ODDS_API_SPORT}/odds"
# region "us" = DK/FD/MGM etc. Add "eu" if you want Pinnacle included
# (matches the pattern from your WC2026 live betting tool), e.g. "us,eu"
ODDS_API_REGIONS = "us"

# ESPN's internal numeric team IDs — needed for roster/athlete endpoints
# (the scoreboard endpoint uses abbreviations, but roster/stats need these).
ESPN_TEAM_IDS = {
    "ARI": 22, "ATL": 1, "BAL": 33, "BUF": 2, "CAR": 29, "CHI": 3, "CIN": 4,
    "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GB": 9, "HOU": 34, "IND": 11,
    "JAX": 30, "KC": 12, "LV": 13, "LAC": 24, "LAR": 14, "MIA": 15, "MIN": 16,
    "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21, "PIT": 23, "SF": 25,
    "SEA": 26, "TB": 27, "TEN": 10, "WSH": 28,
}

_roster_cache = {}  # team_abbr -> {position_abbr: [(name, athlete_id), ...]} in roster order
_roster_by_id_cache = {}  # team_abbr -> {athlete_id: name}


def fetch_team_roster(team_abbr):
    """Returns {position_abbr: [(name, athlete_id), ...]}, ordered as ESPN
    lists them. NOTE: roster order is NOT reliably depth-chart order (can
    be closer to alphabetical/jersey-number) — use fetch_depth_chart() for
    actual starter ranking. This is kept as a fallback and as a name lookup
    by ID for depth chart results (which only give athlete IDs via $ref).
    Cached per team for the run."""
    if team_abbr in _roster_cache:
        return _roster_cache[team_abbr]

    team_id = ESPN_TEAM_IDS.get(team_abbr)
    if not team_id:
        _roster_cache[team_abbr] = {}
        _roster_by_id_cache[team_abbr] = {}
        return {}

    url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"
    by_pos = {}
    by_id = {}
    try:
        data = _http_get_json(url)
        for group in data.get("athletes", []):
            for athlete in group.get("items", []):
                pos = (athlete.get("position") or {}).get("abbreviation")
                name = athlete.get("fullName") or athlete.get("displayName")
                aid = athlete.get("id")
                if aid:
                    by_id[str(aid)] = name
                if pos:
                    by_pos.setdefault(pos, []).append((name, aid))
    except Exception:
        _roster_failures.append(team_abbr)

    _roster_cache[team_abbr] = by_pos
    _roster_by_id_cache[team_abbr] = by_id
    return by_pos


_roster_failures = []
_depth_chart_failures = []
_depth_chart_cache = {}


def _season_year():
    # ESPN's depth chart "season" param is the year the season STARTS
    # (e.g. the 2025 season runs Sep 2025-Feb 2026, still referenced as
    # 2025). Treat Mar-Dec as "current year's upcoming/active season",
    # Jan-Feb as "previous year's season still wrapping up".
    now = datetime.now()
    return now.year if now.month >= 3 else now.year - 1


def fetch_depth_chart(team_abbr):
    """Returns {position_abbr: [athlete_id, ...]} in starter-to-backup
    rank order, or {} on failure. Position keys are lowercase per ESPN's
    schema (e.g. 'qb', 'rb', 'wr', 'te'). Cached per team for the run."""
    if team_abbr in _depth_chart_cache:
        return _depth_chart_cache[team_abbr]

    team_id = ESPN_TEAM_IDS.get(team_abbr)
    if not team_id:
        _depth_chart_cache[team_abbr] = {}
        return {}

    url = (f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
           f"seasons/{_season_year()}/teams/{team_id}/depthcharts")
    by_pos = {}
    try:
        data = _http_get_json(url)
        for chart in data.get("items", []):
            for pos_key, pos_data in (chart.get("positions") or {}).items():
                ids = []
                for entry in pos_data.get("athletes", []):
                    ref = (entry.get("athlete") or {}).get("$ref", "")
                    # athlete IDs are the trailing numeric segment of the $ref URL
                    m = re.search(r"/athletes/(\d+)", ref)
                    if m:
                        ids.append(m.group(1))
                if ids:
                    by_pos.setdefault(pos_key.lower(), []).extend(ids)
    except Exception:
        _depth_chart_failures.append(team_abbr)

    if os.environ.get("DEBUG_DEPTH") and by_pos:
        print(f"[debug] {team_abbr} depth chart position keys: {sorted(by_pos.keys())}")

    _depth_chart_cache[team_abbr] = by_pos
    return by_pos


def _name_for_id(team_abbr, athlete_id):
    fetch_team_roster(team_abbr)  # ensures by-id cache is populated
    return _roster_by_id_cache.get(team_abbr, {}).get(str(athlete_id))


def _starter_ids_for_prefix(depth, prefix, count):
    """ESPN splits some positions into numbered slot keys (e.g. wr1/wr2/wr3
    rather than one generic 'wr' key with a ranked list) — mirrors how
    defensive linemen show up as lde/rde rather than one 'de' key. Handles
    both shapes: if multiple slot keys match, take the rank-1 athlete from
    each (e.g. wr1, wr2, wr3 starters). If only ONE key matches, that key
    likely holds a full ranked depth list for the position (e.g. a single
    'rb' key with RB1/RB2/RB3 ranked inside it) — take the top `count`
    from that list instead."""
    matched_keys = sorted(k for k in depth if k.startswith(prefix))
    if not matched_keys:
        return []

    if len(matched_keys) == 1:
        return depth[matched_keys[0]][:count]

    ids = []
    for key in matched_keys:
        lst = depth[key]
        if lst:
            ids.append(lst[0])
        if len(ids) >= count:
            break
    return ids[:count]


def fetch_starting_qb(team_abbr):
    """Returns (name, athlete_id) for the team's QB1, or (None, None)."""
    depth = fetch_depth_chart(team_abbr)
    qb_ids = _starter_ids_for_prefix(depth, "qb", 1)
    if qb_ids:
        name = _name_for_id(team_abbr, qb_ids[0])
        if name:
            return name, qb_ids[0]

    # Fallback: roster order (less reliable, but better than nothing)
    qbs = fetch_team_roster(team_abbr).get("QB", [])
    return qbs[0] if qbs else (None, None)


def fetch_skill_starters(team_abbr):
    """Returns a flat list of (name, athlete_id, pos) for RB1-2, WR1-3, TE1-2.
    Uses depth chart ranking when available, falls back to roster order."""
    depth = fetch_depth_chart(team_abbr)
    roster = fetch_team_roster(team_abbr)
    starters = []

    for depth_prefix, roster_pos, count in (("rb", "RB", 2), ("wr", "WR", 3), ("te", "TE", 2)):
        ids = _starter_ids_for_prefix(depth, depth_prefix, count)
        if ids:
            for aid in ids:
                name = _name_for_id(team_abbr, aid)
                if name:
                    starters.append((name, aid, roster_pos))
        else:
            # Fallback: roster order
            for name, aid in roster.get(roster_pos, [])[:count]:
                starters.append((name, aid, roster_pos))

    return starters


def _find_stat_category(node, target_names):
    """Recursively search a JSON blob for a dict that looks like a stat
    category (has a 'name' in target_names and a 'stats' list) — handles
    ESPN's payload nesting varying by endpoint/sport/version without
    assuming a fixed shape."""
    if isinstance(node, dict):
        name = node.get("name") or node.get("displayName")
        if name and str(name).lower() in target_names and isinstance(node.get("stats"), list):
            return node
        for v in node.values():
            found = _find_stat_category(v, target_names)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_stat_category(item, target_names)
            if found:
                return found
    return None


def fetch_qb_season_stats(athlete_id):
    """Pulls season passing stats for a QB. Returns dict or None on failure."""
    if not athlete_id:
        return None
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/overview"
    try:
        data = _http_get_json(url)
        stat_block = _find_stat_category(data, {"passing"})
        if not stat_block:
            return None

        def stat(*names):
            for s in stat_block.get("stats", []):
                if not isinstance(s, dict):
                    continue
                if s.get("name") in names or s.get("shortDisplayName") in names:
                    try:
                        return float(s.get("value"))
                    except (TypeError, ValueError):
                        continue
            return None

        completions = stat("completions", "CMP")
        attempts = stat("passingAttempts", "ATT")
        yards = stat("passingYards", "YDS")
        tds = stat("passingTouchdowns", "TD")
        ints = stat("interceptions", "INT")
        games = stat("gamesPlayed", "GP") or 1
        rating = stat("QBRating", "passerRating", "RTG")

        if not attempts:
            return None

        return {
            "comp_pct": round((completions or 0) / attempts * 100, 1),
            "pass_yds": round((yards or 0) / games, 1),
            "pass_td": round((tds or 0) / games, 2),
            "int": round((ints or 0) / games, 2),
            "rating": round(rating, 1) if rating else None,
        }
    except Exception as e:
        _qb_stat_failures.append(str(athlete_id))
        return None

_qb_stat_failures = []


def fetch_skill_season_stats(athlete_id):
    """Pulls receiving + rushing season-to-date for a skill player.
    Returns dict or None on failure."""
    if not athlete_id:
        return None
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/{athlete_id}/overview"
    try:
        data = _http_get_json(url)
        rec_block = _find_stat_category(data, {"receiving"})
        rush_block = _find_stat_category(data, {"rushing"})
        if not rec_block and not rush_block:
            return None

        def stat(block, *names):
            if not block:
                return None
            for s in block.get("stats", []):
                if not isinstance(s, dict):
                    continue
                if s.get("name") in names or s.get("shortDisplayName") in names:
                    try:
                        return float(s.get("value"))
                    except (TypeError, ValueError):
                        continue
            return None

        targets = stat(rec_block, "receivingTargets", "TGTS")
        receptions = stat(rec_block, "receptions", "REC")
        rec_yds = stat(rec_block, "receivingYards", "YDS")
        rec_td = stat(rec_block, "receivingTouchdowns", "TD") or 0
        rush_att = stat(rush_block, "rushingAttempts", "ATT")
        rush_yds = stat(rush_block, "rushingYards", "YDS")
        rush_td = stat(rush_block, "rushingTouchdowns", "TD") or 0
        games = stat(rec_block, "gamesPlayed", "GP") or stat(rush_block, "gamesPlayed", "GP") or 1

        if targets is None and rush_att is None:
            return None

        return {
            "targets": round((targets or 0) / games, 1),
            "rec": round((receptions or 0) / games, 1),
            "rec_yds": round((rec_yds or 0) / games, 1),
            "rush_att": round((rush_att or 0) / games, 1),
            "rush_yds": round((rush_yds or 0) / games, 1),
            "td_per_game": round((rec_td + rush_td) / games, 2),
        }
    except Exception as e:
        _skill_stat_failures.append(str(athlete_id))
        return None


_skill_stat_failures = []

# Canonical full names keyed by abbreviation — must match TEAMS in
# nfl_target_board.html exactly so logos/colors resolve correctly.
NFL_TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons", "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns", "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos", "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts", "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "LV": "Las Vegas Raiders", "LAC": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "MIA": "Miami Dolphins", "MIN": "Minnesota Vikings",
    "NE": "New England Patriots", "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles", "PIT": "Pittsburgh Steelers",
    "SF": "San Francisco 49ers", "SEA": "Seattle Seahawks", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WSH": "Washington Commanders",
}

# Placeholder fallback used only if the live pull fails (offline, offseason
# with no week param resolving, ESPN hiccup, etc.) so the script still
# produces a runnable nfl_slate.json.
FALLBACK_GAMES = [
    {
        "away_team": "Kansas City Chiefs", "away_abbr": "KC",
        "home_team": "Buffalo Bills", "home_abbr": "BUF",
        "venue": "Highmark Stadium",
        "game_time": "2026-09-11T00:20:00Z",
        "game_state": "Preview",
    },
]


def _http_get_json(url):
    if requests is not None:
        res = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        return res.json()
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode())


# ──────────────────────────────────────────────────────────────────────────
# STEP 1 — SCHEDULE / GAMES
# Pulls the current week's slate from ESPN's public scoreboard endpoint.
# Pass a week number as the first CLI arg to pull a specific week instead
# of whatever ESPN considers "current":
#   python3 nfl_projections.py 3
# ──────────────────────────────────────────────────────────────────────────
def fetch_schedule(week=None, season=None, seasontype=2):
    url = ESPN_SCOREBOARD
    params = []
    if week:
        params.append(f"week={week}")
    if season:
        params.append(f"dates={season}")
        params.append(f"seasontype={seasontype}")
    if params:
        url += "?" + "&".join(params)

    try:
        data = _http_get_json(url)
    except Exception as e:
        print(f"⚠️  ESPN scoreboard pull failed ({e}) — using fallback game.")
        return FALLBACK_GAMES

    games = []
    for event in data.get("events", []):
        comp = (event.get("competitions") or [None])[0]
        if not comp:
            continue
        competitors = comp.get("competitors", [])
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        if not away or not home:
            continue

        away_abbr = away["team"]["abbreviation"]
        home_abbr = home["team"]["abbreviation"]
        venue = (comp.get("venue") or {}).get("fullName", "")

        status = (event.get("status") or {}).get("type", {})
        state = status.get("state")  # 'pre' | 'in' | 'post'
        game_state = {"pre": "Preview", "in": "Live", "post": "Final"}.get(state, "Preview")

        game = {
            "away_team": NFL_TEAM_NAMES.get(away_abbr, away["team"].get("displayName")),
            "away_abbr": away_abbr,
            "home_team": NFL_TEAM_NAMES.get(home_abbr, home["team"].get("displayName")),
            "home_abbr": home_abbr,
            "venue": venue,
            "game_time": event.get("date"),
            "game_state": game_state,
            "event_id": event.get("id"),
        }
        if state == "in":
            game["quarter"] = (event.get("status") or {}).get("period")
            game["away_score"] = away.get("score")
            game["home_score"] = home.get("score")
        elif state == "post":
            game["away_score"] = away.get("score")
            game["home_score"] = home.get("score")

        games.append(game)

    if not games:
        print("⚠️  ESPN returned no games for this query — using fallback game.")
        return FALLBACK_GAMES

    return games


# ──────────────────────────────────────────────────────────────────────────
# ODDS — pulled from The Odds API, matched to ESPN games by team name
# ──────────────────────────────────────────────────────────────────────────
def american_to_implied(price):
    if price is None:
        return None
    price = float(price)
    if price > 0:
        return 100 / (price + 100)
    return abs(price) / (abs(price) + 100)


def devig_pair(price_a, price_b):
    """Two-way devig: normalize implied probs so they sum to 1."""
    ia, ib = american_to_implied(price_a), american_to_implied(price_b)
    if ia is None or ib is None:
        return None, None
    total = ia + ib
    if total <= 0:
        return None, None
    return ia / total, ib / total


def fetch_odds_for_slate():
    """Returns {(away_full_name, home_full_name): {...lines...}} or {} on failure."""
    if not ODDS_API_KEY:
        return {}
    url = (
        f"{ODDS_API_URL}?apiKey={ODDS_API_KEY}&regions={ODDS_API_REGIONS}"
        "&markets=h2h,spreads,totals&oddsFormat=american"
    )
    try:
        data = _http_get_json(url)
    except Exception as e:
        print(f"⚠️  Odds API pull failed ({e}) — proceeding without live lines.")
        return {}

    odds_map = {}
    for event in data:
        away_name = event.get("away_team")
        home_name = event.get("home_team")
        if not away_name or not home_name:
            continue

        # Average across books for a simple consensus line rather than
        # trusting a single book — swap to a preferred book key if you'd
        # rather pin to one (e.g. "pinnacle" or "draftkings").
        h2h_away, h2h_home = [], []
        spread_away_pt, spread_home_pt = None, None
        spread_away_price, spread_home_price = [], []
        total_pt = None
        over_price, under_price = [], []

        for bk in event.get("bookmakers", []):
            for mkt in bk.get("markets", []):
                if mkt["key"] == "h2h":
                    for o in mkt["outcomes"]:
                        if o["name"] == away_name:
                            h2h_away.append(o["price"])
                        elif o["name"] == home_name:
                            h2h_home.append(o["price"])
                elif mkt["key"] == "spreads":
                    for o in mkt["outcomes"]:
                        if o["name"] == away_name:
                            spread_away_pt = o.get("point", spread_away_pt)
                            spread_away_price.append(o["price"])
                        elif o["name"] == home_name:
                            spread_home_pt = o.get("point", spread_home_pt)
                            spread_home_price.append(o["price"])
                elif mkt["key"] == "totals":
                    for o in mkt["outcomes"]:
                        if o["name"] == "Over":
                            total_pt = o.get("point", total_pt)
                            over_price.append(o["price"])
                        elif o["name"] == "Under":
                            under_price.append(o["price"])

        def avg(lst):
            return sum(lst) / len(lst) if lst else None

        odds_map[(away_name, home_name)] = {
            "ml_away": avg(h2h_away),
            "ml_home": avg(h2h_home),
            "spread_away_pt": spread_away_pt,
            "spread_home_pt": spread_home_pt,
            "spread_away_price": avg(spread_away_price),
            "spread_home_price": avg(spread_home_price),
            "total_pt": total_pt,
            "over_price": avg(over_price),
            "under_price": avg(under_price),
            "book_count": len(event.get("bookmakers", [])),
        }

    return odds_map


# ──────────────────────────────────────────────────────────────────────────
# MODEL — ESPN's own win-probability predictor (FPI-based)
# This is the independent projection edge is computed against. It's not
# Mike's own model — it's ESPN's — but it IS genuinely independent of the
# sportsbook's number, which is what makes a real edge calc possible instead
# of just diffing the market against itself. Swap this out once a real
# power-rating / EPA model exists; until then this is a legitimate stand-in.
# ──────────────────────────────────────────────────────────────────────────
_predictor_failures = []


def fetch_espn_predictor(event_id):
    """Returns home team's model win probability (0-1), or None on failure."""
    if not event_id:
        return None
    url = (f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
           f"events/{event_id}/competitions/{event_id}/predictor")
    try:
        data = _http_get_json(url)
        home_proj = (data.get("homeTeam") or {}).get("gameProjection")
        if home_proj is None:
            return None
        return float(home_proj) / 100.0
    except Exception:
        _predictor_failures.append(str(event_id))
        return None


# ──────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────
# OPPONENT DEFENSE STRENGTH — used to matchup-adjust QB/skill projections
# Uses points allowed per game vs league average as the defense-strength
# signal. Pulled from the league standings endpoint (one call for all 32
# teams, rather than 32 separate per-team detail calls that turned out to
# return an empty record object during the offseason). This is a blunt
# instrument compared to a real pass/rush-defense-specific model, but it's
# a genuine, ESPN-backed adjustment. Effect is dampened 50% so one
# aggregate number doesn't swing projections too hard.
# ──────────────────────────────────────────────────────────────────────────
LEAGUE_AVG_PTS_AGAINST = 22.0
_defense_cache = {}
_defense_failures = []
_standings_cache = {}  # season -> {team_abbr: pts_against}


def fetch_standings_pts_against(season=None):
    if season in _standings_cache:
        return _standings_cache[season]

    url = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
    if season:
        url += f"?season={season}"

    by_team = {}
    try:
        data = _http_get_json(url)
        # ESPN nests standings under conference/division groups -- walk
        # whatever grouping shape comes back rather than assuming exactly
        # one level of nesting.
        groups = data.get("children") or [data]
        entries = []
        for g in groups:
            entries.extend(((g.get("standings") or {}).get("entries")) or [])

        if os.environ.get("DEBUG_DEF") and entries:
            sample_stats = [s.get("name") for s in entries[0].get("stats", [])]
            print(f"[debug] standings season={season or 'current'} "
                  f"entries={len(entries)} sample stat names: {sample_stats}")
        elif os.environ.get("DEBUG_DEF"):
            print(f"[debug] standings season={season or 'current'} "
                  f"NO entries found. top-level keys: {list(data.keys())}")

        for entry in entries:
            abbr = (entry.get("team") or {}).get("abbreviation")
            stats = entry.get("stats", [])
            pa = next((s.get("value") for s in stats
                       if s.get("name") in ("pointsAgainst", "avgPointsAgainst")), None)
            if abbr and pa:
                by_team[abbr] = float(pa)
    except Exception:
        pass

    _standings_cache[season] = by_team
    return by_team


def fetch_defense_factor(team_abbr):
    """Returns a multiplier: >1.0 means this team's defense is below
    average (good for the opposing offense), <1.0 means above average
    (bad for the opposing offense). 1.0 on failure (no adjustment).

    Falls back to last season's final standings if the current season has
    no data yet (offseason)."""
    if team_abbr in _defense_cache:
        return _defense_cache[team_abbr]

    factor = 1.0
    try:
        pts_against_map = fetch_standings_pts_against()
        pts_against = pts_against_map.get(team_abbr)
        if not pts_against:
            pts_against_map = fetch_standings_pts_against(season=_season_year() - 1)
            pts_against = pts_against_map.get(team_abbr)
        if pts_against:
            # "pointsAgainst" from standings is often season-total, not
            # per-game -- normalize using games played if we can infer it,
            # otherwise treat values >100 as season totals over ~17 games.
            per_game = pts_against / 17 if pts_against > 100 else pts_against
            raw_factor = per_game / LEAGUE_AVG_PTS_AGAINST
            factor = 1 + (raw_factor - 1) * 0.5  # dampen to 50% effect
        else:
            _defense_failures.append(team_abbr)
    except Exception:
        _defense_failures.append(team_abbr)

    _defense_cache[team_abbr] = factor
    return factor



# ──────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────
# STEP 2 — QB PROJECTIONS
# Pulls real season-to-date passing stats for the team's QB1 off ESPN, then
# adjusts for the upcoming opponent's defense (points allowed/game vs
# league average, dampened 50%). "epa_db" remains a proxy derived from
# passer rating, not real EPA — true EPA needs play-by-play data.
# ──────────────────────────────────────────────────────────────────────────
LEAGUE_AVG_QB = {"comp_pct": 64.5, "pass_yds": 220, "pass_td": 1.4, "int": 0.7, "rating": 88.0}


def project_qb(team_abbr, opponent_abbr=None):
    name, athlete_id = fetch_starting_qb(team_abbr)
    stats = fetch_qb_season_stats(athlete_id) if athlete_id else None
    if not stats:
        stats = dict(LEAGUE_AVG_QB)  # fallback so the field never breaks the dashboard

    def_factor = fetch_defense_factor(opponent_abbr) if opponent_abbr else 1.0

    rating = stats.get("rating") or LEAGUE_AVG_QB["rating"]
    rating_adj = round(rating * (1 + (def_factor - 1) * 0.6), 1)  # rating moves less than counting stats
    quality = round(max(0, min(50, (rating_adj - 70) / 1.2)))
    epa_db = round((rating_adj - 85) / 100, 2)  # proxy only — not real EPA, see note above

    return {
        "name": name,
        "quality": quality,
        "comp_pct": stats.get("comp_pct", LEAGUE_AVG_QB["comp_pct"]),
        "pass_yds": round(stats.get("pass_yds", LEAGUE_AVG_QB["pass_yds"]) * def_factor, 1),
        "pass_td": round(stats.get("pass_td", LEAGUE_AVG_QB["pass_td"]) * def_factor, 2),
        "int": stats.get("int", LEAGUE_AVG_QB["int"]),
        "rating": rating_adj,
        "epa_db": epa_db,
        "_matchup_factor": round(def_factor, 3) if opponent_abbr else None,
    }


# ──────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────
# STEP 3 — SKILL PLAYER PROJECTIONS
# Pulls real season-to-date usage for each team's RB1-2, WR1-3, TE1-2, then
# applies the same opponent defense adjustment as QBs. Targets/receptions
# (usage) are left unadjusted — defense affects efficiency/yards more than
# how often a player gets the ball — while yards/TD-rate/fantasy points
# scale with the matchup.
# ──────────────────────────────────────────────────────────────────────────
def project_skill_players(team_abbr, opponent_abbr=None):
    starters = fetch_skill_starters(team_abbr)
    def_factor = fetch_defense_factor(opponent_abbr) if opponent_abbr else 1.0
    players = []
    for name, athlete_id, pos in starters:
        if not name:
            continue
        stats = fetch_skill_season_stats(athlete_id)
        if not stats:
            continue  # skip rather than show a fake stub player

        rec_yds = round(stats["rec_yds"] * def_factor, 1)
        rush_yds = round(stats["rush_yds"] * def_factor, 1)
        td_per_game_adj = stats["td_per_game"] * def_factor
        td_prob = min(0.85, td_per_game_adj * 0.65)
        fpts = round(
            stats["rec"] * 1.0 +            # PPR, usage left unadjusted
            rec_yds * 0.1 +
            rush_yds * 0.1 +
            td_per_game_adj * 6.0,
            1,
        )
        players.append({
            "name": name,
            "pos": pos,
            "player_id": athlete_id,
            "targets": stats["targets"],
            "rec": stats["rec"],
            "rec_yds": rec_yds,
            "rush_yds": rush_yds,
            "rush_att": stats["rush_att"],
            "td_prob": round(td_prob, 2),
            "fpts": fpts,
        })
    return players


# ──────────────────────────────────────────────────────────────────────────
# STEP 4 — MARKET / EDGE CALC
# away_win_pct/home_win_pct/p_cover/p_over are still the devigged MARKET
# probability — there's no independent spread/total model yet, so those
# specific numbers will keep mirroring the book. BUT edge_pct/p_ml_edge are
# now real: computed as ESPN's predictor (FPI) win probability minus the
# market's devigged win probability. When the two disagree, that's an
# actual signal, not market-vs-itself noise.
# ──────────────────────────────────────────────────────────────────────────
def calc_market_fields(away_team, home_team, odds=None, model_home_win_pct=None):
    if not odds:
        return {
            "away_win_pct": 0.50,
            "home_win_pct": 0.50,
            "p_cover": 0.50,
            "p_over": 0.50,
            "p_ml_edge": 0.0,
            "away_pts": 21.0,
            "home_pts": 21.0,
            "edge": None,
        }

    market_away_win, market_home_win = devig_pair(odds.get("ml_away"), odds.get("ml_home"))
    market_away_win = market_away_win if market_away_win is not None else 0.50
    market_home_win = market_home_win if market_home_win is not None else 0.50

    p_home_cover, _ = devig_pair(odds.get("spread_home_price"), odds.get("spread_away_price"))
    p_cover = p_home_cover if p_home_cover is not None else 0.50

    p_over, _ = devig_pair(odds.get("over_price"), odds.get("under_price"))
    p_over = p_over if p_over is not None else 0.50

    total_pt = odds.get("total_pt")
    home_spread_pt = odds.get("spread_home_pt")
    if total_pt is not None and home_spread_pt is not None:
        home_pts = (total_pt / 2) - (home_spread_pt / 2)
        away_pts = total_pt - home_pts
    else:
        home_pts, away_pts = 21.0, 21.0

    # Default: report the market's own probability (status quo)
    away_win_pct, home_win_pct = market_away_win, market_home_win
    p_ml_edge = 0.0
    edge = None

    if model_home_win_pct is not None:
        # The model IS the projection now — report it, not the market's number
        home_win_pct = model_home_win_pct
        away_win_pct = 1 - model_home_win_pct

        home_edge = model_home_win_pct - market_home_win
        away_edge = (1 - model_home_win_pct) - market_away_win

        if abs(home_edge) >= abs(away_edge):
            best_edge, best_team = home_edge, home_team
        else:
            best_edge, best_team = away_edge, away_team

        p_ml_edge = round(abs(best_edge), 3)
        confidence = "HIGH" if p_ml_edge >= 0.07 else "MODERATE" if p_ml_edge >= 0.03 else "LOW"
        if p_ml_edge >= 0.01:
            edge = {
                "team": best_team,
                "edge_pct": round(best_edge * 100, 1),
                "confidence": confidence,
            }

    return {
        "away_win_pct": round(away_win_pct, 3),
        "home_win_pct": round(home_win_pct, 3),
        "p_cover": round(p_cover, 3),
        "p_over": round(p_over, 3),
        "p_ml_edge": p_ml_edge,
        "away_pts": round(away_pts, 1),
        "home_pts": round(home_pts, 1),
        "edge": edge,
        "_lines": {
            "spread": home_spread_pt,
            "total": total_pt,
            "ml_away": odds.get("ml_away"),
            "ml_home": odds.get("ml_home"),
            "books": odds.get("book_count"),
        },
    }


def build_game(raw, odds_map=None):
    odds = (odds_map or {}).get((raw["away_team"], raw["home_team"]))
    model_home_win_pct = fetch_espn_predictor(raw.get("event_id"))
    market = calc_market_fields(raw["away_team"], raw["home_team"], odds, model_home_win_pct)
    away_qb_proj = project_qb(raw["away_abbr"], raw["home_abbr"])
    home_qb_proj = project_qb(raw["home_abbr"], raw["away_abbr"])
    game = {
        "away_team": raw["away_team"],
        "home_team": raw["home_team"],
        "venue": raw["venue"],
        "game_time": raw["game_time"],
        "game_state": raw.get("game_state", "Preview"),
        "confirmed": True,
        "tier": "LEAN",
        "target_score": 50,
        "referee": None,
        "away_qb": away_qb_proj.get("name"),
        "home_qb": home_qb_proj.get("name"),
        "away_qb_proj": away_qb_proj,
        "home_qb_proj": home_qb_proj,
        "away_skill": project_skill_players(raw["away_abbr"], raw["home_abbr"]),
        "home_skill": project_skill_players(raw["home_abbr"], raw["away_abbr"]),
        **market,
    }
    if "away_score" in raw:
        game["away_score"] = raw["away_score"]
    if "home_score" in raw:
        game["home_score"] = raw["home_score"]
    if "quarter" in raw:
        game["quarter"] = raw["quarter"]
    return game


def main():
    week = None
    if len(sys.argv) > 1:
        try:
            week = int(sys.argv[1])
        except ValueError:
            print(f"Ignoring unrecognized arg '{sys.argv[1]}' — expected a week number.")

    raw_games = fetch_schedule(week=week)
    print("Fetching odds...")
    odds_map = fetch_odds_for_slate()
    if odds_map:
        print(f"  Matched odds available for {len(odds_map)} games.")

    print(f"Fetching QB/skill/predictor data for {len(raw_games)} games "
          f"(parallelized — this is the slow part)...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        games = list(pool.map(lambda g: build_game(g, odds_map), raw_games))

    slate = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "games": games,
    }
    OUT_PATH.write_text(json.dumps(slate, indent=2))
    print(f"Wrote {len(games)} games to {OUT_PATH}")
    if _qb_stat_failures:
        print(f"⚠️  Couldn't pull season stats for {len(_qb_stat_failures)} QB(s) "
              f"(falling back to league-average) — IDs: {', '.join(_qb_stat_failures)}")
    if _skill_stat_failures:
        print(f"⚠️  Couldn't pull season stats for {len(_skill_stat_failures)} skill player(s) "
              f"(omitted from skill tables) — IDs: {', '.join(_skill_stat_failures)}")
    if _roster_failures:
        print(f"⚠️  Roster pull failed for {len(_roster_failures)} team(s): {', '.join(_roster_failures)}")
    if _depth_chart_failures:
        print(f"⚠️  Depth chart pull failed for {len(_depth_chart_failures)} team(s) "
              f"(fell back to roster order, may show backups): {', '.join(_depth_chart_failures)}")
    if _defense_failures:
        print(f"⚠️  Defense-strength pull failed for {len(_defense_failures)} team(s) "
              f"(no matchup adjustment applied for those): {', '.join(set(_defense_failures))}")
    if _predictor_failures:
        print(f"⚠️  ESPN predictor pull failed for {len(_predictor_failures)} game(s) "
              f"(edge falls back to None for those) — event IDs: {', '.join(_predictor_failures)}")


if __name__ == "__main__":
    main()
