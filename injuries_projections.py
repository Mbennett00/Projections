import json
import time
import requests
from bs4 import BeautifulSoup

def fetch_cbs_injuries(sport):
    """
    Scrapes clean data from CBS Sports' layout pages, 
    safely handling variations between sports columns.
    """
    url = f"https://www.cbssports.com/{sport.lower()}/injuries/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        players_list = []
        current_time = int(time.time())

        # Target CBS Sports data row wrappers directly
        rows = soup.find_all("tr", class_="TableBase-bodyTr")

        for row in rows:
            cols = row.find_all("td")

            if len(cols) >= 3:
                # FIX DOUBLE NAMES: Target ONLY the explicit anchor link text inside the column
                name_anchor = cols[0].find("a")
                if name_anchor:
                    name = name_anchor.get_text(strip=True)
                else:
                    # Alternative clean text split fallback
                    name = cols[0].get_text(strip=True)

                # Strip empty entries or standard table structural labels
                if not name or name.lower() in ["player", "position", "status", "injury", "injury / date"]:
                    continue

                # Safely extract table cell arrays across different league schedules
                position = cols[1].get_text(strip=True) if len(cols) > 1 else "N/A"
                status = cols[2].get_text(strip=True) if len(cols) > 2 else "Reported"
                
                # Capture the explicit body issue string safely
                detail = cols[3].get_text(strip=True) if len(cols) > 3 else "Undisclosed"
                if len(cols) >= 5 and "week" in status.lower():
                    # Fallback row adjustment for extended structural slates
                    detail = cols[4].get_text(strip=True)

                # Locate parent team header metadata container objects
                team_div = row.find_previous(["span", "h3", "div", "th"], class_=["TeamName", "TeamLogoNameLockup-name", "TableBase-titleText"])
                team_name = team_div.get_text(strip=True) if team_div else "League Pool"

                # Standardize clean spacing values
                clean_name = " ".join(name.split())
                clean_team = " ".join(team_name.split())

                players_list.append({
                    "player_id": clean_name.lower().replace(" ", "-"),
                    "name": clean_name,
                    "sport": sport.lower(),
                    "team": clean_team,
                    "position": " ".join(position.split()),
                    "injury_status": " ".join(status.split()),
                    "injury_detail": " ".join(detail.split()),
                    "last_updated": current_time
                })

        return players_list
    except Exception as e:
        print(f"Error scraping {sport.upper()}: {e}")
        return []

def run_pipeline():
    combined_injuries = []

    # Loops across all 4 target dashboard sports
    for sport in ["nfl", "nba", "nhl", "mlb"]:
        print(f"Scraping live {sport.upper()} injury updates...")
        data = fetch_cbs_injuries(sport)
        combined_injuries.extend(data)
        time.sleep(1)

    # Save to your repository's automated output path folder block
    with open("injuries_slate.json", "w", encoding="utf-8") as f:
        json.dump(combined_injuries, f, indent=2)

    print(f"\nPipeline Finished! Processed {len(combined_injuries)} slots into injuries_slate.json")

if __name__ == "__main__":
    run_pipeline()
