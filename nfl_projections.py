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

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

def _today_et():
    # Display the slate's date in US Eastern so it doesn't show "tomorrow"
    # after 8pm ET (when the GitHub runner's UTC clock has already rolled over).
    now = datetime.now(_ET) if _ET else datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")
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
            "gp": games,
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
            "gp": games,
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
        _venue_obj = comp.get("venue") or {}
        venue = _venue_obj.get("fullName", "")
        indoor = bool(_venue_obj.get("indoor"))
        _addr = _venue_obj.get("address") or {}
        venue_city = _addr.get("city")
        venue_state = _addr.get("state")

        status = (event.get("status") or {}).get("type", {})
        state = status.get("state")  # 'pre' | 'in' | 'post'
        game_state = {"pre": "Preview", "in": "Live", "post": "Final"}.get(state, "Preview")

        game = {
            "away_team": NFL_TEAM_NAMES.get(away_abbr, away["team"].get("displayName")),
            "away_abbr": away_abbr,
            "home_team": NFL_TEAM_NAMES.get(home_abbr, home["team"].get("displayName")),
            "home_abbr": home_abbr,
            "venue": venue,
            "indoor": indoor,
            "venue_city": venue_city,
            "venue_state": venue_state,
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


# ── DEFENSE-VS-POSITION (DvP) ──────────────────────────────────────────────
# Pulls per-team yards allowed to each position (QB/RB/WR/TE) from ESPN's
# team stat splits. Produces a per-position matchup grade: A+ = this defense
# is generous to that position (great spot), F = shuts it down.
# Falls back to prior season in the offseason. Team-level factor stays as a
# secondary signal; DvP is the position-specific layer on top.
_dvp_cache = {}

def fetch_dvp(season=None):
    """Returns {team_abbr: {'QB': ypg, 'RB': ypg, 'WR': ypg, 'TE': ypg}}
    of yards allowed per game to each position. Empty on failure."""
    season = season or _season_year()
    if season in _dvp_cache:
        return _dvp_cache[season]
    out = {}
    try:
        # ESPN opponent stats: passing yards allowed proxies QB/WR/TE,
        # rushing yards allowed proxies RB. We split passing into WR/TE via
        # league-average target share since ESPN doesn't break it out cleanly.
        url = (f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/"
               f"statistics/byteam?season={season}&seasontype=2")
        data = _http_get_json(url)

        teams = (data.get("teams") or
                 data.get("stats", {}).get("teams") or [])
        for t in teams:
            team = (t.get("team", {}) or {}).get("abbreviation")
            if not team:
                continue
            cats = {}
            for cat in t.get("categories", t.get("stats", [])):
                for s in cat.get("stats", cat.get("splits", [])):
                    nm = (s.get("name") or s.get("abbreviation") or "").lower()
                    val = s.get("perGameValue") or s.get("value")
                    if val is not None:
                        cats[nm] = float(val)
            pass_ypg = cats.get("passingyardsallowed") or cats.get("passingyards")
            rush_ypg = cats.get("rushingyardsallowed") or cats.get("rushingyards")
            if pass_ypg or rush_ypg:
                out[team] = {
                    "QB": pass_ypg or 220,
                    "WR": (pass_ypg or 220) * 0.62,   # ~62% of pass yards to WRs
                    "TE": (pass_ypg or 220) * 0.20,   # ~20% to TEs
                    "RB": rush_ypg or 110,
                }
    except Exception as e:
        print(f"  (DvP fetch failed for {season}: {e})")
    _dvp_cache[season] = out
    return out


# league baselines for grading (yards allowed per game, per position)
DVP_BASE = {"QB": 220.0, "WR": 136.0, "TE": 44.0, "RB": 110.0}

def dvp_grade(opponent_abbr, pos):
    """Letter grade for how favorable `opponent`'s defense is to `pos`.
    Higher yards allowed vs baseline = better matchup = higher grade."""
    pos = (pos or "").upper()
    if pos not in DVP_BASE:
        return None
    table = fetch_dvp()
    if not table or opponent_abbr not in table:
        table = fetch_dvp(_season_year() - 1)   # offseason fallback
    row = table.get(opponent_abbr)
    if not row or pos not in row:
        return None
    ratio = row[pos] / DVP_BASE[pos]            # >1 = generous defense
    # map ratio to grade: +12% or more allowed = A+, -12% or less = F
    scale = [(1.12, "A+"), (1.07, "A"), (1.03, "B+"), (0.99, "B"),
             (0.95, "C+"), (0.91, "C"), (0.86, "D"), (0.0, "F")]
    for thr, g in scale:
        if ratio >= thr:
            return g
    return "F"


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
# ── PRIOR-SEASON STATS + BLENDING ────────────────────────────────────────
# Early-season per-game stats are tiny-sample noise (one blowout skews
# everything). Fix: blend current season with last season's per-game rates
# using shrinkage: blended = (gp*current + K*prior) / (gp + K).
# Week 1: ~all prior. Week 5: ~50/50. Week 10+: current dominates.
from datetime import date as _date
_today = _date.today()
PRIOR_SEASON = _today.year - 1 if _today.month >= 3 else _today.year - 2
K_RATE = 4      # shrinkage for efficiency-ish rates (yards, TDs, rating)
K_USAGE = 2     # usage stabilizes faster (targets/carries are coach decisions)
LEAGUE_YPT = 7.6    # league yards per target
LEAGUE_YPA = 4.3    # league yards per rush attempt
LEAGUE_CATCH = 0.655
TD_PER_CARRY = 0.025
TD_PER_TARGET = 0.045

INJURY_STATUS = None  # lazy-loaded league-wide {athlete_id: status}

def fetch_injuries():
    """League-wide injury statuses from ESPN's free feed.
    Returns {athlete_id_str: 'Out'|'Doubtful'|'Questionable'|...}."""
    global INJURY_STATUS
    if INJURY_STATUS is not None:
        return INJURY_STATUS
    out = {}
    try:
        data = _http_get_json("https://site.api.espn.com/apis/site/v2/sports/football/nfl/injuries")

        def walk(node):
            if isinstance(node, dict):
                ath = node.get("athlete")
                status = node.get("status")
                if isinstance(ath, dict) and ath.get("id") and status:
                    s = status if isinstance(status, str) else (status.get("name") or status.get("type", {}).get("name"))
                    if s:
                        out[str(ath["id"])] = s
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
    except Exception as e:
        print(f"  (injury feed unavailable: {e})")
    INJURY_STATUS = out
    if out:
        n_out = sum(1 for s in out.values() if s.lower() in ("out", "injured reserve", "ir", "doubtful"))
        print(f"Injuries: {len(out)} statuses loaded ({n_out} out/doubtful)")
    return out


def player_status(athlete_id):
    s = fetch_injuries().get(str(athlete_id))
    if not s:
        return None
    sl = s.lower()
    if sl in ("out", "injured reserve", "ir", "physically unable to perform", "pup", "suspension"):
        return "O"
    if sl == "doubtful":
        return "D"
    if sl == "questionable":
        return "Q"
    return None


def fetch_prior_stats(athlete_id, kind):
    """Last completed season per-game stats via ESPN core API.
    kind: 'passing' or 'skill'. Returns dict or None."""
    if not athlete_id:
        return None
    url = (f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
           f"seasons/{PRIOR_SEASON}/types/2/athletes/{athlete_id}/statistics/0")
    try:
        data = _http_get_json(url)

        def stat(block, *names):
            if not block:
                return None
            for s in block.get("stats", []):
                if isinstance(s, dict) and (s.get("name") in names or s.get("shortDisplayName") in names):
                    try:
                        return float(s.get("value"))
                    except (TypeError, ValueError):
                        continue
            return None

        gen = _find_stat_category(data, {"general"})
        games = stat(gen, "gamesPlayed", "GP")
        if kind == "passing":
            blk = _find_stat_category(data, {"passing"})
            if not blk:
                return None
            games = games or stat(blk, "teamGamesPlayed") or 17
            att = stat(blk, "passingAttempts", "ATT")
            if not att or att < 100:   # ignore fringe/no-sample seasons
                return None
            comp = stat(blk, "completions", "CMP") or 0
            return {
                "comp_pct": round(comp / att * 100, 1),
                "pass_yds": round((stat(blk, "passingYards", "YDS") or 0) / games, 1),
                "pass_td": round((stat(blk, "passingTouchdowns", "TD") or 0) / games, 2),
                "int": round((stat(blk, "interceptions", "INT") or 0) / games, 2),
                "rating": stat(blk, "QBRating", "passerRating", "RTG"),
                "gp": games,
            }
        else:
            rec_b = _find_stat_category(data, {"receiving"})
            rush_b = _find_stat_category(data, {"rushing"})
            if not rec_b and not rush_b:
                return None
            games = games or 17
            tgt = stat(rec_b, "receivingTargets", "TGTS") or 0
            att = stat(rush_b, "rushingAttempts", "ATT") or 0
            if tgt + att < 20:         # too small even for a prior
                return None
            return {
                "targets": round(tgt / games, 1),
                "rec": round((stat(rec_b, "receptions", "REC") or 0) / games, 1),
                "rec_yds": round((stat(rec_b, "receivingYards", "YDS") or 0) / games, 1),
                "rush_att": round(att / games, 1),
                "rush_yds": round((stat(rush_b, "rushingYards", "YDS") or 0) / games, 1),
                "td_per_game": round(((stat(rec_b, "receivingTouchdowns", "TD") or 0)
                                     + (stat(rush_b, "rushingTouchdowns", "TD") or 0)) / games, 2),
                "gp": games,
            }
    except Exception:
        return None

def _blend(cur, pri, gp, k):
    """Shrinkage blend of two per-game rates. Either side may be None."""
    if cur is None and pri is None:
        return None
    if pri is None:
        return cur
    if cur is None or not gp:
        return pri
    return (gp * cur + k * pri) / (gp + k)

def blend_stats(cur, pri, keys_usage=(), keys_rate=()):
    gp = (cur or {}).get("gp") or 0
    out = {}
    for key in keys_usage:
        out[key] = _blend((cur or {}).get(key), (pri or {}).get(key), gp, K_USAGE)
    for key in keys_rate:
        out[key] = _blend((cur or {}).get(key), (pri or {}).get(key), gp, K_RATE)
    return out


# ---------------------------------------------------------------------------
# RECENT FORM (last-N games). A hot or cold stretch should move a projection
# off the season baseline. We pull ESPN's per-game gamelog, average the last
# RECENT_N games, and overlay that on the season/prior blend at RECENT_W.
# Fails safe: any error or thin sample -> returns None -> projection is
# exactly the old season/prior blend. Never breaks a run.
# ---------------------------------------------------------------------------
RECENT_N = 4       # games in the rolling window
RECENT_W = 0.35    # weight on recent form (0 = ignore, 1 = only recent)
RECENT_MIN = 2     # need at least this many games or we skip form entirely

def _gl_val(stats_list, labels, names):
    """Pull a value out of a gamelog row by matching the column label/name."""
    for i, lab in enumerate(labels):
        key = (lab.get("name") or lab.get("abbreviation") or "").lower()
        disp = (lab.get("displayName") or "").lower()
        if any(n.lower() in (key, disp) for n in names):
            try:
                return float(stats_list[i])
            except (TypeError, ValueError, IndexError):
                return None
    return None

def fetch_recent_form(athlete_id, kind):
    """Average of the last RECENT_N games from ESPN's gamelog. dict or None."""
    if not athlete_id:
        return None
    url = (f"https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/"
           f"athletes/{athlete_id}/gamelog")
    try:
        data = _http_get_json(url)
    except Exception:
        return None
    # gamelog structure: seasonTypes -> categories -> events{ eventId: {stats:[...]}}
    # labels live at data["labels"] or data["names"]; we map by label text.
    labels = data.get("labels") or data.get("names") or []
    label_objs = []
    # ESPN sometimes gives plain-string labels; wrap them uniformly
    for l in labels:
        label_objs.append({"name": l} if isinstance(l, str) else l)

    # collect per-game stat arrays, most-recent first
    rows = []
    seasontypes = data.get("seasonTypes") or []
    for st in seasontypes:
        for cat in (st.get("categories") or []):
            for ev in (cat.get("events") or []):
                stt = ev.get("stats")
                if isinstance(stt, list) and stt:
                    rows.append(stt)
    # events may already be newest-first; guard both ways by keeping order given
    if not rows:
        # alternate shape: data["events"] dict + data["seasonTypes"]... skip if absent
        return None

    rows = rows[:RECENT_N]
    n = len(rows)
    if n < RECENT_MIN:
        return None

    def avg(names):
        vals = [_gl_val(r, label_objs, names) for r in rows]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    if kind == "passing":
        py = avg(["passingYards", "YDS", "yds"])
        ptd = avg(["passingTouchdowns", "passing touchdowns", "TD"])
        cpct = avg(["completionPct", "comp%", "CMP%"])
        rtg = avg(["QBRating", "passer rating", "RTG", "rating"])
        if py is None and ptd is None:
            return None
        return {"pass_yds": py, "pass_td": ptd, "comp_pct": cpct, "rating": rtg, "gp": n}
    else:
        ry = avg(["rushingYards", "rush yds", "YDS"])
        rtd = avg(["rushingTouchdowns", "rushing touchdowns"])
        rec = avg(["receptions", "REC"])
        recy = avg(["receivingYards", "rec yds"])
        rectd = avg(["receivingTouchdowns", "receiving touchdowns"])
        tgt = avg(["receivingTargets", "targets", "TGTS"])
        if all(v is None for v in (ry, recy, rec)):
            return None
        return {"rush_yds": ry, "rush_td": rtd, "rec": rec, "rec_yds": recy,
                "rec_td": rectd, "targets": tgt, "gp": n}

def apply_recent_form(blended, recent, keys):
    """Overlay recent-form onto a blended stat dict for the given keys.
    result = (1-w)*season_blend + w*recent, only where recent has the stat."""
    if not recent:
        return blended
    w = RECENT_W
    out = dict(blended)
    for k in keys:
        rv = recent.get(k)
        bv = blended.get(k)
        if rv is not None and bv is not None:
            out[k] = (1 - w) * bv + w * rv
        elif rv is not None and bv is None:
            out[k] = rv
    return out

LEAGUE_AVG_QB = {"comp_pct": 64.5, "pass_yds": 220, "pass_td": 1.4, "int": 0.7, "rating": 88.0}


def project_qb(team_abbr, opponent_abbr=None, vegas_factor=None, weather=None):
    name, athlete_id = fetch_starting_qb(team_abbr)
    cur = fetch_qb_season_stats(athlete_id) if athlete_id else None
    pri = fetch_prior_stats(athlete_id, "passing") if athlete_id else None

    if cur or pri:
        stats = blend_stats(cur, pri, keys_rate=("comp_pct", "pass_yds", "pass_td", "int", "rating"))
        src_tag = "blend" if (cur and pri) else ("current" if cur else "prior")
        # recent-form overlay (last few games) — nudges toward a hot/cold streak
        recent = fetch_recent_form(athlete_id, "passing")
        if recent:
            stats = apply_recent_form(stats, recent, ("pass_yds", "pass_td", "comp_pct", "rating"))
            src_tag = "form"
    else:
        stats = dict(LEAGUE_AVG_QB)
        src_tag = "league_avg"

    # Vegas-implied team total is the sharpest single signal available:
    # the market already prices injuries, pace, weather, and matchup.
    # When lines exist, scale to them; otherwise fall back to the
    # defense-strength factor (avoids double-counting defense).
    if vegas_factor is not None:
        factor = vegas_factor
    else:
        factor = fetch_defense_factor(opponent_abbr) if opponent_abbr else 1.0

    _wxp = (weather or {}).get("pass", 1.0)
    rating = stats.get("rating") or LEAGUE_AVG_QB["rating"]
    rating_adj = round(rating * (1 + (factor - 1) * 0.6), 1)
    quality = round(max(0, min(50, (rating_adj - 70) / 1.2)))
    epa_db = round((rating_adj - 85) / 100, 2)

    return {
        "name": name,
        "player_id": athlete_id,
        "quality": quality,
        "comp_pct": round(stats.get("comp_pct") or LEAGUE_AVG_QB["comp_pct"], 1),
        "pass_yds": round((stats.get("pass_yds") or LEAGUE_AVG_QB["pass_yds"]) * factor * _wxp, 1),
        "pass_td": round((stats.get("pass_td") or LEAGUE_AVG_QB["pass_td"]) * factor * _wxp, 2),
        "int": round(stats.get("int") or LEAGUE_AVG_QB["int"], 2),
        "rating": rating_adj,
        "epa_db": epa_db,
        "src": src_tag,
        "status": player_status(athlete_id),
        "matchup_grade": dvp_grade(opponent_abbr, "QB"),
        "_matchup_factor": round(factor, 3),
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
def project_skill_players(team_abbr, opponent_abbr=None, vegas_factor=None, game_script=None, weather=None):
    import math as _math
    starters = fetch_skill_starters(team_abbr)
    gs = game_script or {"rush": 1.0, "pass": 1.0}
    _wxp = (weather or {}).get("pass", 1.0)
    if vegas_factor is not None:
        factor = vegas_factor
    else:
        factor = fetch_defense_factor(opponent_abbr) if opponent_abbr else 1.0

    # ── pass 1: blended usage + status for everyone ──
    roster = []
    for name, athlete_id, pos in starters:
        if not name:
            continue
        cur = fetch_skill_season_stats(athlete_id)
        pri = fetch_prior_stats(athlete_id, "skill")
        if not cur and not pri:
            continue
        b = blend_stats(cur, pri,
                        keys_usage=("targets", "rush_att"),
                        keys_rate=("rec", "rec_yds", "rush_yds", "td_per_game"))
        _src = "blend" if (cur and pri) else ("current" if cur else "prior")
        recent = fetch_recent_form(athlete_id, "skill")
        if recent:
            b = apply_recent_form(b, recent, ("rec", "rec_yds", "rush_yds", "targets"))
            _src = "form"
        roster.append({
            "name": name, "id": athlete_id, "pos": pos, "b": b,
            "status": player_status(athlete_id),
            "src": _src,
        })

    # ── vacated usage from OUT/doubtful players, redistributed 85% ──
    out_players = [r for r in roster if r["status"] in ("O", "D")]
    active = [r for r in roster if r["status"] not in ("O", "D")]
    vac_tgt = sum((r["b"].get("targets") or 0) for r in out_players) * 0.85
    vac_att = sum((r["b"].get("rush_att") or 0) for r in out_players) * 0.85
    out_names = ", ".join(f'{r["name"]} ({r["pos"]})' for r in out_players[:2])
    tot_tgt = sum((r["b"].get("targets") or 0) for r in active) or 1.0
    tot_att = sum((r["b"].get("rush_att") or 0) for r in active) or 1.0

    def compute(b, targets, rush_att):
        """Full projection from a usage pair (lets us diff base vs boosted)."""
        ypt = ((b.get("rec_yds") or 0) / (b.get("targets") or 1)) if (b.get("targets") or 0) else LEAGUE_YPT
        ypa = ((b.get("rush_yds") or 0) / (b.get("rush_att") or 1)) if (b.get("rush_att") or 0) else LEAGUE_YPA
        ypt = 0.8 * ypt + 0.2 * LEAGUE_YPT
        ypa = 0.8 * ypa + 0.2 * LEAGUE_YPA
        catch = ((b.get("rec") or 0) / (b.get("targets") or 1)) if (b.get("targets") or 0) else LEAGUE_CATCH
        catch = min(0.9, 0.85 * catch + 0.15 * LEAGUE_CATCH)
        rec = targets * catch
        rec_yds = targets * ypt * factor * _wxp
        rush_yds = rush_att * ypa * factor
        td_usage = rush_att * TD_PER_CARRY + targets * TD_PER_TARGET
        td_hist = b.get("td_per_game") or td_usage
        td_pg = (0.7 * td_usage + 0.3 * td_hist) * factor
        td_prob = min(0.85, 1 - _math.exp(-max(0.0, td_pg)))
        fpts = rec * 1.0 + rec_yds * 0.1 + rush_yds * 0.1 + td_pg * 6.0
        return rec, rec_yds, rush_yds, td_prob, fpts

    players = []
    for r in active:
        b = r["b"]
        # game-script: nudge this player's usage by team run/pass tendency
        b = dict(b)
        if b.get("targets"):
            b["targets"] = b["targets"] * gs["pass"]
        if b.get("rush_att"):
            b["rush_att"] = b["rush_att"] * gs["rush"]
        base_tgt = b.get("targets") or 0.0
        base_att = b.get("rush_att") or 0.0
        boost_tgt = base_tgt + vac_tgt * (base_tgt / tot_tgt)
        boost_att = base_att + vac_att * (base_att / tot_att)

        _, _, _, _, fpts_base = compute(b, base_tgt, base_att)
        rec, rec_yds, rush_yds, td_prob, fpts = compute(b, boost_tgt, boost_att)

        p = {
            "name": r["name"], "pos": r["pos"], "player_id": r["id"],
            "matchup_grade": dvp_grade(opponent_abbr, r["pos"]),
            "targets": round(boost_tgt, 1), "rec": round(rec, 1),
            "rec_yds": round(rec_yds, 1), "rush_yds": round(rush_yds, 1),
            "rush_att": round(boost_att, 1),
            "td_prob": round(td_prob, 2), "fpts": round(fpts, 1),
            "src": r["src"],
        }
        if r["status"]:
            p["status"] = r["status"]           # Q shows a badge on the board
        if out_players and (fpts - fpts_base) >= 0.5:
            p["news"] = {
                "reason": f"{out_names} ruled OUT",
                "fpts_from": round(fpts_base, 1),
                "fpts_to": round(fpts, 1),
            }
        players.append(p)
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


def _vegas_factors(market):
    """Implied team totals from spread+total -> dampened scaling factors.
    home_spread is negative when home is favored: A - H = spread, A + H = total
    => H = (total - spread) / 2, A = (total + spread) / 2.
    Factor is (implied / 22.0) ** 0.7 clipped to [0.75, 1.30] -- dampened so
    the market steers the projection without steamrolling player identity."""
    L = (market or {}).get("_lines") or {}
    spread, total = L.get("spread"), L.get("total")
    if spread is None or total is None:
        return None, None
    home_imp = (total - spread) / 2.0
    away_imp = (total + spread) / 2.0
    def f(pts):
        raw_f = max(0.75, min(1.30, pts / 22.0))
        return round(raw_f ** 0.7, 3)
    return f(away_imp), f(home_imp)


OWM_API_KEY = os.environ.get("OWM_API_KEY")

def get_json_generic(url):
    return _http_get_json(url)

def fetch_weather(city, state, indoor):
    """Wind (mph) + precip flag for a game. Indoor games return calm.
    Uses OpenWeather (same key as MLB). Returns dict or None."""
    if indoor:
        return {"wind": 0, "precip": False, "indoor": True, "temp": 72}
    if not (OWM_API_KEY and city):
        return None
    try:
        q = f"{city},{state},US" if state else f"{city},US"
        url = (f"https://api.openweathermap.org/data/2.5/weather"
               f"?q={q}&units=imperial&appid={OWM_API_KEY}")
        d = get_json_generic(url)
        wind = (d.get("wind") or {}).get("speed", 0)
        main = (d.get("weather") or [{}])[0].get("main", "").lower()
        precip = main in ("rain", "snow", "thunderstorm", "drizzle")
        temp = (d.get("main") or {}).get("temp", 60)
        return {"wind": round(wind), "precip": precip, "indoor": False, "temp": round(temp)}
    except Exception as e:
        print(f"  (weather fetch failed for {city}: {e})")
        return None


def weather_factor(wx):
    """Passing/kicking suppression from weather. 1.0 = no effect.
    Wind is the big one: >15mph starts hurting the deep passing game."""
    if not wx or wx.get("indoor"):
        return {"pass": 1.0, "note": None}
    wind = wx.get("wind", 0)
    pass_mult = 1.0
    note = None
    if wind >= 25:
        pass_mult, note = 0.86, f"{wind}mph wind"
    elif wind >= 20:
        pass_mult, note = 0.91, f"{wind}mph wind"
    elif wind >= 15:
        pass_mult, note = 0.95, f"{wind}mph wind"
    if wx.get("precip"):
        pass_mult *= 0.96
        note = (note + " + precip") if note else "precip"
    return {"pass": round(pass_mult, 3), "note": note}


def _game_script(market):
    """Spread -> per-team run/pass tendencies. Returns (away_script, home_script)
    where each is {'rush': mult, 'pass': mult}. Favorites lean run (game-flow
    lead -> clock-killing carries), underdogs lean pass (chasing points).
    Dampened so it nudges rather than dominates."""
    L = (market or {}).get("_lines") or {}
    spread = L.get("spread")   # home spread; negative = home favored
    if spread is None:
        return None, None
    # magnitude of the favorite's edge, capped
    mag = max(-10, min(10, spread))
    # home favored (spread<0): home runs more, away passes more
    # scale: each point of spread ~1.2% shift, capped ~ +/-12%
    shift = min(0.12, abs(mag) * 0.012)
    if mag < 0:      # home favored
        home_script = {"rush": 1 + shift, "pass": 1 - shift * 0.6}
        away_script = {"rush": 1 - shift, "pass": 1 + shift * 0.6}
    elif mag > 0:    # away favored
        away_script = {"rush": 1 + shift, "pass": 1 - shift * 0.6}
        home_script = {"rush": 1 - shift, "pass": 1 + shift * 0.6}
    else:
        away_script = home_script = {"rush": 1.0, "pass": 1.0}
    return away_script, home_script


def build_game(raw, odds_map=None):
    odds = (odds_map or {}).get((raw["away_team"], raw["home_team"]))
    model_home_win_pct = fetch_espn_predictor(raw.get("event_id"))
    market = calc_market_fields(raw["away_team"], raw["home_team"], odds, model_home_win_pct)
    away_vf, home_vf = _vegas_factors(market)
    away_gs, home_gs = _game_script(market)
    wx = fetch_weather(raw.get("venue_city"), raw.get("venue_state"), raw.get("indoor"))
    wxf = weather_factor(wx)
    away_qb_proj = project_qb(raw["away_abbr"], raw["home_abbr"], vegas_factor=away_vf, weather=wxf)
    home_qb_proj = project_qb(raw["home_abbr"], raw["away_abbr"], vegas_factor=home_vf, weather=wxf)
    game = {
        "away_team": raw["away_team"],
        "home_team": raw["home_team"],
        "venue": raw["venue"],
        "weather": wx,
        "weather_note": wxf.get("note"),
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
        "away_skill": project_skill_players(raw["away_abbr"], raw["home_abbr"], vegas_factor=away_vf, game_script=away_gs, weather=wxf),
        "home_skill": project_skill_players(raw["home_abbr"], raw["away_abbr"], vegas_factor=home_vf, game_script=home_gs, weather=wxf),
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
        "date": _today_et(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "games": games,
    }
    # attach run-over-run projection deltas (generic news layer)
    try:
        prev_path = Path(__file__).parent / "data" / "nfl_slate.json"
        if prev_path.exists():
            prev = json.loads(prev_path.read_text())
            prev_sk = {}
            for pg in prev.get("games", []):
                for side in ("away_skill", "home_skill"):
                    for pp in pg.get(side, []) or []:
                        prev_sk[pp.get("name")] = pp
            for g in slate.get("games", []):
                for side in ("away_skill", "home_skill"):
                    for pp in g.get(side, []) or []:
                        old_p = prev_sk.get(pp.get("name"))
                        if old_p and "news" not in pp:
                            d = (pp.get("fpts") or 0) - (old_p.get("fpts") or 0)
                            if abs(d) >= 0.8:
                                pp["fpts_prev"] = old_p.get("fpts")
    except Exception:
        pass
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
