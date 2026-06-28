import logging
from datetime import datetime
from typing import Any

from db.connection import get_session
from db.models import Fixture, Team
from services.football_data import _get


LEAGUES_TO_COLLECT = (
    {"name": "Mundial 2026", "league_id": 1, "season": 2026},
    {"name": "Premier League", "league_id": 39, "season": 2025},
    {"name": "La Liga", "league_id": 140, "season": 2025},
    {"name": "Libertadores", "league_id": 144, "season": 2026},
    {"name": "Torneo Argentino", "league_id": 128, "season": 2026},
)


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


def _upsert_team(session, team: dict[str, Any], league_name: str) -> None:
    team_id = team.get("id")
    if team_id is None:
        return
    record = session.get(Team, int(team_id)) or Team(id=int(team_id))
    record.name = team.get("name") or "Unknown"
    record.league = league_name
    session.merge(record)


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

    record = session.get(Fixture, int(fixture_id)) or Fixture(id=int(fixture_id))
    record.home_team = home.get("name") or "Local"
    record.away_team = away.get("name") or "Visitante"
    record.league = league_name
    record.date = _parse_api_date(fixture.get("date"))
    record.status = (fixture.get("status") or {}).get("short")
    record.result = _score_text(raw)
    session.merge(record)
    return True


def collect_upcoming_fixtures() -> int:
    """Fetch upcoming fixtures for tracked leagues and persist them in PostgreSQL."""
    try:
        session = get_session()
    except RuntimeError:
        logging.info("[fixtures_collector] DATABASE_URL no configurado; collector deshabilitado.")
        return 0

    saved = 0
    with session:
        for league in LEAGUES_TO_COLLECT:
            try:
                data = _get(
                    "/fixtures",
                    params={
                        "league": league["league_id"],
                        "season": league["season"],
                        "next": 20,
                    },
                )
            except Exception:
                logging.exception("[fixtures_collector] error trayendo %s", league["name"])
                continue

            for raw in data.get("response", []) or []:
                if _upsert_fixture(session, raw, league["name"]):
                    saved += 1
        session.commit()

    logging.info("[fixtures_collector] fixtures guardados/actualizados: %s", saved)
    return saved
