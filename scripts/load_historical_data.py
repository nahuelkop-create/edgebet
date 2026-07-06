import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert as pg_insert

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from db.connection import get_session  # noqa: E402
from db.migrations import initialize_postgres  # noqa: E402
from db.models import Fixture, Player, PlayerStat, Team, TeamStat  # noqa: E402


load_dotenv(ROOT_DIR / ".env")

BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").replace(" ", "").strip()
HEADERS = {"x-apisports-key": API_KEY} if API_KEY else {}
REQUEST_SLEEP_SECONDS = 2
RATE_LIMIT_RETRY_SECONDS = 60
MAX_RATE_LIMIT_RETRIES = 3
REQUEST_BATCH_LIMIT = 500
REQUEST_BATCH_PAUSE_SECONDS = 600
FINISHED_STATUSES = {"FT", "AET", "PEN"}

LEAGUES = (
    {"name": "Mundial 2026", "league_id": 1, "season": 2026},
    {"name": "Premier League", "league_id": 39, "season": 2024},
    {"name": "La Liga", "league_id": 140, "season": 2024},
    {"name": "Copa Libertadores", "league_id": 13, "season": 2025},
    {"name": "Torneo Argentino", "league_id": 128, "season": 2024},
)

REQUESTS_USED = 0
REQUESTS_SINCE_PAUSE = 0


def _request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    global REQUESTS_USED, REQUESTS_SINCE_PAUSE
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY no esta definido")

    for attempt in range(1, MAX_RATE_LIMIT_RETRIES + 2):
        if REQUESTS_SINCE_PAUSE >= REQUEST_BATCH_LIMIT:
            print(f"Requests usados desde la ultima pausa: {REQUESTS_SINCE_PAUSE}. Pausando 10 minutos...")
            time.sleep(REQUEST_BATCH_PAUSE_SECONDS)
            REQUESTS_SINCE_PAUSE = 0

        time.sleep(REQUEST_SLEEP_SECONDS)
        REQUESTS_USED += 1
        REQUESTS_SINCE_PAUSE += 1
        response = requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params or {}, timeout=30)
        if response.status_code == 429 and attempt <= MAX_RATE_LIMIT_RETRIES:
            print(
                f"Rate limit 429 en {path}. Esperando {RATE_LIMIT_RETRY_SECONDS}s "
                f"antes de reintentar ({attempt}/{MAX_RATE_LIMIT_RETRIES})..."
            )
            time.sleep(RATE_LIMIT_RETRY_SECONDS)
            continue
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise RuntimeError(f"API-Football error {path}: {errors}")
        return payload

    raise RuntimeError(f"API-Football rate limit persistente en {path}")


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace("%", "").replace(",", ".").strip()))
    except (TypeError, ValueError):
        return None


def _to_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _parse_api_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _score_text(raw: dict[str, Any]) -> str | None:
    goals = raw.get("goals", {}) or {}
    home = goals.get("home")
    away = goals.get("away")
    if home is None or away is None:
        return None
    return f"{home}-{away}"


def _stat_map(raw: dict[str, Any]) -> dict[str, Any]:
    return {item.get("type"): item.get("value") for item in raw.get("statistics", []) or []}


def _upsert_team(session, team: dict[str, Any], league_name: str) -> bool:
    team_id = team.get("id")
    if team_id is None:
        return False
    stmt = pg_insert(Team).values(
        id=int(team_id),
        name=team.get("name") or "Unknown",
        league=league_name,
    ).on_conflict_do_update(
        index_elements=["id"],
        set_={"name": team.get("name") or "Unknown", "league": league_name},
    )
    session.execute(stmt)
    return True


def _upsert_fixture(session, raw: dict[str, Any], league_name: str) -> bool:
    fixture = raw.get("fixture", {}) or {}
    fixture_id = fixture.get("id")
    if fixture_id is None:
        return False

    teams = raw.get("teams", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    _upsert_team(session, home, league_name)
    _upsert_team(session, away, league_name)

    values = {
        "id": int(fixture_id),
        "home_team": home.get("name") or "Local",
        "away_team": away.get("name") or "Visitante",
        "league": league_name,
        "date": _parse_api_date(fixture.get("date")),
        "status": (fixture.get("status") or {}).get("short"),
        "result": _score_text(raw),
    }
    stmt = pg_insert(Fixture).values(**values).on_conflict_do_update(
        index_elements=["id"],
        set_={key: value for key, value in values.items() if key != "id"},
    )
    session.execute(stmt)
    return True


def _upsert_player(session, player: dict[str, Any], team_id: int | None, position: str | None = None) -> bool:
    player_id = player.get("id")
    if player_id is None:
        return False
    birth = player.get("birth", {}) or {}
    values = {
        "id": int(player_id),
        "name": player.get("name") or "Unknown",
        "team_id": team_id,
        "position": position or player.get("position"),
        "nationality": player.get("nationality"),
        "age": _to_int(player.get("age")),
        "height": player.get("height"),
        "weight": player.get("weight"),
        "birth_date": birth.get("date"),
        "birth_place": birth.get("place"),
    }
    stmt = pg_insert(Player).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": values["name"],
            "team_id": values["team_id"],
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


def _store_team_stats(session, fixture_id: int) -> int:
    data = _request("/fixtures/statistics", {"fixture": fixture_id})
    saved = 0
    session.execute(delete(TeamStat).where(TeamStat.fixture_id == fixture_id))
    for item in data.get("response", []) or []:
        team = item.get("team", {}) or {}
        team_id = team.get("id")
        if team_id is None:
            continue
        flat = _stat_map(item)
        session.add(
            TeamStat(
                fixture_id=fixture_id,
                team_id=int(team_id),
                corners=_to_int(flat.get("Corner Kicks")),
                possession=_to_float(flat.get("Ball Possession")),
                shots=_to_int(flat.get("Total Shots")),
                fouls=_to_int(flat.get("Fouls")),
            )
        )
        saved += 1
    return saved


def _store_player_stats(session, fixture_id: int) -> tuple[int, int]:
    data = _request("/fixtures/players", {"fixture": fixture_id})
    stats_saved = 0
    players_saved = 0
    session.execute(delete(PlayerStat).where(PlayerStat.fixture_id == fixture_id))
    for team_block in data.get("response", []) or []:
        team = team_block.get("team", {}) or {}
        team_id = team.get("id")
        for item in team_block.get("players", []) or []:
            player = item.get("player", {}) or {}
            player_id = player.get("id")
            if player_id is None:
                continue
            stat = (item.get("statistics") or [{}])[0] or {}
            games = stat.get("games", {}) or {}
            goals = stat.get("goals", {}) or {}
            shots = stat.get("shots", {}) or {}
            fouls = stat.get("fouls", {}) or {}
            cards = stat.get("cards", {}) or {}
            if _upsert_player(session, player, int(team_id) if team_id is not None else None, games.get("position")):
                players_saved += 1
            session.add(
                PlayerStat(
                    player_id=int(player_id),
                    fixture_id=fixture_id,
                    appearances=_to_int(games.get("appearences") or games.get("appearances")),
                    minutes=_to_int(games.get("minutes")),
                    shots=_to_int(shots.get("total")),
                    shots_on_target=_to_int(shots.get("on")),
                    fouls_committed=_to_int(fouls.get("committed")),
                    fouls_drawn=_to_int(fouls.get("drawn")),
                    saves=_to_int(goals.get("saves")),
                    assists=_to_int(goals.get("assists")),
                    goals=_to_int(goals.get("total")),
                    yellow_cards=_to_int(cards.get("yellow")),
                    red_cards=_to_int(cards.get("red")),
                    rating=_to_float(games.get("rating")),
                )
            )
            stats_saved += 1
    return stats_saved, players_saved


def _load_team_players(session, team_id: int, season: int) -> int:
    saved = 0
    page = 1
    while True:
        data = _request("/players", {"team": team_id, "season": season, "page": page})
        for item in data.get("response", []) or []:
            player = item.get("player", {}) or {}
            stats = item.get("statistics", []) or []
            position = ((stats[0] if stats else {}).get("games") or {}).get("position")
            if _upsert_player(session, player, team_id, position):
                saved += 1
        paging = data.get("paging", {}) or {}
        current = int(paging.get("current") or page)
        total = int(paging.get("total") or current)
        if current >= total:
            break
        page += 1
    return saved


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not initialize_postgres():
        print("ERROR: DATABASE_URL no esta configurado; no se puede cargar PostgreSQL.")
        return 1

    fixtures_loaded = 0
    team_stats_loaded = 0
    player_stats_loaded = 0
    players_loaded = 0
    teams_by_season: dict[tuple[int, int], None] = {}

    session = get_session()
    with session:
        for league in LEAGUES:
            print(f"Procesando {league['name']} league={league['league_id']} season={league['season']}")
            data = _request(
                "/fixtures",
                {"league": league["league_id"], "season": league["season"], "last": 50},
            )
            fixtures = data.get("response", []) or []
            print(f"  Fixtures recibidos: {len(fixtures)}")
            for fixture_idx, raw in enumerate(fixtures, 1):
                print(f"  Procesando fixture {fixture_idx}/{len(fixtures)}...")
                if _upsert_fixture(session, raw, league["name"]):
                    fixtures_loaded += 1
                teams = raw.get("teams", {}) or {}
                for side in ("home", "away"):
                    team_id = (teams.get(side, {}) or {}).get("id")
                    if team_id is not None:
                        teams_by_season[(int(team_id), int(league["season"]))] = None

                fixture_id = ((raw.get("fixture") or {}).get("id"))
                status = (((raw.get("fixture") or {}).get("status") or {}).get("short"))
                if fixture_id is not None and status in FINISHED_STATUSES:
                    try:
                        team_stats_loaded += _store_team_stats(session, int(fixture_id))
                        pstats, pplayers = _store_player_stats(session, int(fixture_id))
                        player_stats_loaded += pstats
                        players_loaded += pplayers
                        session.commit()
                    except Exception:
                        session.rollback()
                        logging.exception("No se pudieron cargar stats fixture=%s", fixture_id)
            session.commit()

        print(f"Equipos unicos para cargar jugadores: {len(teams_by_season)}")
        for idx, (team_id, season) in enumerate(teams_by_season.keys(), 1):
            try:
                count = _load_team_players(session, team_id, season)
                players_loaded += count
                session.commit()
                print(f"  [{idx}/{len(teams_by_season)}] team={team_id} season={season}: jugadores={count}")
            except Exception:
                session.rollback()
                logging.exception("No se pudieron cargar jugadores team=%s season=%s", team_id, season)

    print("")
    print("Resumen carga historica")
    print(f"Fixtures cargados: {fixtures_loaded}")
    print(f"Stats cargados: team_stats={team_stats_loaded} player_stats={player_stats_loaded}")
    print(f"Jugadores cargados: {players_loaded}")
    print(f"Requests usados: {REQUESTS_USED}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
