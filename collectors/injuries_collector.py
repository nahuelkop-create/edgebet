import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.connection import get_session
from db.models import Fixture, InjuryReport, Player, Team
from services.football_data import _get


UPCOMING_STATUSES = {"TBD", "NS", "PST", "SCHEDULED", "TIMED"}


def _upsert_team(session, team: dict[str, Any]) -> None:
    team_id = team.get("id")
    if team_id is None:
        return
    stmt = pg_insert(Team).values(
        id=int(team_id),
        name=team.get("name") or "Unknown",
    ).on_conflict_do_update(
        index_elements=["id"],
        set_={"name": team.get("name") or "Unknown"},
    )
    session.execute(stmt)


def _upsert_player(session, player: dict[str, Any], team_id: int | None) -> None:
    player_id = player.get("id")
    if player_id is None:
        return
    stmt = pg_insert(Player).values(
        id=int(player_id),
        name=player.get("name") or "Unknown",
        team_id=team_id,
    ).on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": player.get("name") or "Unknown",
            "team_id": team_id,
        },
    )
    session.execute(stmt)


def _store_injury(session, fixture_id: int, raw: dict[str, Any]) -> bool:
    player = raw.get("player", {}) or {}
    team = raw.get("team", {}) or {}
    player_id = player.get("id")
    team_id = team.get("id")
    reason = player.get("reason") or raw.get("reason")
    if player_id is None or reason is None:
        return False

    team_id_int = int(team_id) if team_id is not None else None
    _upsert_team(session, team)
    _upsert_player(session, player, team_id_int)

    values = {
        "fixture_id": fixture_id,
        "player_id": int(player_id),
        "team_id": team_id_int,
        "player_name": player.get("name"),
        "team_name": team.get("name"),
        "reason": str(reason),
        "status": player.get("type") or raw.get("type"),
        "reported_at": datetime.now(timezone.utc),
    }
    stmt = pg_insert(InjuryReport).values(**values)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=["fixture_id", "player_id", "reason"],
            set_={
                "team_id": values["team_id"],
                "player_name": values["player_name"],
                "team_name": values["team_name"],
                "status": values["status"],
                "reported_at": values["reported_at"],
            },
        )
    )
    return True


def _upcoming_fixture_ids(session, days_ahead: int = 14) -> list[int]:
    now = datetime.now(timezone.utc)
    until = now + timedelta(days=days_ahead)
    return [
        int(fixture_id)
        for fixture_id in session.scalars(
            select(Fixture.id).where(
                Fixture.status.in_(UPCOMING_STATUSES),
                Fixture.date >= now - timedelta(hours=6),
                Fixture.date <= until,
            )
        ).all()
    ]


def collect_injuries() -> int:
    """Collect injury reports for upcoming fixtures when API-Football provides them."""
    try:
        session = get_session()
    except RuntimeError:
        logging.info("[injuries_collector] DATABASE_URL no configurado; collector deshabilitado.")
        return 0

    saved = 0
    with session:
        fixture_ids = _upcoming_fixture_ids(session)
        for fixture_id in fixture_ids:
            try:
                data = _get("/injuries", params={"fixture": fixture_id})
            except Exception:
                logging.exception("[injuries_collector] error trayendo lesiones fixture %s", fixture_id)
                continue

            for raw in data.get("response", []) or []:
                if _store_injury(session, fixture_id, raw):
                    saved += 1
        session.commit()

    logging.info("[injuries_collector] reportes guardados/actualizados: %s", saved)
    return saved
