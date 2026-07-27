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
import time
import requests
from bs4 import BeautifulSoup


def fetch_cbs_injuries(sport):
    """Scrapes multi-sport data directly from CBS Sports' open layout pages."""
    url = f"https://www.cbssports.com/{sport.lower()}/injuries/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        players_list = []
        current_time = int(time.time())

        # Find team containers or table headers
        team_headers = soup.find_all(["h3", "div"], class_="TeamName")
        tables = soup.find_all("table", class_="TableBase-table")

        # Fallback if specific classes shift: process all content blocks
        rows = soup.find_all("tr", class_="TableBase-bodyTr")

        for row in rows:
            cols = row.find_all("td")
            if len(cols) >= 3:
                # Scrape cell values safely
                name_cell = cols[0].get_text(strip=True)
                position_cell = cols[1].get_text(strip=True)

                # Differentiate injury status and detail layout variations
                status_cell = (
                    cols[3].get_text(strip=True)
                    if len(cols) >= 4
                    else "Reported"
                )
                detail_cell = cols[2].get_text(strip=True)

                # Pull ancestral team structure mapping from parents
                team_div = row.find_previous("span", class_="TeamName")
                team_name = (
                    team_div.get_text(strip=True) if team_div else "League Pool"
                )

                # Clean parsed names containing duplicate text strings
                clean_name = name_cell
                if "  " in clean_name:
                    clean_name = clean_name.split("  ")[0]

                players_list.append(
                    {
                        "player_id": clean_name.lower().replace(" ", "-"),
                        "name": clean_name,
                        "sport": sport.lower(),
                        "team": team_name,
                        "position": position_cell,
                        "injury_status": status_cell if status_cell else "Out",
                        "injury_detail": detail_cell
                        if detail_cell
                        else "Undisclosed",
                        "last_updated": current_time,
                    }
                )

        return players_list
    except Exception as e:
        print(f"Error scraping {sport.upper()}: {e}")
        return []


def run_pipeline():
    combined_injuries = []

    # Iterate through your 3 required projection leagues
    for sport in ["nfl", "nba", "nhl"]:
        print(f"Scraping live {sport.upper()} injury updates...")
        data = fetch_cbs_injuries(sport)
        combined_injuries.extend(data)
        time.sleep(1)  # Space calls politely

    with open("injuries.json", "w", encoding="utf-8") as f:
        json.dump(combined_injuries, f, indent=2)

    print(
        f"\nPipeline Finished! Saved {len(combined_injuries)} profiles across 3 leagues to injuries.json"
    )


if __name__ == "__main__":
    run_pipeline()

