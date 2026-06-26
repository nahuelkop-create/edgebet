import requests
from datetime import date
from dotenv import load_dotenv
import os
from services.football_data import BASE_URL, COMPETITIONS, HEADERS

load_dotenv()
KEY = os.getenv("FOOTBALL_DATA_KEY")
print(f"FOOTBALL_DATA_KEY: {KEY}")


def fetch_competition_matches(competition_code: str, params=None):
    url = f"{BASE_URL}/competitions/{competition_code}/matches"
    response = requests.get(url, headers=HEADERS, params=params, timeout=15)
    print(f"\n=== Competition {competition_code} ===")
    print(f"URL: {response.url}")
    print(f"Status: {response.status_code}")
    print("Response raw:")
    print(response.text)
    return response


def main():
    competition_code = "2000"
    params = {"matchday": 2}
    print(f"Consultando Mundial 2026 ({competition_code}) matchday=2")
    try:
        response = fetch_competition_matches(competition_code, params=params)
        try:
            data = response.json()
            matches = data.get("matches", [])
            print(f"\nPartidos encontrados: {len(matches)}")
            for match in matches:
                print(match)
        except Exception as e:
            print("Error parsing JSON:", e)
    except Exception as e:
        print(f"Error en Mundial 2026: {e}")


if __name__ == '__main__':
    main()
