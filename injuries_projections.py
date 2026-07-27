import json
import time
import requests
from bs4 import BeautifulSoup


def fetch_cbs_injuries(sport):
    """Scrapes clean multi-sport data from CBS Sports' open injury layouts."""
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
        rows = soup.find_all("tr", class_="TableBase-bodyTr")

        for row in rows:
            cols = row.find_all("td")
            if len(cols) >= 3:
                name_cell = cols[0].get_text(strip=True)
                position_cell = cols[1].get_text(strip=True)

                # Differentiate cell length structures safely
                status_cell = (
                    cols[2].get_text(strip=True)
                    if len(cols) >= 3
                    else "Reported"
                )
                detail_cell = (
                    cols[3].get_text(strip=True)
                    if len(cols) >= 4
                    else "Undisclosed"
                )

                team_div = row.find_previous("span", class_="TeamName")
                team_name = (
                    team_div.get_text(strip=True) if team_div else "League Pool"
                )

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
                        "injury_status": status_cell,
                        "injury_detail": detail_cell,
                        "last_updated": current_time,
                    }
                )

        return players_list
    except Exception as e:
        print(f"Error scraping {sport.upper()}: {e}")
        return []


def run_pipeline():
    combined_injuries = []

    # Scraping loops across your 4 target dashboard sports
    for sport in ["nfl", "nba", "nhl", "mlb"]:
        print(f"Scraping live {sport.upper()} injury updates...")
        data = fetch_cbs_injuries(sport)
        combined_injuries.extend(data)
        time.sleep(1)

    with open("injuries_slate.json", "w", encoding="utf-8") as f:
        json.dump(combined_injuries, f, indent=2)

    print(
        f"\nPipeline Finished! Saved {len(combined_injuries)} profiles to injuries_slate.json"
    )


if __name__ == "__main__":
    run_pipeline()
