import os
import requests
from datetime import date
from typing import List, Dict, Any

BASE_URL = os.getenv("API_FOOTBALL_BASE", "https://v3.football.api-sports.io")
API_KEY = os.getenv("API_FOOTBALL_KEY")

HEADERS = {"x-apisports-key": API_KEY} if API_KEY else {}

LEAGUE_IDS = {
    "Premier League": 39,
    "La Liga": 140,
    "Copa Libertadores": 13,
    "Torneo Argentino / Copa Argentina": 128,
    "Mundial 2026": 1,
}


def _get(path: str, params: dict = None) -> Dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY no está definido")
    url = f"{BASE_URL}{path}"
    resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_fixtures_today() -> List[Dict[str, Any]]:
    """Obtener partidos de hoy para las ligas definidas en LEAGUE_IDS.

    Llama a la API por cada liga y combina los fixtures.
    """
    today = date.today().isoformat()
    fixtures = []
    for league_name, lid in LEAGUE_IDS.items():
        try:
            data = _get("/fixtures", params={"date": today, "league": lid, "season": 2024})
            resp = data.get("response") or data.get("results") or data
            if isinstance(resp, dict) and "response" in data:
                items = data["response"]
            elif isinstance(resp, list):
                items = resp
            else:
                items = []
            for it in items:
                it.setdefault("league_name", league_name)
            fixtures.extend(items)
        except Exception:
            continue
    return fixtures


def get_player_stats(fixture_id: int) -> Dict[str, Any]:
    """Obtener estadísticas de jugadores para un partido.

    Usa el endpoint `/players` filtrando por `fixture`.
    """
    try:
        data = _get("/players", params={"fixture": fixture_id})
        return data.get("response") or data
    except Exception:
        return {}


def get_h2h(team1_id: int, team2_id: int) -> Dict[str, Any]:
    """Obtener historial entre dos equipos (H2H).

    Usa el endpoint `/fixtures/headtohead` o `/fixtures/h2h` si está disponible.
    """
    # Intentar ruta estándar
    try:
        data = _get("/fixtures/headtohead", params={"h2h": f"{team1_id}-{team2_id}"})
        return data.get("response") or data
    except Exception:
        pass

    try:
        data = _get("/fixtures", params={"team": team1_id, "opponent": team2_id})
        return data.get("response") or data
    except Exception:
        return {}
