#!/usr/bin/env python3
"""
NHL Projections -> nhl_slate.json

Data sources (all free):
  - NHL API (api-web.nhle.com): schedule, rosters, live game states. No key.
  - MoneyPuck (moneypuck.com): skater xGoals/shots, goalie quality, team
    xGF/xGA. Free CSVs. This is the talent layer -- xG stabilizes far
    faster than raw goals.
  - The Odds API (optional, ODDS_API_KEY env var): NHL totals/spreads for
    Vegas-implied team goal anchoring, same as the NFL engine.

Engine (mirrors nfl_projections v2):
  - Prior-season blending with shrinkage: (gp*current + K*prior)/(gp+K)
  - Team goal lambdas from blended team xGF vs opponent xGA (+ home ice)
  - Win % / over % from a Poisson score matrix
  - Skater anytime-goal probability: blended xG/game * matchup factor,
    P(>=1 goal) = 1 - exp(-lambda)
  - Vegas-implied team goals override the stats-based matchup factor when
    lines exist (market already prices goalies, injuries, b2b fatigue).

Output: nhl_slate.json next to this script (workflow copies to data/).
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

import pandas as pd
from io import StringIO

OUT_PATH = Path(__file__).parent / "nhl_slate.json"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")  # optional, never hardcode

# ── season bookkeeping ────────────────────────────────────────────────────
# MoneyPuck names seasons by starting year (2025 == 2025-26).
_today = date.today()
CUR_SEASON = _today.year if _today.month >= 9 else _today.year - 1
PRIOR_SEASON = CUR_SEASON - 1
K_GP = 10                 # shrinkage constant (games) for per-game rates
LEAGUE_GOALS_PG = 3.05    # league average team goals per game
HOME_ICE = 1.05           # ~5% home bump

# ── http helper ───────────────────────────────────────────────────────────
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


def get_csv(url):
    try:
        r = _get(url, timeout=60)
        text = r.text if requests else r.read().decode("utf-8")
        return pd.read_csv(StringIO(text))
    except Exception as e:
        print(f"  (couldn't fetch {url.split('/')[-1]}: {e})")
        return None


# ── MoneyPuck layer ───────────────────────────────────────────────────────
MP_BASE = "https://moneypuck.com/moneypuck/playerData/seasonSummary"

def mp_skaters(season):
    df = get_csv(f"{MP_BASE}/{season}/regular/skaters.csv")
    if df is None:
        return None
    df = df[df["situation"] == "all"].copy()
    keep = {
        "playerId": "player_id", "name": "name", "team": "team",
        "position": "pos", "games_played": "gp", "icetime": "icetime",
        "I_F_xGoals": "xg", "I_F_goals": "goals",
        "I_F_shotsOnGoal": "shots", "I_F_points": "points",
    }
    have = {k: v for k, v in keep.items() if k in df.columns}
    df = df[list(have)].rename(columns=have)
    if "points" not in df.columns:
        df["points"] = df.get("goals", 0)
    for col in ("xg", "goals", "shots", "points", "icetime"):
        if col in df.columns:
            df[col + "_pg"] = df[col] / df["gp"].clip(lower=1)
    df["toi_pg"] = df.get("icetime_pg", 0) / 60.0  # minutes
    return df.set_index("player_id")


def mp_goalies(season):
    """Goalie quality: goals saved above expected per 60. The single biggest
    driver of a game's scoring environment. Higher GSAx = tougher to score on."""
    df = get_csv(f"{MP_BASE}/{season}/regular/goalies.csv")
    if df is None:
        return None
    df = df[df["situation"] == "all"].copy()
    keep = {"playerId": "player_id", "name": "name", "team": "team",
            "games_played": "gp", "icetime": "icetime",
            "xGoals": "xg_against", "goals": "goals_against"}
    have = {k: v for k, v in keep.items() if k in df.columns}
    df = df[list(have)].rename(columns=have)
    # GSAx = expected goals against - actual goals against (positive = good)
    if "xg_against" in df.columns and "goals_against" in df.columns:
        mins = (df.get("icetime", 0) / 60.0).clip(lower=1)
        df["gsax_60"] = (df["xg_against"] - df["goals_against"]) / mins * 60.0
        df["sv_quality"] = df["xg_against"] / df["goals_against"].clip(lower=1)
    return df.set_index("player_id") if "player_id" in df.columns else None


def mp_skaters_pp(season):
    """Power-play production: identifies PP1 usage via PP time-on-ice and
    PP xG. Big chunk of goal-scoring happens on the man advantage."""
    df = get_csv(f"{MP_BASE}/{season}/regular/skaters.csv")
    if df is None:
        return None
    df = df[df["situation"] == "5on4"].copy()   # power-play situation
    keep = {"playerId": "player_id", "games_played": "gp", "icetime": "pp_icetime",
            "I_F_xGoals": "pp_xg", "I_F_goals": "pp_goals"}
    have = {k: v for k, v in keep.items() if k in df.columns}
    df = df[list(have)].rename(columns=have)
    for col in ("pp_xg", "pp_goals", "pp_icetime"):
        if col in df.columns:
            df[col + "_pg"] = df[col] / df["gp"].clip(lower=1)
    df["pp_toi_pg"] = df.get("pp_icetime_pg", 0) / 60.0
    return df.set_index("player_id")


def mp_teams(season):
    df = get_csv(f"{MP_BASE}/{season}/regular/teams.csv")
    if df is None:
        return None
    df = df[df["situation"] == "all"].copy()
    keep = {"team": "team", "games_played": "gp",
            "xGoalsFor": "xgf", "xGoalsAgainst": "xga",
            "goalsFor": "gf", "goalsAgainst": "ga"}
    have = {k: v for k, v in keep.items() if k in df.columns}
    df = df[list(have)].rename(columns=have)
    for col in ("xgf", "xga", "gf", "ga"):
        if col in df.columns:
            df[col + "_pg"] = df[col] / df["gp"].clip(lower=1)
    return df.set_index("team")


def _blend(cur, pri, gp, k=K_GP):
    if cur is None and pri is None:
        return None
    if pri is None:
        return cur
    if cur is None or not gp:
        return pri
    try:
        if pd.isna(cur): cur = None
        if pd.isna(pri): pri = None
    except (TypeError, ValueError):
        pass
    if cur is None and pri is None:
        return None
    if pri is None:
        return cur
    if cur is None:
        return pri
    return (gp * cur + k * pri) / (gp + k)


class Talent:
    """Blended current+prior MoneyPuck rates, keyed by playerId / team."""

    def __init__(self):
        print(f"MoneyPuck: loading seasons {CUR_SEASON} + {PRIOR_SEASON}...")
        self.sk_cur = mp_skaters(CUR_SEASON)
        self.sk_pri = mp_skaters(PRIOR_SEASON)
        self.tm_cur = mp_teams(CUR_SEASON)
        self.tm_pri = mp_teams(PRIOR_SEASON)
        self.g_cur = mp_goalies(CUR_SEASON)
        self.g_pri = mp_goalies(PRIOR_SEASON)
        self.pp_cur = mp_skaters_pp(CUR_SEASON)
        self.pp_pri = mp_skaters_pp(PRIOR_SEASON)
        if self.sk_cur is None and self.sk_pri is None:
            print("  WARNING: no MoneyPuck skater data at all -- goal probs will be thin.")
        if self.g_cur is not None or self.g_pri is not None:
            print("  goalie quality loaded")
        if self.pp_cur is not None or self.pp_pri is not None:
            print("  power-play units loaded")

    def goalie_by_name(self, name):
        """Find a goalie's quality by name (NHL API gives name, MP keys by id)."""
        for tbl in (self.g_cur, self.g_pri):
            if tbl is None or "name" not in tbl.columns:
                continue
            m = tbl[tbl["name"].str.lower() == (name or "").lower()]
            if not m.empty:
                row = m.iloc[0]
                return {"gsax_60": float(row.get("gsax_60", 0) or 0),
                        "sv_quality": float(row.get("sv_quality", 1) or 1),
                        "src": "current" if tbl is self.g_cur else "prior"}
        return None

    def pp(self, player_id):
        cur = self.pp_cur.loc[player_id].to_dict() if (
            self.pp_cur is not None and player_id in self.pp_cur.index) else None
        pri = self.pp_pri.loc[player_id].to_dict() if (
            self.pp_pri is not None and player_id in self.pp_pri.index) else None
        if cur is None and pri is None:
            return None
        gp = (cur or {}).get("gp", 0) or 0
        return {"pp_toi": _blend((cur or {}).get("pp_toi_pg"), (pri or {}).get("pp_toi_pg"), gp) or 0,
                "pp_xg": _blend((cur or {}).get("pp_xg_pg"), (pri or {}).get("pp_xg_pg"), gp) or 0}

    def skater(self, player_id):
        cur = self.sk_cur.loc[player_id].to_dict() if (
            self.sk_cur is not None and player_id in self.sk_cur.index) else None
        pri = self.sk_pri.loc[player_id].to_dict() if (
            self.sk_pri is not None and player_id in self.sk_pri.index) else None
        if cur is None and pri is None:
            return None
        gp = (cur or {}).get("gp", 0) or 0
        out = {"gp": gp, "src": "blend" if (cur and pri) else ("current" if cur else "prior")}
        for k in ("xg_pg", "shots_pg", "points_pg", "goals_pg", "toi_pg"):
            out[k] = _blend((cur or {}).get(k), (pri or {}).get(k), gp)
        return out

    def team(self, abbr):
        cur = self.tm_cur.loc[abbr].to_dict() if (
            self.tm_cur is not None and abbr in self.tm_cur.index) else None
        pri = self.tm_pri.loc[abbr].to_dict() if (
            self.tm_pri is not None and abbr in self.tm_pri.index) else None
        if cur is None and pri is None:
            return None
        gp = (cur or {}).get("gp", 0) or 0
        out = {}
        for k in ("xgf_pg", "xga_pg", "gf_pg", "ga_pg"):
            out[k] = _blend((cur or {}).get(k), (pri or {}).get(k), gp)
        return out


# ── NHL API layer ─────────────────────────────────────────────────────────
def fetch_schedule(day):
    """Games for a YYYY-MM-DD day via the NHL API."""
    data = get_json(f"https://api-web.nhle.com/v1/schedule/{day}")
    games = []
    for week_day in data.get("gameWeek", []):
        if week_day.get("date") != day:
            continue
        for g in week_day.get("games", []):
            state = g.get("gameState", "FUT")
            mapped = ("Live" if state in ("LIVE", "CRIT")
                      else "Final" if state in ("FINAL", "OFF")
                      else "Preview")
            away, home = g["awayTeam"], g["homeTeam"]

            def full_name(t):
                place = (t.get("placeName", {}) or {}).get("default", "")
                common = (t.get("commonName", {}) or {}).get("default", "")
                nm = f"{place} {common}".strip()
                return nm or t.get("abbrev", "")

            games.append({
                "away_abbr": away["abbrev"],
                "home_abbr": home["abbrev"],
                "away_name": full_name(away),
                "home_name": full_name(home),
                "venue": (g.get("venue", {}) or {}).get("default"),
                "game_time": g.get("startTimeUTC"),
                "game_state": mapped,
                "away_score": away.get("score"),
                "home_score": home.get("score"),
            })
    return games


def fetch_roster(abbr):
    """Skaters + goalies for a team from the NHL API."""
    try:
        data = get_json(f"https://api-web.nhle.com/v1/roster/{abbr}/current")
    except Exception:
        return [], []

    def nm(p):
        f = (p.get("firstName", {}) or {}).get("default", "")
        l = (p.get("lastName", {}) or {}).get("default", "")
        return f"{f} {l}".strip()

    skaters = [(p["id"], nm(p), p.get("positionCode", "F"))
               for grp in ("forwards", "defensemen") for p in data.get(grp, [])]
    goalies = [(p["id"], nm(p)) for p in data.get("goalies", [])]
    return skaters, goalies


# ── injuries (ESPN NHL feed; matched by lowercase name) ──────────────────
_INJ = None

def fetch_injuries():
    global _INJ
    if _INJ is not None:
        return _INJ
    out = {}
    try:
        data = get_json("https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/injuries")

        def walk(node):
            if isinstance(node, dict):
                ath, status = node.get("athlete"), node.get("status")
                if isinstance(ath, dict) and ath.get("displayName") and status:
                    s = status if isinstance(status, str) else (status.get("name") or "")
                    if s:
                        out[ath["displayName"].lower()] = s
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(data)
        if out:
            print(f"Injuries: {len(out)} NHL statuses loaded")
    except Exception as e:
        print(f"  (injury feed unavailable: {e})")
    _INJ = out
    return out


def skater_status(name):
    s = fetch_injuries().get((name or "").lower())
    if not s:
        return None
    sl = s.lower()
    if sl in ("out", "injured reserve", "ir"):
        return "O"
    if "day-to-day" in sl or sl in ("doubtful", "questionable"):
        return "Q"
    return None


# ── Vegas layer (optional) ────────────────────────────────────────────────
def fetch_nhl_odds():
    if not ODDS_API_KEY:
        return {}
    url = (f"https://api.the-odds-api.com/v4/sports/icehockey_nhl/odds"
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
                "total": round(sum(totals) / len(totals), 2),
                "home_spread": round(sum(spreads) / len(spreads), 2) if spreads else None,
            }
    return out


def match_odds(odds_map, away_name, home_name):
    """Odds API uses full names; NHL API name pieces vary. Loose match."""
    a_key = away_name.split()[-1].lower() if away_name else ""
    h_key = home_name.split()[-1].lower() if home_name else ""
    for (a, h), v in odds_map.items():
        if a_key and h_key and a_key in a.lower() and h_key in h.lower():
            return v
    return None


# ── engine ────────────────────────────────────────────────────────────────
def team_lambdas(talent, away, home, line=None):
    """Expected goals per side. Vegas-implied when lines exist, else
    blended xGF vs opponent xGA."""
    if line and line.get("total"):
        total = line["total"]
        spread = line.get("home_spread") or 0.0  # negative = home favored
        home_l = (total - spread) / 2.0
        away_l = (total + spread) / 2.0
        return max(1.5, away_l), max(1.5, home_l), "vegas"
    ta, th = talent.team(away), talent.team(home)
    la = lh = LEAGUE_GOALS_PG
    if ta and th:
        la = (ta.get("xgf_pg") or LEAGUE_GOALS_PG) * ((th.get("xga_pg") or LEAGUE_GOALS_PG) / LEAGUE_GOALS_PG)
        lh = (th.get("xgf_pg") or LEAGUE_GOALS_PG) * ((ta.get("xga_pg") or LEAGUE_GOALS_PG) / LEAGUE_GOALS_PG)
    lh *= HOME_ICE
    return max(1.5, la), max(1.5, lh), "model"


def poisson_matrix(la, lh, cap=9):
    def pmf(l, k):
        return math.exp(-l) * l ** k / math.factorial(k)
    p_away = p_home = p_tie = 0.0
    total_dist = {}
    for a in range(cap + 1):
        for h in range(cap + 1):
            p = pmf(la, a) * pmf(lh, h)
            total_dist[a + h] = total_dist.get(a + h, 0) + p
            if a > h:
                p_away += p
            elif h > a:
                p_home += p
            else:
                p_tie += p
    # OT/SO: split regulation-tie mass with a slight home edge
    p_away += p_tie * 0.48
    p_home += p_tie * 0.52
    p_over_6_5 = sum(v for k, v in total_dist.items() if k >= 7)
    return p_away, p_home, p_over_6_5


def matchup_factor(lam):
    """Dampened team-environment scaler, same philosophy as NFL v2."""
    raw = max(0.75, min(1.30, lam / LEAGUE_GOALS_PG))
    return round(raw ** 0.7, 3)


def _def_grade(opp_xga):
    """Grade opposing team defense by xG allowed per game. Weak D = high grade."""
    if not opp_xga:
        return None
    ratio = opp_xga / LEAGUE_GOALS_PG   # >1 = leaky defense = good matchup
    scale = [(1.12,"A+"),(1.06,"A"),(1.02,"B+"),(0.98,"B"),(0.94,"C+"),(0.90,"C"),(0.85,"D"),(0.0,"F")]
    for thr, g in scale:
        if ratio >= thr:
            return g
    return "F"


def goalie_factor(gsax_60):
    """Opposing goalie quality -> scoring multiplier. A goalie saving +0.5
    goals/60 above expected suppresses scoring; a leaky one inflates it.
    Dampened so an elite goalie is ~-12%, a weak one ~+10%."""
    if gsax_60 is None:
        return 1.0
    # gsax_60 typically ranges roughly -1.0 (bad) to +1.0 (elite)
    return round(max(0.82, min(1.12, 1 - gsax_60 * 0.14)), 3)


def project_skaters(talent, abbr, factor, opp_xga=None, opp_goalie_name=None):
    skaters, goalies = fetch_roster(abbr)
    grade = _def_grade(opp_xga)
    # opposing goalie quality scales every skater's goal probability
    gq = talent.goalie_by_name(opp_goalie_name) if opp_goalie_name else None
    gfac = goalie_factor(gq["gsax_60"]) if gq else 1.0
    pool = []
    for pid, name, pos in skaters:
        t = talent.skater(pid)
        if not t or not t.get("xg_pg"):
            continue
        pool.append({"pid": pid, "name": name, "pos": pos, "t": t,
                     "status": skater_status(name)})

    out_sk = [r for r in pool if r["status"] == "O"]
    active = [r for r in pool if r["status"] != "O"]
    # 60% of an out skater's xG/shots redistribute (line juggling isn't 1:1)
    vac_xg = sum((r["t"].get("xg_pg") or 0) for r in out_sk) * 0.6
    vac_sh = sum((r["t"].get("shots_pg") or 0) for r in out_sk) * 0.6
    out_names = ", ".join(r["name"] for r in out_sk[:2])
    tot_xg = sum((r["t"].get("xg_pg") or 0) for r in active) or 1.0

    result = []
    for r in active:
        t = r["t"]
        base_xg = (t.get("xg_pg") or 0) * factor
        share = (t.get("xg_pg") or 0) / tot_xg
        boost_xg = (base_xg + vac_xg * share * factor) * gfac   # goalie-adjusted
        goal_prob = min(0.75, 1 - math.exp(-max(0.0, boost_xg)))
        base_prob = min(0.75, 1 - math.exp(-max(0.0, base_xg)))
        # power-play unit: real PP1 detection from MoneyPuck PP time-on-ice
        ppd = talent.pp(r["pid"])
        pp1 = bool(ppd and (ppd.get("pp_toi") or 0) >= 2.2)   # ~2.2+ PP min/game = top unit
        p = {
            "name": r["name"], "player_id": r["pid"], "pos": r["pos"],
            "toi": round(t.get("toi_pg") or 0, 1),
            "shots_pg": round((t.get("shots_pg") or 0) + vac_sh * share, 2),
            "xg_pg": round(boost_xg, 3),
            "goal_prob": round(goal_prob, 3),
            "pts_pg": round((t.get("points_pg") or 0) * factor, 2),
            "pp1": pp1,
            "pp_xg": round((ppd or {}).get("pp_xg", 0), 3) if ppd else 0,
            "matchup_grade": grade,
            "src": t["src"],
        }
        if r["status"]:
            p["status"] = r["status"]
        if out_sk and (goal_prob - base_prob) >= 0.03:
            p["news"] = {
                "reason": f"{out_names} ruled OUT",
                "prob_from": round(base_prob, 3),
                "prob_to": round(goal_prob, 3),
            }
        result.append(p)
    result.sort(key=lambda p: p["toi"], reverse=True)
    goalie = goalies[0][1] if goalies else None
    return result[:12], goalie


def build_game(talent, raw, odds_map):
    away, home = raw["away_abbr"], raw["home_abbr"]
    line = match_odds(odds_map, raw["away_name"], raw["home_name"])
    la, lh, source = team_lambdas(talent, away, home, line)
    p_away, p_home, p_over = poisson_matrix(la, lh)

    edge = abs(p_home - 0.5)
    tier = "STRONG" if edge >= 0.12 else "LEAN" if edge >= 0.06 else "PASS"
    target = round(50 + edge * 200 + (p_over - 0.5) * 40)

    fa, fh = matchup_factor(la), matchup_factor(lh)
    ta_def, th_def = talent.team(away), talent.team(home)
    away_xga = (th_def or {}).get("xga_pg")   # away skaters face home defense
    home_xga = (ta_def or {}).get("xga_pg")

    # resolve probable goalies first so each side's skaters are graded against
    # the ACTUAL netminder they'll face
    away_g_list = fetch_roster(away)[1]
    home_g_list = fetch_roster(home)[1]
    away_goalie = away_g_list[0][1] if away_g_list else None
    home_goalie = home_g_list[0][1] if home_g_list else None

    # away skaters face the HOME goalie; home skaters face the AWAY goalie
    away_skaters, _ = project_skaters(talent, away, fa, away_xga, opp_goalie_name=home_goalie)
    home_skaters, _ = project_skaters(talent, home, fh, home_xga, opp_goalie_name=away_goalie)

    # goalie quality + Brick Wall grade for display
    def goalie_card(name):
        gq = talent.goalie_by_name(name) if name else None
        if not gq:
            return {"name": name, "grade": None, "gsax": None}
        gsax = gq["gsax_60"]
        scale = [(0.6,"A+"),(0.35,"A"),(0.15,"B+"),(0.0,"B"),(-0.2,"C+"),(-0.4,"C"),(-0.7,"D"),(-99,"F")]
        grade = next(g for thr,g in scale if gsax >= thr)
        return {"name": name, "grade": grade, "gsax": round(gsax, 2),
                "brick_wall": gsax >= 0.35}

    game = {
        "away_team": raw["away_name"], "home_team": raw["home_name"],
        "away_abbr": away, "home_abbr": home,
        "venue": raw.get("venue"), "game_time": raw.get("game_time"),
        "game_state": raw.get("game_state", "Preview"),
        "away_goals": round(la, 2), "home_goals": round(lh, 2),
        "away_win_pct": round(p_away, 3), "home_win_pct": round(p_home, 3),
        "p_over_6_5": round(p_over, 3),
        "tier": tier, "target_score": max(0, min(100, target)),
        "line_source": source,
        "away_goalie": away_goalie, "home_goalie": home_goalie,
        "away_goalie_card": goalie_card(away_goalie),
        "home_goalie_card": goalie_card(home_goalie),
        "away_skaters": away_skaters, "home_skaters": home_skaters,
    }
    if raw.get("away_score") is not None:
        game["away_score"] = raw["away_score"]
        game["home_score"] = raw["home_score"]
    return game


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else _today_et()
    print(f"NHL Projections for {day}")

    try:
        games_raw = fetch_schedule(day)
    except Exception as e:
        print(f"Schedule fetch failed: {e}")
        games_raw = []
    print(f"{len(games_raw)} games on the slate")

    games = []
    if games_raw:
        talent = Talent()
        odds_map = fetch_nhl_odds()
        if odds_map:
            print(f"Odds matched for {len(odds_map)} events (Vegas anchoring on)")
        for raw in games_raw:
            print(f"  {raw['away_abbr']} @ {raw['home_abbr']}...")
            try:
                games.append(build_game(talent, raw, odds_map))
            except Exception as e:
                print(f"    skipped ({e})")

    all_sk = [s for g in games for s in g["away_skaters"] + g["home_skaters"]]
    top_goal = sorted(all_sk, key=lambda s: s["goal_prob"], reverse=True)[:10]

    export = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "date": day,
        "games": games,
        "standouts": {"top_goal": top_goal},
    }
    try:
        prev_path = Path(__file__).parent / "data" / "nhl_slate.json"
        if prev_path.exists():
            prev = json.loads(prev_path.read_text())
            prev_sk = {}
            for pg in prev.get("games", []):
                for side in ("away_skaters", "home_skaters"):
                    for pp in pg.get(side, []) or []:
                        prev_sk[pp.get("name")] = pp
            for g in export.get("games", []):
                for side in ("away_skaters", "home_skaters"):
                    for pp in g.get(side, []) or []:
                        old_p = prev_sk.get(pp.get("name"))
                        if old_p and "news" not in pp:
                            d = (pp.get("xg_pg") or 0) - (old_p.get("xg_pg") or 0)
                            if abs(d) >= 0.06:
                                pp["xg_prev"] = old_p.get("xg_pg")
    except Exception:
        pass
    OUT_PATH.write_text(json.dumps(export, indent=2))
    print(f"Wrote {len(games)} games to {OUT_PATH}")


if __name__ == "__main__":
    main()
