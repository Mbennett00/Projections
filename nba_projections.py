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
MARGIN_SD = 13.0        # NBA final margins, standard deviation in points
HOME_COURT_PTS = 2.5    # modern NBA home-court advantage, in points
TOTAL_SD = 17.0         # combined-score standard deviation

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
        return []
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
    """O = out, D = doubtful, Q = questionable, None = expected to play.

    Matched on substrings, not equality. ESPN returns strings like "Out For
    Season", "Injured Reserve" and "Game Time Decision"; the previous exact
    match only recognised "out", "injured" and "suspension", so a player ruled
    out for the season came back as None and was projected to play a full game.
    That silently broke the injury redistribution for exactly the long-term
    absences it matters most for.
    """
    s = fetch_injuries().get(str(player_id))
    if not s:
        return None
    sl = s.lower().strip()
    if any(k in sl for k in ("out", "injured reserve", " ir", "suspen",
                             "not with team", "inactive")):
        return "O"
    if "doubtful" in sl:
        return "D"
    if any(k in sl for k in ("questionable", "day-to-day", "day to day",
                             "game time", "game-time", "probable")):
        return "Q"
    return None


# ── Vegas ─────────────────────────────────────────────────────────────────
def fetch_odds():
    if not ODDS_API_KEY:
        return {}
    url = (f"https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
           f"?apiKey={ODDS_API_KEY}&regions=us&markets=spreads,totals,h2h&bookmakers=fanduel&oddsFormat=american")
    try:
        rows = get_json(url)
    except Exception as e:
        print(f"  (odds fetch failed: {e})")
        return {}
    out = {}
    for ev in rows:
        totals, spreads, ml_away, ml_home = [], [], [], []
        # Prices, not just the points. The boards show a real priced market
        # (e.g. "TOR -1.5 (+124)"), which needs the odds on each side too.
        over_px, under_px, sp_away_px, sp_home_px = [], [], [], []
        for bk in ev.get("bookmakers", []):
            if bk.get("key") != "fanduel":
                continue
            for mk in bk.get("markets", []):
                for o in mk.get("outcomes", []):
                    if mk["key"] == "totals":
                        if o.get("name") == "Over":
                            if o.get("point") is not None:
                                totals.append(o["point"])
                            if o.get("price") is not None:
                                over_px.append(o["price"])
                        elif o.get("name") == "Under" and o.get("price") is not None:
                            under_px.append(o["price"])
                    if mk["key"] == "spreads":
                        if o.get("name") == ev.get("home_team"):
                            if o.get("point") is not None:
                                spreads.append(o["point"])
                            if o.get("price") is not None:
                                sp_home_px.append(o["price"])
                        elif o.get("name") == ev.get("away_team") and o.get("price") is not None:
                            sp_away_px.append(o["price"])
                    if mk["key"] == "h2h" and o.get("price") is not None:
                        if o.get("name") == ev.get("away_team"):
                            ml_away.append(o["price"])
                        elif o.get("name") == ev.get("home_team"):
                            ml_home.append(o["price"])
        if totals or (ml_away and ml_home):
            out[(ev.get("away_team"), ev.get("home_team"))] = {
                "total": round(sum(totals) / len(totals), 1) if totals else None,
                "home_spread": round(sum(spreads) / len(spreads), 1) if spreads else None,
                "ml_away": round(sum(ml_away) / len(ml_away)) if ml_away else None,
                "ml_home": round(sum(ml_home) / len(ml_home)) if ml_home else None,
                "over_price": round(sum(over_px) / len(over_px)) if over_px else None,
                "under_price": round(sum(under_px) / len(under_px)) if under_px else None,
                "spread_away_price": round(sum(sp_away_px) / len(sp_away_px)) if sp_away_px else None,
                "spread_home_price": round(sum(sp_home_px) / len(sp_home_px)) if sp_home_px else None,
            }
    return out


def match_odds(odds_map, away_name, home_name):
    a = (away_name or "").split()[-1].lower()
    h = (home_name or "").split()[-1].lower()
    for (ak, hk), v in odds_map.items():
        if a and h and a in ak.lower() and h in hk.lower():
            return v
    return None


def american_to_implied(price):
    if price is None:
        return None
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


# ---------------------------------------------------------------------------
# EDGE CONFIDENCE SCORE -- shared 0-100 scale across all four sports. See
# mlb_projections.py for the full rationale.
# ---------------------------------------------------------------------------
EDGE_SCORE_K = 6.0

def edge_confidence_score(edge_pts, k=EDGE_SCORE_K):
    score = round(100 * (1 - math.exp(-abs(edge_pts) / k)))
    tier = "ELITE" if score >= 75 else "HIGH" if score >= 50 else "MEDIUM" if score >= 25 else "LOW"
    return score, tier


# ── projections ───────────────────────────────────────────────────────────
# ---------------------------------------------------------------------------
# Injury impact
#
# Vacated minutes and production used to be shared out in proportion to each
# active player's own minutes, and nothing else. That treats every teammate as
# interchangeable: with Luka out, a centre absorbed the same share of his
# assists as the backup point guard, and a guard absorbed the same share of a
# centre's rebounds.
#
# Redistribution is now role-aware, on two axes that need no new data:
#
#   minutes    flow toward players at the same position -- a starting guard
#              sitting frees guard minutes, not centre minutes.
#   production flows toward players who already produce THAT stat per minute.
#              Assists go to ball-handlers, rebounds to bigs, because those are
#              the players already doing it when they're on the floor.
# ---------------------------------------------------------------------------
POS_ORDER = {"PG": 0, "G": 0.5, "SG": 1, "GF": 1.5, "SF": 2,
             "F": 2.5, "PF": 3, "FC": 3.5, "C": 4}


def pos_affinity(a, b):
    """1.0 for the same position, falling away with positional distance."""
    pa, pb = POS_ORDER.get((a or "").upper()), POS_ORDER.get((b or "").upper())
    if pa is None or pb is None:
        return 0.6                      # unknown position: middling, not zero
    d = abs(pa - pb)
    if d <= 0.5:
        return 1.0
    if d <= 1.5:
        return 0.65
    if d <= 2.5:
        return 0.35
    return 0.15


def _weights(active, out_players, key, stat=None):
    """Share of a vacated resource each active player should absorb.

    `stat` is None for minutes (weighted by position fit) or a stat name for
    production (weighted by that player's own per-minute rate). Falls back to
    minute share whenever the signal is missing, which is the old behaviour.
    """
    w = []
    for r in active:
        base_min = r["b"].get("min") or 0
        if stat is None:
            fit = max(pos_affinity(r["pos"], o["pos"]) for o in out_players) if out_players else 1.0
            w.append(base_min * fit)
        else:
            rate = (r["b"].get(stat) or 0) / base_min if base_min else 0
            w.append(rate * base_min)   # = the stat itself, i.e. who already does it
    total = sum(w)
    if total <= 0:
        total = sum((r["b"].get("min") or 0) for r in active) or 1.0
        w = [(r["b"].get("min") or 0) for r in active]
    return [x / total for x in w]


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

    # Role-aware shares: one set for minutes, one per stat.
    min_share = _weights(active, out_players, "min")
    stat_share = {k: _weights(active, out_players, "min", k) for k in ("pts", "reb", "ast", "tpm")}

    out = []
    for idx, r in enumerate(active):
        b = r["b"]
        base_min = b.get("min") or 0
        share = min_share[idx]
        # redistribute up to 85% of vacated minutes, capped so nobody exceeds 42
        boost_min = min(42, base_min + vac_min * share * 0.85)
        # blowout risk: in likely blowouts, high-minute starters sit late.
        # blowout is 0..1; caps a 34-min starter toward ~30 at full blowout.
        if blowout > 0 and boost_min >= 30:
            boost_min *= (1 - blowout * 0.12 * ((boost_min - 30) / 12 + 0.5))
        min_scale = (boost_min / base_min) if base_min else 1.0

        def proj(stat):
            base = (b.get(stat) or 0)
            # Redistributed production now uses the share for THIS stat, so
            # assists follow ball-handlers and rebounds follow bigs.
            redist = vac[stat] * stat_share[stat][idx] * 0.85
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
        # Injury impact, written so the card can state it plainly: who is out,
        # how many minutes this player gains, and how much of the vacated
        # scoring load he actually absorbs. The usage delta is the part that
        # matters -- extra minutes for a low-usage big are worth far less than
        # the same minutes for the guard inheriting the ball.
        if out_players:
            d_min = boost_min - base_min
            base_usage = min(0.45, ((b.get("pts") or 0) + (b.get("ast") or 0) * 0.5)
                             / max(1, base_min) * 2.4)
            d_usage = usage - base_usage
            if d_min >= 1.0 or abs(d_usage) >= 0.01:
                p["injury_impact"] = {
                    "out": [o["name"] for o in out_players[:3]],
                    "min_from": round(base_min, 1), "min_to": round(boost_min, 1),
                    "min_delta": round(d_min, 1),
                    "usage_from": round(base_usage, 3), "usage_to": usage,
                    "usage_delta": round(d_usage, 3),
                }
                bits = []
                if d_min >= 1.0:
                    bits.append(f"+{d_min:.0f} min")
                if d_usage >= 0.01:
                    bits.append(f"+{d_usage*100:.0f}% usage")
                p["usage_note"] = f"{out_names} OUT \u00b7 " + ", ".join(bits) if bits else None
            if d_min >= 1.5:
                p["news"] = {
                    "reason": f"{out_names} OUT",
                    "min_from": round(base_min, 1), "min_to": round(boost_min, 1),
                }
        out.append(p)
    out.sort(key=lambda p: p["min"], reverse=True)
    return out[:10]


def norm_cdf(z):
    """Standard normal CDF. Used to turn a point margin into a win probability."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_over_total(away_pts, home_pts, line, sd=None):
    """P(combined score clears the posted total). Totals vary more than margins."""
    if not line:
        return None
    sd = sd or TOTAL_SD
    return 1.0 - norm_cdf((line - (away_pts + home_pts)) / sd)


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

    # defense grades (each side graded vs the opponent's points allowed)
    away_def = fetch_def_rating(raw["away_id"], CUR_SEASON) or fetch_def_rating(raw["away_id"], PRIOR_SEASON)
    home_def = fetch_def_rating(raw["home_id"], CUR_SEASON) or fetch_def_rating(raw["home_id"], PRIOR_SEASON)
    for p in away_players:      # away players face home defense
        p["matchup_grade"] = matchup_grade(home_def)
    for p in home_players:
        p["matchup_grade"] = matchup_grade(away_def)

    # Projected team totals from the roster projections. The board reads these
    # directly, and the no-spread fallback below needs a margin to work from.
    away_pts = round(sum((p.get("pts") or 0) for p in away_players), 1)
    home_pts = round(sum((p.get("pts") or 0) for p in home_players), 1)

    # Win probability.
    #
    # Previously: 1 / (1 + 10 ** (spread / 8.0)), which put an 8-point favourite
    # at 90.9%. Real NBA is ~73%. That divisor manufactured ~17 points of
    # spurious edge on every non-close game. NBA margins are approximately
    # normal with a standard deviation around 13 points, so the normal CDF is
    # the standard mapping and is used here instead.
    #
    # The old fallback (0.5 * HOME_EDGE) returned the same number for every
    # game regardless of who was playing. Against a real moneyline that
    # produced a full slate of fictional edges whenever FanDuel had a price up
    # but no spread posted -- common early in the season. It now falls back to
    # the projected margin plus home court.
    if spread is not None:
        margin = -spread                      # spread is the HOME number
        source_wp = "spread"
    else:
        margin = (home_pts - away_pts) + HOME_COURT_PTS
        source_wp = "projection"
    home_win = norm_cdf(margin / MARGIN_SD)
    home_win = max(0.05, min(0.95, home_win))

    # P(over) against the posted total, and an offence rating comparable to the
    # other sports' TARGET: how strong a scoring environment this game projects.
    p_over = prob_over_total(away_pts, home_pts, total)
    proj_total = away_pts + home_pts
    target_score = int(round(max(0, min(100,
        50 + (proj_total - LEAGUE_PACE_TOTAL) * 1.6))))

    edge = abs(home_win - 0.5)
    tier = "STRONG" if edge >= 0.15 else "LEAN" if edge >= 0.07 else "PASS"

    # Model-vs-market moneyline edge (the "Edge Confidence Score" on the
    # board). Only computed when a real book moneyline exists to check
    # against -- comparing the spread-implied win% to the moneyline market's
    # own devigged win% surfaces real cross-market disagreement.
    game_edge = None
    if line and line.get("ml_away") is not None and line.get("ml_home") is not None:
        market_away, market_home = devig_pair(line["ml_away"], line["ml_home"])
        if market_home is not None:
            home_edge = (home_win - market_home) * 100
            away_edge = ((1 - home_win) - market_away) * 100
            if abs(home_edge) >= abs(away_edge):
                best_edge, best_team = home_edge, raw["home_team"]
            else:
                best_edge, best_team = away_edge, raw["away_team"]
            g_score, g_confidence = edge_confidence_score(best_edge)
            game_edge = {"team": best_team, "edge_pct": round(best_edge, 1),
                         "score": g_score, "confidence": g_confidence,
                         "away_ml": line["ml_away"], "home_ml": line["ml_home"], "book": "FanDuel"}

    game = {
        "away_team": raw["away_team"], "home_team": raw["home_team"],
        "away_abbr": raw["away_abbr"], "home_abbr": raw["home_abbr"],
        "venue": raw.get("venue"), "game_time": raw.get("game_time"),
        "game_state": raw.get("game_state", "Preview"),
        "away_win_pct": round(1 - home_win, 3), "home_win_pct": round(home_win, 3),
        # Basketball scores points, not goals. These field names were copied
        # from the NHL board when this model was written; the slate is empty
        # in the offseason, so renaming them now costs nothing.
        "away_points": away_pts, "home_points": home_pts,
        "p_over_total": None if p_over is None else round(p_over, 3),
        "target_score": target_score,
        "total": total, "spread": spread,
        "tier": tier, "line_source": source, "blowout_risk": round(blowout, 2), "edge": game_edge,
        "moneylines": {"away": line.get("ml_away"), "home": line.get("ml_home"), "book": "FanDuel"} if line else None,
        # Canonical betting-line block every board reads. Same key names across
        # all four sports so the front end has one shape to handle. `spread` is
        # always the HOME number (negative = home favoured).
        "_lines": {
            "book": "FanDuel",
            "ml_away": line.get("ml_away"), "ml_home": line.get("ml_home"),
            "spread": line.get("home_spread"),
            "spread_away_price": line.get("spread_away_price"),
            "spread_home_price": line.get("spread_home_price"),
            "total": line.get("total"),
            "over_price": line.get("over_price"),
            "under_price": line.get("under_price"),
        } if line else None,
        "away_players": away_players, "home_players": home_players,
    }
    if raw.get("away_score") is not None:
        game["away_score"] = raw["away_score"]
        game["home_score"] = raw["home_score"]
    return game


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else _today_et()
    print(f"NBA Projections for {day}  (season {CUR_SEASON}, prior {PRIOR_SEASON})")

    games_raw = fetch_schedule(day)
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
