#!/usr/bin/env python3
"""
calibration_log.py -- records what the model predicted vs what the market
priced vs what actually happened, one row per game, so model accuracy can be
measured instead of argued about.

Why this exists
---------------
A single slate showed the MLB model's win probabilities correlating at
r = -0.47 against FanDuel's devigged prices, agreeing on the favourite in only
5 of 15 games. That is a suspicious result, not a proven one: n=15, p~0.07.
This accumulates the sample needed to tell the difference between a broken
model and a noisy night.

Design notes
------------
* Append-and-update, keyed by (date, away, home, start). Runs several times a
  day; each run overwrites the prediction with the freshest one and fills in
  the outcome once the game is Final. The LAST prediction before first pitch is
  the closest thing to a closing line this pipeline can produce.
* Never deletes rows. A model change should be visible as a break in the
  series, not silently rewrite history.
* Stdlib only, so it can't break the Action by way of a dependency.
"""

import csv
import json
import os
import sys
from datetime import datetime, timezone

FIELDS = [
    "date", "sport", "away", "home", "start",
    "model_away_win",     # what the model said
    "book_away_win",      # FanDuel, devigged
    "ml_away", "ml_home",
    "edge_pts",           # model - book, in percentage points
    "away_score", "home_score", "winner",   # filled once Final
    "state", "logged_at", "settled_at",
]


def devig(ml_away, ml_home):
    """American odds -> implied probabilities with the vig proportionally removed."""
    if ml_away is None or ml_home is None:
        return None
    def imp(o):
        return 100.0 / (o + 100.0) if o > 0 else -o / (-o + 100.0)
    a, h = imp(ml_away), imp(ml_home)
    total = a + h
    return a / total if total else None


def key(row):
    return (row["date"], row["away"], row["home"], row["start"])


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {key(r): r for r in csv.DictReader(f)}


def save(path, rows):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: (r["date"], r["start"], r["home"]))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(ordered)


def collect(slate_path, sport, existing):
    """Merge one slate file into the log. Returns (added, updated, settled)."""
    if not os.path.exists(slate_path):
        return 0, 0, 0
    with open(slate_path, encoding="utf-8") as f:
        slate = json.load(f)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = updated = settled = 0

    for g in slate.get("games", []):
        away, home = g.get("away_team"), g.get("home_team")
        if not away or not home:
            continue

        ml = g.get("moneylines") or {}
        lines = g.get("_lines") or {}
        ml_away = ml.get("away", lines.get("ml_away"))
        ml_home = ml.get("home", lines.get("ml_home"))
        book = devig(ml_away, ml_home)
        model = g.get("away_win_pct")
        state = g.get("game_state") or ""

        row = {
            "date": slate.get("date", ""), "sport": sport,
            "away": away, "home": home, "start": g.get("game_time", "") or "",
            "model_away_win": "" if model is None else round(model, 4),
            "book_away_win": "" if book is None else round(book, 4),
            "ml_away": "" if ml_away is None else ml_away,
            "ml_home": "" if ml_home is None else ml_home,
            "edge_pts": "" if (model is None or book is None) else round((model - book) * 100, 2),
            "away_score": "", "home_score": "", "winner": "",
            "state": state, "logged_at": now, "settled_at": "",
        }

        a_sc, h_sc = g.get("away_score"), g.get("home_score")
        if state == "Final" and a_sc is not None and h_sc is not None:
            row["away_score"], row["home_score"] = a_sc, h_sc
            row["winner"] = "away" if a_sc > h_sc else "home" if h_sc > a_sc else "tie"
            row["settled_at"] = now

        k = key(row)
        prev = existing.get(k)
        if prev is None:
            existing[k] = row
            added += 1
            continue

        # Keep the earliest logged_at; it marks when this game first appeared.
        row["logged_at"] = prev.get("logged_at") or now

        # Never overwrite a settled result, and never let a later run blank out
        # a prediction we already captured (odds get pulled once a game starts).
        if prev.get("winner"):
            for f in ("away_score", "home_score", "winner", "settled_at"):
                row[f] = prev[f]
        elif row["winner"]:
            settled += 1

        for f in ("model_away_win", "book_away_win", "ml_away", "ml_home", "edge_pts"):
            if row[f] == "" and prev.get(f):
                row[f] = prev[f]

        if row != prev:
            existing[k] = row
            updated += 1

    return added, updated, settled


def report(rows):
    """Calibration by predicted-probability bucket, plus model vs market accuracy."""
    done = [r for r in rows.values() if r.get("winner") in ("away", "home") and r.get("model_away_win")]
    if not done:
        print("\nNo settled games logged yet -- nothing to score.")
        return

    print(f"\nCalibration over {len(done)} settled games")
    print("  bucket        n   predicted   actual   gap")
    buckets = [(0, .35), (.35, .45), (.45, .55), (.55, .65), (.65, 1.01)]
    for lo, hi in buckets:
        sel = [r for r in done if lo <= float(r["model_away_win"]) < hi]
        if not sel:
            continue
        pred = sum(float(r["model_away_win"]) for r in sel) / len(sel)
        act = sum(1 for r in sel if r["winner"] == "away") / len(sel)
        print("  %.2f-%.2f  %4d      %5.1f%%   %5.1f%%  %+5.1f" % (
            lo, hi, len(sel), pred * 100, act * 100, (act - pred) * 100))

    def hits(field):
        sel = [r for r in done if r.get(field)]
        if not sel:
            return None, 0
        ok = sum(1 for r in sel
                 if (float(r[field]) > .5) == (r["winner"] == "away"))
        return ok / len(sel), len(sel)

    m, mn = hits("model_away_win")
    b, bn = hits("book_away_win")
    print("\n  picked the winner:")
    if m is not None:
        print("    model    %5.1f%%  (%d games)" % (m * 100, mn))
    if b is not None:
        print("    FanDuel  %5.1f%%  (%d games)" % (b * 100, bn))
    if m is not None and b is not None:
        print("    -> the model must beat the book here for its edges to mean anything")


def main():
    log_path = os.environ.get("CALIB_LOG", "data/calibration_log.csv")
    data_dir = os.environ.get("CALIB_DATA", "data")
    rows = load(log_path)
    before = len(rows)

    for sport in ("mlb", "nfl", "nhl", "nba"):
        a, u, s = collect(os.path.join(data_dir, f"{sport}_slate.json"), sport, rows)
        if a or u or s:
            print(f"  {sport}: +{a} new, {u} updated, {s} settled")

    save(log_path, rows)
    print(f"\ncalibration log: {before} -> {len(rows)} rows  ({log_path})")

    if "--report" in sys.argv:
        report(rows)


if __name__ == "__main__":
    main()
