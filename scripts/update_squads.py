import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from db.connection import get_session  # noqa: E402
from db.migrations import initialize_postgres  # noqa: E402
from db.models import Player, Team  # noqa: E402


load_dotenv(ROOT_DIR / ".env")

BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").replace(" ", "").strip()
HEADERS = {"x-apisports-key": API_KEY} if API_KEY else {}
REQUEST_SLEEP_SECONDS = 1
MAX_RETRIES = 3

TEAMS = (
    {"id": 451, "name": "Boca Juniors", "season": 2026},
    {"id": 435, "name": "River Plate", "season": 2026},
    {"id": 453, "name": "Independiente", "season": 2026},
    {"id": 460, "name": "San Lorenzo", "season": 2026},
    {"id": 436, "name": "Racing Club", "season": 2026},
    {"id": 450, "name": "Estudiantes L.P.", "season": 2026},
    {"id": 26, "name": "Argentina", "season": 2026},
    {"id": 541, "name": "Real Madrid", "season": 2025},
    {"id": 529, "name": "Barcelona", "season": 2025},
    {"id": 530, "name": "Atletico Madrid", "season": 2025},
    {"id": 50, "name": "Manchester City", "season": 2025},
    {"id": 40, "name": "Liverpool", "season": 2025},
    {"id": 157, "name": "Bayern Munich", "season": 2025},
    {"id": 85, "name": "PSG", "season": 2025},
    {"id": 496, "name": "Juventus", "season": 2025},
)


def _request(path: str, params: dict[str, Any]) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY no está definido")

    for attempt in range(1, MAX_RETRIES + 1):
        time.sleep(REQUEST_SLEEP_SECONDS)
        response = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=30)
        if response.status_code == 429 and attempt < MAX_RETRIES:
            wait_seconds = 30 * attempt
            print(f"  Rate limit 429. Esperando {wait_seconds}s antes de reintentar...")
            time.sleep(wait_seconds)
            continue
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise RuntimeError(f"API-Football error: {errors}")
        return payload

    raise RuntimeError(f"No se pudo completar request {path} params={params}")


def _to_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(str(value).replace(",", ".").strip()))
    except (TypeError, ValueError):
        return None


def _upsert_team(session, team_id: int, name: str, league: str | None) -> None:
    stmt = pg_insert(Team).values(id=team_id, name=name, league=league)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "league": func.coalesce(stmt.excluded.league, Team.league),
        },
    )
    session.execute(stmt)


def _upsert_player(session, player_payload: dict[str, Any], team_id: int, position: str | None) -> bool:
    player_id = player_payload.get("id")
    if player_id is None:
        return False

    birth = player_payload.get("birth", {}) or {}
    values = {
        "id": int(player_id),
        "name": player_payload.get("name") or "Unknown",
        "team_id": team_id,
        "position": position,
        "nationality": player_payload.get("nationality"),
        "age": _to_int(player_payload.get("age")),
        "height": player_payload.get("height"),
        "weight": player_payload.get("weight"),
        "birth_date": birth.get("date"),
        "birth_place": birth.get("place"),
    }
    stmt = pg_insert(Player).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "team_id": stmt.excluded.team_id,
            "position": func.coalesce(stmt.excluded.position, Player.position),
            "nationality": func.coalesce(stmt.excluded.nationality, Player.nationality),
            "age": func.coalesce(stmt.excluded.age, Player.age),
            "height": func.coalesce(stmt.excluded.height, Player.height),
            "weight": func.coalesce(stmt.excluded.weight, Player.weight),
            "birth_date": func.coalesce(stmt.excluded.birth_date, Player.birth_date),
            "birth_place": func.coalesce(stmt.excluded.birth_place, Player.birth_place),
        },
    )
    session.execute(stmt)
    return True


def update_team_squad(session, team_id: int, fallback_name: str, season: int) -> int:
    saved = 0
    page = 1
    team_name = fallback_name
    league_name = None

    while True:
        payload = _request("/players", {"team": team_id, "season": season, "page": page})
        rows = payload.get("response", []) or []
        paging = payload.get("paging", {}) or {}
        current = int(paging.get("current") or page)
        total_pages = int(paging.get("total") or current)

        print(f"  Página {current}/{total_pages}: {len(rows)} jugadores")
        for item in rows:
            player = item.get("player", {}) or {}
            statistics = item.get("statistics", []) or []
            stat = statistics[0] if statistics else {}
            team = stat.get("team", {}) or {}
            league = stat.get("league", {}) or {}
            games = stat.get("games", {}) or {}

            team_name = team.get("name") or team_name
            league_name = league.get("name") or league_name
            _upsert_team(session, team_id, team_name, league_name)

            if _upsert_player(session, player, team_id, games.get("position")):
                saved += 1

        session.commit()
        if current >= total_pages:
            break
        page += 1

    return saved


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not initialize_postgres():
        print("ERROR: DATABASE_URL/POSTGRES_URL no está configurado; no se puede actualizar PostgreSQL.")
        return 1

    total_saved = 0
    failures = 0
    with get_session() as session:
        for idx, team in enumerate(TEAMS, 1):
            print(f"[{idx}/{len(TEAMS)}] Actualizando {team['name']} id={team['id']} season={team['season']}")
            try:
                count = update_team_squad(session, team["id"], team["name"], team["season"])
                total_saved += count
                print(f"  Guardados/actualizados: {count}")
            except Exception:
                failures += 1
                session.rollback()
                logging.exception("No se pudo actualizar plantel team_id=%s", team["id"])

    print("")
    print("Resumen update_squads")
    print("Temporadas: " + ", ".join(f"{team['name']}={team['season']}" for team in TEAMS))
    print(f"Equipos procesados: {len(TEAMS) - failures}/{len(TEAMS)}")
    print(f"Jugadores guardados/actualizados: {total_saved}")
    print(f"Errores: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
