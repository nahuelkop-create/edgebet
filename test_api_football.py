from dotenv import load_dotenv
import os
import json
from services.api_football import get_fixtures_today

load_dotenv()


def main():
    try:
        fixtures = get_fixtures_today()
    except Exception as e:
        print("Error al obtener fixtures:", e)
        return

    if not fixtures:
        print("No hay fixtures para hoy o la API devolvió vacío.")
        return

    print(f"Se encontraron {len(fixtures)} fixtures hoy:")
    for i, f in enumerate(fixtures[:20], start=1):
        try:
            print(json.dumps(f, indent=2, ensure_ascii=False))
        except Exception:
            print(f)


if __name__ == '__main__':
    main()
