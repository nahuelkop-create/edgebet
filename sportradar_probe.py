from dotenv import load_dotenv
import os
import requests
import json
from pathlib import Path

load_dotenv()

API_KEY = os.getenv("SPORTRADAR_API_KEY")
if not API_KEY:
    raise RuntimeError("SPORTRADAR_API_KEY no encontrada en el entorno")

URL = f"https://api.sportradar.com/soccer/trial/v4/en/competitions.json?api_key={API_KEY}"


def main():
    print(f"Haciendo GET a: {URL}")
    resp = requests.get(URL, timeout=15)
    print("Status:", resp.status_code)
    text = resp.text or ""
    print("Response (primeros 500 chars):")
    print(text[:500])

    # Intentar parsear JSON
    try:
        data = resp.json()
    except Exception as e:
        print("No se pudo parsear JSON:", e)
        return

    # Buscar competiciones de interés
    targets = [
        "Premier League",
        "La Liga",
        "Copa Libertadores",
        "Torneo Argentino",
        "Mundial 2026",
    ]

    found = {}
    comps = data.get("competitions") or data.get("data") or []
    for c in comps:
        name = c.get("name") or c.get("competition_name") or c.get("title")
        cid = c.get("id") or c.get("competition_id") or c.get("code")
        if not name or not cid:
            continue
        for t in targets:
            if t.lower() in name.lower():
                found[t] = cid

    if not found:
        print("No se encontraron competiciones objetivo en la respuesta.")
        return

    print("Competiciones encontradas:")
    for k, v in found.items():
        print(f"- {k}: {v}")

    # Escribir config/leagues.py
    cfg_dir = Path("config")
    cfg_dir.mkdir(exist_ok=True)
    cfg_file = cfg_dir / "leagues.py"
    with cfg_file.open("w", encoding="utf-8") as f:
        f.write("# Auto-generado por sportradar_probe.py\n")
        f.write("LEAGUE_IDS = {\n")
        for k, v in found.items():
            f.write(f"    '{k}': '{v}',\n")
        f.write("}\n")

    print(f"Archivo creado: {cfg_file}")


if __name__ == '__main__':
    main()
