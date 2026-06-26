from dotenv import load_dotenv
import os
import json
from datetime import date

load_dotenv()

from services.sportradar import get_fixtures_by_league


def pretty_print_fixtures(fixtures, limit=5):
    for i, f in enumerate(fixtures[:limit], start=1):
        print(f"--- Fixture {i} ---")
        try:
            print(json.dumps(f, indent=2, ensure_ascii=False))
        except Exception:
            print(str(f))


def main():
    today = date.today()
    leagues = ["Premier League", "La Liga", "Copa Libertadores"]

    for league in leagues:
        try:
            fixtures = get_fixtures_by_league(league, target_date=today)
        except Exception as e:
            print(f"Error consultando {league}: {e}")
            fixtures = []

        if fixtures:
            print(f"Fixtures encontrados para {league} ({len(fixtures)}) el {today}:")
            pretty_print_fixtures(fixtures)
            return

    print("No se encontraron fixtures para hoy en las ligas probadas.")


if __name__ == "__main__":
    main()
