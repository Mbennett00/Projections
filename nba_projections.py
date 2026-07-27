#!/usr/bin/env python3
"""
NBA Projections -> nba_slate.json

Data sources (all free):
  - ESPN API (site.api.espn.com): schedule, rosters, player season stats,
    team defense. Same pattern as the NFL/NHL engines -- works from GitHub
    Actions runners (unlike stats.nba.com, which blocks cloud IPs).
  - The Odds API (optional, ODDS_API_KEY): NBA totals/spreads for
    Vegas-implied team-total anchoring.

Engine (mirrors nfl/nhl v2):
  - MINUTES are the foundation: everything scales with projected minutes.
    Blend season minutes with prior season via shrinkage (gp*cur + K*prior)/(gp+K).
  - Per-minute rates for PTS/REB/AST/3PM, projected onto blended minutes.
  - Team pace/total from Vegas when available, else team scoring blend.
  - Matchup grade: opponent defensive rating vs league average (DvP-style).
  - Points/rebounds/assists are low-variance projections -- most stable in sports.

Output: nba_slate.json next to this script (workflow copies to data/).
"""

import json
import math
import os
import sys
from datetime import datetime, date, timezone

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

def _today_et():
    # Slate day is US Eastern, not the GitHub runner's UTC clock (prevents the
    # slate rolling to "tomorrow / waiting for lines" after 8pm ET).
    now = datetime.now(_ET) if _ET else datetime.now()
    return now.strftime("%Y-%m-%d")
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None
    import urllib.request

OUT_PATH = Path(__file__).parent / "nba_slate.json"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

# ── season bookkeeping ────────────────────────────────────────────────────
# NBA season spanning e.g. Oct 2025-Jun 2026 is the "2026" season in ESPN.
_today = date.today()
CUR_SEASON = _today.year + 1 if _today.month >= 9 else _today.year
PRIOR_SEASON = CUR_SEASON - 1
K_GP = 8                     # shrinkage constant (games) for per-game rates
LEAGUE_PACE_TOTAL = 226.0    # league avg combined game total (both teams)
LEAGUE_DEF_RTG = 114.0       # league avg defensive rating (pts/100 poss)
HOME_EDGE = 1.02

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"


def _get(url, timeout=25):
    if requests:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "slate-app"})
        r.raise_for_status()
        return r
    req = urllib.request.Request(url, headers={"User-Agent": "slate-app"})
    return urllib.request.urlopen(req, timeout=timeout)


def get_json(url):
    r = _get(url)
    return r.json() if requests else json.load(r)


# ── schedule ──────────────────────────────────────────────────────────────
def fetch_schedule(day):
    """Games for a YYYYMMDD day via ESPN scoreboard."""
    ymd = day.replace("-", "")
    try:
        data = get_json(f"{ESPN}/scoreboard?dates={ymd}")
    except Exception as e:
        print(f"Schedule fetch failed: {e}")
        return None
    games = []
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        cs = comp.get("competitors", [])
        home = next((c for c in cs if c.get("homeAway") == "home"), {})
        away = next((c for c in cs if c.get("homeAway") == "away"), {})
        state = (ev.get("status", {}).get("type", {}) or {}).get("state", "pre")
        mapped = "Live" if state == "in" else "Final" if state == "post" else "Preview"
        games.append({
            "id": ev.get("id"),
            "away_abbr": (away.get("team", {}) or {}).get("abbreviation"),
            "home_abbr": (home.get("team", {}) or {}).get("abbreviation"),
            "away_team": (away.get("team", {}) or {}).get("displayName"),
            "home_team": (home.get("team", {}) or {}).get("displayName"),
            "away_id": (away.get("team", {}) or {}).get("id"),
            "home_id": (home.get("team", {}) or {}).get("id"),
            "venue": (comp.get("venue", {}) or {}).get("fullName"),
            "game_time": ev.get("date"),
            "game_state": mapped,
            "away_score": _int(away.get("score")),
            "home_score": _int(home.get("score")),
        })
    return games


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ── rosters + player stats ────────────────────────────────────────────────
def fetch_roster(team_id):
    """Return list of (player_id, name, pos) for a team."""
    try:
        data = get_json(f"{ESPN}/teams/{team_id}/roster")
    except Exception:
        return []
    out = []
    for grp in data.get("athletes", []):
        items = grp.get("items", []) if isinstance(grp, dict) else [grp]
        for a in items:
            pid = a.get("id")
            name = a.get("displayName") or a.get("fullName")
            pos = ((a.get("position") or {}) or {}).get("abbreviation", "")
            if pid and name:
                out.append((pid, name, pos))
    return out


def fetch_player_stats(player_id, season):
    """Season per-game MIN/PTS/REB/AST/3PM for a player. None on failure."""
    url = f"{CORE}/seasons/{season}/types/2/athletes/{player_id}/statistics/0"
    try:
        data = get_json(url)
    except Exception:
        return None

    def stat(cat_names, *names):
        for cat in data.get("splits", {}).get("categories", []):
            if cat.get("name") in cat_names:
                for s in cat.get("stats", []):
                    if s.get("name") in names or s.get("abbreviation") in names:
                        try:
                            return float(s.get("value"))
                        except (TypeError, ValueError):
                            return None
        return None

    gp = stat({"general"}, "gamesPlayed", "GP")
    if not gp or gp < 1:
        return None
    mpg = stat({"general"}, "avgMinutes", "MIN") or stat({"offensive"}, "avgMinutes")
    if mpg is None:
        # try total minutes / gp
        tot_min = stat({"general"}, "minutes")
        mpg = (tot_min / gp) if tot_min else None
    return {
        "gp": gp,
        "min": mpg or 0,
        "pts": stat({"offensive"}, "avgPoints", "PTS") or 0,
        "reb": stat({"general"}, "avgRebounds", "REB") or 0,
        "ast": stat({"offensive"}, "avgAssists", "AST") or 0,
        "tpm": stat({"offensive"}, "avgThreePointFieldGoalsMade", "3PM") or 0,
    }


def _blend(cur, pri, gp, k=K_GP):
    if cur is None and pri is None:
        return None
    if pri is None:
        return cur
    if cur is None or not gp:
        return pri
    return (gp * cur + k * pri) / (gp + k)


def fetch_recent_form(player_id, n=12):
    """Last-N game averages for MIN/PTS/REB/AST/3PM. None if unavailable.
    Recent form captures role changes the season average lags behind."""
    try:
        data = get_json(f"{CORE}/seasons/{CUR_SEASON}/types/2/athletes/{player_id}/eventlog")
    except Exception:
        return None
    # eventlog lists recent events; pull box lines if present
    events = (data.get("events", {}) or {}).get("items", [])[:n]
    if not events:
        return None
    tot = {"min": 0, "pts": 0, "reb": 0, "ast": 0, "tpm": 0}
    cnt = 0
    for ev in events:
        stats = ev.get("statistics") or ev.get("stats")
        if not stats:
            continue
        # stats may be a ref or inline; only use inline numeric lines
        if isinstance(stats, dict):
            for k, key in [("min","minutes"),("pts","points"),("reb","rebounds"),("ast","assists"),("tpm","threePointFieldGoalsMade")]:
                v = stats.get(key)
                if isinstance(v, (int, float)):
                    tot[k] += v
            cnt += 1
    if cnt == 0:
        return None
    return {k: tot[k] / cnt for k in tot}


def blended_player(player_id):
    cur = fetch_player_stats(player_id, CUR_SEASON)
    pri = fetch_player_stats(player_id, PRIOR_SEASON)
    if not cur and not pri:
        return None
    gp = (cur or {}).get("gp", 0) or 0
    out = {"src": "blend" if (cur and pri) else ("current" if cur else "prior")}
    for k in ("min", "pts", "reb", "ast", "tpm"):
        out[k] = _blend((cur or {}).get(k), (pri or {}).get(k), gp)
    # recent-form overlay: weight last-N games 35% when we have them and the
    # player has enough current-season sample for a gamelog to exist
    recent = fetch_recent_form(player_id) if (cur and (cur.get("gp") or 0) >= 5) else None
    if recent:
        for k in ("min", "pts", "reb", "ast", "tpm"):
            if out.get(k) is not None and recent.get(k) is not None:
                out[k] = 0.65 * out[k] + 0.35 * recent[k]
        out["src"] = "recent"
    return out


# ── team defense (for DvP-style grade) ────────────────────────────────────
_def_cache = {}

def fetch_def_rating(team_id, season):
    """Opponent points allowed per game as a defense proxy. None on failure."""
    key = (team_id, season)
    if key in _def_cache:
        return _def_cache[key]
    val = None
    try:
        url = f"{CORE}/seasons/{season}/types/2/teams/{team_id}/statistics"
        data = get_json(url)
        for cat in data.get("splits", {}).get("categories", []):
            for s in cat.get("stats", []):
                if s.get("name") in ("avgPointsAgainst", "pointsAgainstPerGame"):
                    val = float(s.get("value"))
                    break
    except Exception:
        pass
    _def_cache[key] = val
    return val


def matchup_grade(opp_pts_allowed):
    """Weak defense (allows more) = better matchup = higher grade."""
    if not opp_pts_allowed:
        return None
    ratio = opp_pts_allowed / 114.0     # league avg ~114 pts/game allowed
    scale = [(1.05, "A+"), (1.03, "A"), (1.01, "B+"), (0.99, "B"),
             (0.97, "C+"), (0.95, "C"), (0.92, "D"), (0.0, "F")]
    for thr, g in scale:
        if ratio >= thr:
            return g
    return "F"


# ── injuries (ESPN NBA feed) ───────────────────────────────────────────────
_INJ = None

def fetch_injuries():
    global _INJ
    if _INJ is not None:
        return _INJ
    out = {}
    try:
        data = get_json(f"{ESPN}/injuries")
        def walk(node):
            if isinstance(node, dict):
                ath, status = node.get("athlete"), node.get("status")
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
        if out:
            print(f"Injuries: {len(out)} NBA statuses loaded")
    except Exception as e:
        print(f"  (injury feed unavailable: {e})")
    _INJ = out
    return out


def player_status(player_id):
    s = fetch_injuries().get(str(player_id))
    if not s:
        return None
    sl = s.lower()
    if sl in ("out", "injured", "suspension"):
        return "O"
    if sl in ("doubtful",):
        return "D"
    if sl in ("questionable", "day-to-day", "game-time decision"):
        return "Q"
    return None


# ── Vegas ─────────────────────────────────────────────────────────────────
def fetch_odds():
    if not ODDS_API_KEY:
        return {}
    url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
           f"?apiKey={ODDS_API_KEY}&regions=us&markets=spreads,totals&oddsFormat=american")
    try:
        rows = get_json(url)
    except Exception as e:
        print(f"  (odds fetch failed: {e})")
        return {}
    out = {}
    for ev in rows:
        totals, spreads = [], []
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    if mk["key"] == "totals" and o.get("name") == "Over" and o.get("point") is not None:
                        totals.append(o["point"])
                    if mk["key"] == "spreads" and o.get("name") == ev.get("home_team") and o.get("point") is not None:
                        spreads.append(o["point"])
        if totals:
            out[(ev.get("away_team"), ev.get("home_team"))] = {
                "total": round(sum(totals) / len(totals), 1),
                "home_spread": round(sum(spreads) / len(spreads), 1) if spreads else None,
                "event_id": ev.get("id"),
            }
    return out


def match_odds(odds_map, away_name, home_name):
    a = (away_name or "").split()[-1].lower()
    h = (home_name or "").split()[-1].lower()
    for (ak, hk), v in odds_map.items():
        if a and h and a in ak.lower() and h in hk.lower():
            return v
    return None


# ── projections ───────────────────────────────────────────────────────────
def project_team(talent_players, pace_factor, b2b=False, blowout=0.0):
    """Roster projections with injury-redistribution, pace, usage, B2B fatigue.

    - OUT/doubtful players are removed; their minutes AND production
      redistribute to the rotation proportionally (the biggest NBA edge).
    - pace_factor scales counting stats to the game's expected possessions.
    - b2b applies a small fatigue haircut (2nd night of a back-to-back).
    """
    # attach status + split out unavailable players
    roster = []
    for pid, name, pos, b in talent_players:
        if (b.get("min") or 0) < 8:
            continue
        roster.append({"pid": pid, "name": name, "pos": pos, "b": b,
                       "status": player_status(pid)})

    out_players = [r for r in roster if r["status"] in ("O", "D")]
    active = [r for r in roster if r["status"] not in ("O", "D")]
    if not active:
        active = roster  # everyone flagged? fall back to raw

    # redistribute the unavailable players' minutes (capped at 48/player) and
    # production proportionally to active players by their own minute share
    vac_min = sum((r["b"].get("min") or 0) for r in out_players)
    vac = {k: sum((r["b"].get(k) or 0) for r in out_players) for k in ("pts", "reb", "ast", "tpm")}
    tot_active_min = sum((r["b"].get("min") or 0) for r in active) or 1.0
    out_names = ", ".join(r["name"] for r in out_players[:2])

    fatigue = 0.96 if b2b else 1.0
    out = []
    for r in active:
        b = r["b"]
        base_min = b.get("min") or 0
        share = base_min / tot_active_min
        # redistribute up to 85% of vacated minutes, capped so nobody exceeds 42
        boost_min = min(42, base_min + vac_min * share * 0.85)
        # blowout risk: in likely blowouts, high-minute starters sit late.
        # blowout is 0..1; caps a 34-min starter toward ~30 at full blowout.
        if blowout > 0 and boost_min >= 30:
            boost_min *= (1 - blowout * 0.12 * ((boost_min - 30) / 12 + 0.5))
        min_scale = (boost_min / base_min) if base_min else 1.0

        def proj(stat):
            base = (b.get(stat) or 0)
            # redistributed production: own rate scaled by minutes + share of vacated
            redist = vac[stat] * share * 0.85
            return (base * min_scale * 0.6 + (base + redist) * 0.4) * pace_factor * fatigue

        pts, reb, ast, tpm = proj("pts"), proj("reb"), proj("ast"), proj("tpm")
        pra = pts + reb + ast
        # usage proxy: how much scoring load this player carries in the rotation
        usage = round(min(0.45, (pts + ast * 0.5) / max(1, boost_min) * 2.4), 3)

        p = {
            "name": r["name"], "player_id": r["pid"], "pos": r["pos"],
            "min": round(boost_min, 1),
            "pts": round(pts, 1), "reb": round(reb, 1),
            "ast": round(ast, 1), "tpm": round(tpm, 1),
            "pra": round(pra, 1), "usage": usage,
            "src": b.get("src"),
        }
        if r["status"]:
            p["status"] = r["status"]
        if b2b:
            p["b2b"] = True
        if out_players and boost_min - base_min >= 1.5:
            p["news"] = {
                "reason": f"{out_names} OUT",
                "min_from": round(base_min, 1), "min_to": round(boost_min, 1),
            }
        out.append(p)
    out.sort(key=lambda p: p["min"], reverse=True)
    return out[:10]


# ---------------------------------------------------------------------------
# PROP VALUE ENGINE
# ---------------------------------------------------------------------------
# Player props are pulled live, per event, from The Odds API -- real posted
# lines and prices. internal key -> real API market key:
PROP_MARKET_MAP = {
    "pts": "player_points",
    "reb": "player_rebounds",
    "ast": "player_assists",
    "tpm": "player_threes",
    "pra": "player_points_rebounds_assists",
}
PROP_LABELS = {"pts": "Points", "reb": "Rebounds", "ast": "Assists", "tpm": "Threes Made", "pra": "Pts+Reb+Ast"}
EDGE_THRESHOLD = 4.0  # flag as a play if model edge >= this percentage


def poisson_pmf(k, lam):
    return (lam ** k) * math.exp(-lam) / math.factorial(k)


def poisson_over(mean, line):
    """P(stat > line) using Poisson around the projected mean -- same
    approximation already used across this app's other sports; counting
    stats like points/rebounds/assists are overdispersed vs true Poisson,
    but it's a reasonable, transparent approximation for a real model mean."""
    k = int(math.floor(line))
    p_at_most_k = sum(poisson_pmf(i, max(0.001, mean)) for i in range(k + 1))
    return max(0.001, min(0.999, 1 - p_at_most_k))


def american_to_prob(odds):
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return -odds / (-odds + 100)


def devig_prob(over_odds, under_odds):
    over_raw = american_to_prob(over_odds)
    under_raw = american_to_prob(under_odds)
    total = over_raw + under_raw
    return over_raw / total, under_raw / total


def american_to_decimal(odds):
    return (odds / 100 + 1) if odds > 0 else (100 / -odds + 1)


def edge_tier(score):
    if score >= 85: return "elite"
    if score >= 70: return "strong"
    if score >= 50: return "lean"
    return "none"


def edge_score(edge_pct, model_prob_pct, book_odds):
    """0-100 Edge Score from three real, already-computed signals:
      - edge_pct: how far the model's probability beats the market's
      - model_prob_pct: the model's own conviction (distance from a 50/50 coin flip)
      - the resulting expected value of actually betting it at the posted price
    Weights (40% edge / 25% confidence / 35% EV) match the MLB engine exactly,
    so a score means the same thing across sports."""
    edge_component = max(0, min(100, (edge_pct / 15) * 100))
    confidence_component = max(0, min(100, (abs(model_prob_pct - 50) / 25) * 100))
    dec = american_to_decimal(book_odds)
    ev_pct = (model_prob_pct / 100 * dec - 1) * 100
    ev_component = max(0, min(100, (ev_pct / 20) * 100))
    score = round(0.40 * edge_component + 0.25 * confidence_component + 0.35 * ev_component)
    return score, round(ev_pct, 1)


def _make_play(name, market, line, side, model_prob, book_prob, edge, book_odds):
    score, ev_pct = edge_score(edge, model_prob, book_odds)
    return {"name": name, "market": market, "line": line, "side": side,
            "model_prob": round(model_prob, 1), "book_prob": round(book_prob, 1),
            "edge": round(edge, 1), "book_odds": book_odds,
            "ev_pct": ev_pct, "edge_score": score, "edge_tier": edge_tier(score)}


def fetch_player_props(event_id):
    """Pull every player prop for one live NBA event in a single call.
    Returns {player_name: {internal_key: {"line": float, "over": odds, "under": odds}}}."""
    if not ODDS_API_KEY or not event_id:
        return {}
    markets_param = ",".join(sorted(set(PROP_MARKET_MAP.values())))
    url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"
           f"?apiKey={ODDS_API_KEY}&regions=us&markets={markets_param}&oddsFormat=american")
    try:
        data = get_json(url)
    except Exception as e:
        print(f"  (props fetch failed for event {event_id}: {e})")
        return {}

    reverse_map = {v: k for k, v in PROP_MARKET_MAP.items()}
    bms = data.get("bookmakers", [])
    if not bms:
        return {}
    book = bms[0]
    out = {}
    for m in book.get("markets", []):
        internal_key = reverse_map.get(m.get("key"))
        if not internal_key:
            continue
        by_player_line = {}
        for oc in m.get("outcomes", []):
            player, side, point, price = oc.get("description"), oc.get("name"), oc.get("point"), oc.get("price")
            if not player or side not in ("Over", "Under") or point is None:
                continue
            by_player_line.setdefault((player, point), {})[side.lower()] = price
        for (player, line), sides in by_player_line.items():
            if "over" not in sides or "under" not in sides:
                continue
            out.setdefault(player, {})[internal_key] = {"line": line, "over": sides["over"], "under": sides["under"]}
    return out


def evaluate_props(name, proj, live_props):
    """Check this player's projected pts/reb/ast/tpm/pra against LIVE lines
    for this game. Returns a list of value plays with Edge Score attached."""
    plays = []
    player_lines = live_props.get(name, {})
    for stat_key, book_key in PROP_MARKET_MAP.items():
        market_line = player_lines.get(stat_key)
        if not market_line:
            continue
        model_mean = proj.get(stat_key)
        if not model_mean:
            continue
        line = market_line["line"]
        model_over = poisson_over(model_mean, line - 0.001)
        book_over_prob, book_under_prob = devig_prob(market_line["over"], market_line["under"])
        over_edge = (model_over - book_over_prob) * 100
        under_edge = ((1 - model_over) - book_under_prob) * 100
        label = PROP_LABELS[stat_key]
        if over_edge >= EDGE_THRESHOLD:
            plays.append(_make_play(name, label, line, "OVER",
                                     model_over * 100, book_over_prob * 100, over_edge, market_line["over"]))
        if under_edge >= EDGE_THRESHOLD:
            plays.append(_make_play(name, label, line, "UNDER",
                                     (1 - model_over) * 100, book_under_prob * 100, under_edge, market_line["under"]))
    return plays


def pace_factor_from_total(total):
    if not total:
        return 1.0
    raw = total / LEAGUE_PACE_TOTAL
    return round(raw ** 0.6, 3)     # dampened, like the other engines


def load_team_talent(team_id):
    roster = fetch_roster(team_id)
    players = []
    for pid, name, pos in roster:
        b = blended_player(pid)
        if b and (b.get("min") or 0) >= 8:
            players.append((pid, name, pos, b))
    return players


_b2b_cache = None

def teams_playing_yesterday(day):
    """Set of team abbrs that played the previous day (for B2B fatigue)."""
    global _b2b_cache
    if _b2b_cache is not None:
        return _b2b_cache
    from datetime import datetime as _dt, timedelta as _td
    try:
        d = _dt.strptime(day, "%Y-%m-%d") - _td(days=1)
        prev = fetch_schedule(d.strftime("%Y-%m-%d"))
        _b2b_cache = {g["away_abbr"] for g in prev} | {g["home_abbr"] for g in prev}
    except Exception:
        _b2b_cache = set()
    return _b2b_cache


def build_game(raw, odds_map, yesterday=None):
    line = match_odds(odds_map, raw["away_team"], raw["home_team"])
    total = (line or {}).get("total")
    spread = (line or {}).get("home_spread")
    pace = pace_factor_from_total(total)

    away_talent = load_team_talent(raw["away_id"])
    home_talent = load_team_talent(raw["home_id"])

    # implied team totals -> per-side scaling
    if total and spread is not None:
        home_imp = (total - spread) / 2.0
        away_imp = (total + spread) / 2.0
        home_f = pace_factor_from_total(home_imp * 2)
        away_f = pace_factor_from_total(away_imp * 2)
        source = "vegas"
    else:
        home_f = away_f = pace
        source = "model"

    yset = yesterday or set()
    away_b2b = raw["away_abbr"] in yset 
    home_b2b = raw["home_abbr"] in yset 
    # blowout risk from spread magnitude: 12+ point spread = high garbage-time risk
    blowout = 0.0
    if spread is not None:
        blowout = max(0.0, min(1.0, (abs(spread) - 8) / 12))   # ramps 8->20 pts
    away_players = project_team(away_talent, away_f, b2b=away_b2b, blowout=blowout)
    home_players = project_team(home_talent, home_f, b2b=home_b2b, blowout=blowout)

    # --- Prop Value: live per-event odds, evaluated against every roster player ---
    live_props = fetch_player_props((line or {}).get("event_id"))
    all_prop_plays = []
    for p in away_players + home_players:
        all_prop_plays.extend(evaluate_props(p["name"], p, live_props))
    all_prop_plays.sort(key=lambda x: -x["edge_score"])

    # defense grades (each side graded vs the opponent's points allowed)
    away_def = fetch_def_rating(raw["away_id"], CUR_SEASON) or fetch_def_rating(raw["away_id"], PRIOR_SEASON)
    home_def = fetch_def_rating(raw["home_id"], CUR_SEASON) or fetch_def_rating(raw["home_id"], PRIOR_SEASON)
    for p in away_players:      # away players face home defense
        p["matchup_grade"] = matchup_grade(home_def)
    for p in home_players:
        p["matchup_grade"] = matchup_grade(away_def)

    # win prob from spread if we have it, else pace-neutral
    if spread is not None:
        home_win = 1 / (1 + 10 ** (spread / 8.0))    # rough spread->win map
    else:
        home_win = 0.5 * HOME_EDGE
    home_win = max(0.05, min(0.95, home_win))
    edge = abs(home_win - 0.5)
    tier = "STRONG" if edge >= 0.15 else "LEAN" if edge >= 0.07 else "PASS"

    game = {
        "away_team": raw["away_team"], "home_team": raw["home_team"],
        "away_abbr": raw["away_abbr"], "home_abbr": raw["home_abbr"],
        "venue": raw.get("venue"), "game_time": raw.get("game_time"),
        "game_state": raw.get("game_state", "Preview"),
        "away_win_pct": round(1 - home_win, 3), "home_win_pct": round(home_win, 3),
        "total": total, "spread": spread,
        "tier": tier, "line_source": source, "blowout_risk": round(blowout, 2),
        "away_players": away_players, "home_players": home_players,
        "prop_plays": all_prop_plays,
    }
    if raw.get("away_score") is not None:
        game["away_score"] = raw["away_score"]
        game["home_score"] = raw["home_score"]
    return game


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else _today_et()
    print(f"NBA Projections for {day}  (season {CUR_SEASON}, prior {PRIOR_SEASON})")

    games_raw = fetch_schedule(day)
    if games_raw is None:
        print("Schedule fetch failed — leaving last good data/nba_slate.json untouched, not overwriting with an empty slate.")
        sys.exit(0)
    print(f"{len(games_raw)} games on the slate")

    games = []
    if games_raw:
        odds_map = fetch_odds()
        if odds_map:
            print(f"Odds matched for {len(odds_map)} events (Vegas anchoring on)")
        yesterday = teams_playing_yesterday(day)
        for raw in games_raw:
            b2b_note = ""
            if raw["away_abbr"] in yesterday or raw["home_abbr"] in yesterday:
                b2b_note = " (B2B)"
            print(f"  {raw['away_abbr']} @ {raw['home_abbr']}{b2b_note}...")
            try:
                games.append(build_game(raw, odds_map, yesterday))
            except Exception as e:
                print(f"    skipped ({e})")

    # standouts: top projected PRA (points+reb+ast) across the slate
    all_p = [p for g in games for p in g["away_players"] + g["home_players"]]
    top_pra = sorted(all_p, key=lambda p: p.get("pra", 0), reverse=True)[:10]

    export = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": day,
        "games": games,
        "standouts": {"top_pra": top_pra},
    }
    OUT_PATH.write_text(json.dumps(export, indent=2))
    print(f"Wrote {len(games)} games to {OUT_PATH}")


if __name__ == "__main__":
    main()
