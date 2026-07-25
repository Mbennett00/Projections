#!/usr/bin/env python3
"""
injuries_projections.py
Slate — league-wide injury report puller

Mirrors the role mlb_projections.py / nfl_projections.py / etc. play for
their boards: this script produces injuries_slate.json in the shape
injuries/index.html expects, then the GitHub Actions workflow commits it
to data/ alongside the other slate files.

Source: ESPN's public "site" API, same family of endpoint the other
scripts already use for scoreboards/rosters. No API key required.
    https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/injuries

This is unofficial/undocumented ESPN JSON, so field names below are
best-effort based on the shape ESPN uses elsewhere in this API family —
every read goes through .get() with fallbacks, and one league failing
never takes down the others.

Usage:
    python3 injuries_projections.py
Output:
    injuries_slate.json  (written next to this script)
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

try:
    import requests
except ImportError:
    requests = None
    import urllib.request

OUT_PATH = Path(__file__).parent / "injuries_slate.json"

# sport/league path segments + a short label for the UI
LEAGUES = [
    {"key": "nfl", "label": "NFL", "sport": "football", "league": "nfl"},
    {"key": "nba", "label": "NBA", "sport": "basketball", "league": "nba"},
    {"key": "mlb", "label": "MLB", "sport": "baseball", "league": "mlb"},
    {"key": "nhl", "label": "NHL", "sport": "hockey", "league": "nhl"},
]

ESPN_INJURIES = "https://site.api.espn.com/apis/site/v2/sports/{sport}/{league}/injuries"

TIMEOUT = 15


def _get_json(url):
    """GET url and return parsed JSON, or None on any failure."""
    try:
        if requests:
            r = requests.get(url, timeout=TIMEOUT, headers={"Accept": "application/json"})
            r.raise_for_status()
            return r.json()
        else:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ! fetch failed for {url}: {e}", file=sys.stderr)
        return None


def _pick(*vals):
    for v in vals:
        if v:
            return v
    return ""


def _parse_team_block(block):
    """One team's worth of injuries -> our normalized shape.

    Uses a recursive walk (not a fixed key path) to find every
    {athlete, status} pair nested anywhere inside this team's subtree --
    the same defensive approach already proven live in this app's
    nba/nfl/nhl _projections.py (their fetch_injuries()/walk()), rather
    than assuming one exact nesting shape for a brand-new endpoint call.
    """
    team = block.get("team", block)
    team_name = _pick(team.get("displayName"), team.get("name"), team.get("shortDisplayName"))
    team_abbr = _pick(team.get("abbreviation"), team.get("shortDisplayName"), team_name[:3].upper() if team_name else "")
    team_logo = team.get("logo") or (team.get("logos") or [{}])[0].get("href", "")

    seen_ids = set()
    players = []

    def walk(node):
        if isinstance(node, dict):
            athlete = node.get("athlete")
            status = node.get("status")
            if isinstance(athlete, dict) and athlete.get("id") and status:
                pid = str(athlete["id"])
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    s = status if isinstance(status, str) else (
                        (status.get("name") if isinstance(status, dict) else None)
                        or (status.get("type", {}) or {}).get("name")
                        or (status.get("type", {}) or {}).get("description")
                    )
                    itype = node.get("type", {}) if isinstance(node.get("type"), dict) else {}
                    players.append({
                        "name": _pick(athlete.get("displayName"), athlete.get("shortName"), "Unknown"),
                        "position": (athlete.get("position") or {}).get("abbreviation", ""),
                        "status": _pick(s, "Unknown"),
                        "detail": _pick(node.get("detail"), node.get("shortComment"),
                                         node.get("longComment"), itype.get("description"), ""),
                        "location": _pick(node.get("location"), itype.get("name"), ""),
                        "date": node.get("date", ""),
                    })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(block.get("injuries", block))

    return {
        "team": team_name,
        "abbreviation": team_abbr,
        "logo": team_logo,
        "players": players,
    }


def fetch_league(cfg):
    url = ESPN_INJURIES.format(sport=cfg["sport"], league=cfg["league"])
    print(f"Fetching {cfg['label']} injuries…")
    data = _get_json(url)
    if not data:
        return {"key": cfg["key"], "label": cfg["label"], "teams": [], "error": "fetch_failed"}

    raw_teams = data.get("injuries", []) or []
    teams = [_parse_team_block(b) for b in raw_teams]
    # drop teams that came back with no players (nothing to show)
    teams = [t for t in teams if t["players"]]
    # sort by most players out first, alphabetical after that — most useful teams surface first
    teams.sort(key=lambda t: (-len(t["players"]), t["team"]))

    return {"key": cfg["key"], "label": cfg["label"], "teams": teams}


def main():
    now = datetime.now(_ET) if _ET else datetime.now(timezone.utc)

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(fetch_league, LEAGUES))

    if all(r.get("error") for r in results):
        print("All league fetches failed — leaving last good data/injuries_slate.json untouched.")
        sys.exit(0)

    total_players = sum(len(t["players"]) for lg in results for t in lg["teams"])

    out = {
        "generated_at": now.isoformat(),
        "leagues": results,
        "total_players": total_players,
    }

    OUT_PATH.write_text(json.dumps(out, indent=2))
    print(f"Wrote {OUT_PATH} — {total_players} injured players across {len(results)} leagues")


if __name__ == "__main__":
    main()
