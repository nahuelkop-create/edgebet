import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from db.connection import get_session
from db.models import Fixture, OddsSnapshot, Player, PlayerStat, Prediction, Team, TeamStat
from collectors.fixtures_collector import LEAGUES_TO_COLLECT, _upsert_fixture
from services.football_data import _get


FINISHED_STATUSES = {"FT", "AET", "PEN"}
CORNER_LINE = 9.5


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


def _stat_map(raw: dict[str, Any]) -> dict[str, Any]:
    return {item.get("type"): item.get("value") for item in raw.get("statistics", []) or []}


def _upsert_team(session, team: dict[str, Any]) -> None:
    team_id = team.get("id")
    if team_id is None:
        return
    record = session.get(Team, int(team_id)) or Team(id=int(team_id))
    record.name = team.get("name") or "Unknown"
    session.merge(record)


def _store_team_stats(session, fixture_id: int) -> int:
    data = _get("/fixtures/statistics", params={"fixture": fixture_id})
    saved = 0
    for item in data.get("response", []) or []:
        team = item.get("team", {}) or {}
        team_id = team.get("id")
        if team_id is None:
            continue
        _upsert_team(session, team)
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


def _store_player_stats(session, fixture_id: int) -> int:
    data = _get("/fixtures/players", params={"fixture": fixture_id})
    saved = 0
    for team_block in data.get("response", []) or []:
        team = team_block.get("team", {}) or {}
        team_id = team.get("id")
        if team_id is not None:
            _upsert_team(session, team)

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

            player_record = session.get(Player, int(player_id)) or Player(id=int(player_id))
            player_record.name = player.get("name") or "Unknown"
            player_record.team_id = int(team_id) if team_id is not None else None
            player_record.position = games.get("position")
            session.merge(player_record)

            session.add(
                PlayerStat(
                    player_id=int(player_id),
                    fixture_id=fixture_id,
                    shots=_to_int(shots.get("total")),
                    shots_on_target=_to_int(shots.get("on")),
                    fouls_committed=_to_int(fouls.get("committed")),
                    fouls_drawn=_to_int(fouls.get("drawn")),
                    saves=_to_int(goals.get("saves")),
                    assists=_to_int(goals.get("assists")),
                    goals=_to_int(goals.get("total")),
                    rating=_to_float(games.get("rating")),
                )
            )
            saved += 1
    return saved


def _closing_corner_odds(session, fixture_id: int, is_over: bool) -> float | None:
    snapshot = session.scalar(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == fixture_id,
            OddsSnapshot.market.in_(("corners_9_5", "corners", "corners_total_9_5")),
        )
        .order_by(OddsSnapshot.timestamp.desc())
        .limit(1)
    )
    if snapshot is None:
        return None
    return snapshot.over_odds if is_over else snapshot.under_odds


def _settle_prediction(session, prediction: Prediction, total_corners: int) -> bool:
    market = (prediction.market or "").lower()
    if "corners_over_9_5" in market or market in {"corners_9_5", "corners", "corners_total_9_5"}:
        correct = total_corners > CORNER_LINE
        is_over = True
    elif "corners_under_9_5" in market:
        correct = total_corners < CORNER_LINE
        is_over = False
    else:
        return False

    odds = prediction.real_odds
    closing_odds = _closing_corner_odds(session, int(prediction.fixture_id), is_over) if prediction.fixture_id else None
    prediction.correct = bool(correct)
    prediction.profit = round(float(odds) - 1.0, 4) if correct and odds else (-1.0 if not correct else None)
    prediction.closing_odds = closing_odds
    prediction.closing_implied_probability = round(1 / closing_odds, 4) if closing_odds and closing_odds > 1 else None
    prediction.clv = round((odds / closing_odds) - 1, 4) if closing_odds and odds else None
    prediction.settled_at = datetime.now(timezone.utc)
    return True


def _settle_predictions(session, fixture_id: int) -> int:
    stats = list(session.scalars(select(TeamStat).where(TeamStat.fixture_id == fixture_id)).all())
    corners = [stat.corners for stat in stats if stat.corners is not None]
    if len(corners) < 2:
        return 0

    total_corners = sum(int(value) for value in corners)
    predictions = session.scalars(
        select(Prediction).where(Prediction.fixture_id == fixture_id)
    ).all()
    settled = 0
    for prediction in predictions:
        if _settle_prediction(session, prediction, total_corners):
            settled += 1
    return settled


def collect_match_stats(fixture_id: int) -> dict[str, int]:
    """Persist team and player stats for one finished fixture."""
    try:
        session = get_session()
    except RuntimeError:
        logging.info("[stats_collector] DATABASE_URL no configurado; collector deshabilitado.")
        return {"team_stats": 0, "player_stats": 0}

    with session:
        session.execute(delete(TeamStat).where(TeamStat.fixture_id == fixture_id))
        session.execute(delete(PlayerStat).where(PlayerStat.fixture_id == fixture_id))
        team_count = _store_team_stats(session, fixture_id)
        player_count = _store_player_stats(session, fixture_id)
        settled_count = _settle_predictions(session, fixture_id)
        session.commit()

    logging.info(
        "[stats_collector] fixture %s: team_stats=%s player_stats=%s predictions_settled=%s",
        fixture_id,
        team_count,
        player_count,
        settled_count,
    )
    return {"team_stats": team_count, "player_stats": player_count, "predictions_settled": settled_count}


def collect_finished_match_stats() -> int:
    """Scan recently finished PostgreSQL fixtures and collect missing/updated stats."""
    try:
        session = get_session()
    except RuntimeError:
        logging.info("[stats_collector] DATABASE_URL no configurado; collector deshabilitado.")
        return 0

    fixture_ids: set[int] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    with session:
        for league in LEAGUES_TO_COLLECT:
            try:
                data = _get(
                    "/fixtures",
                    params={
                        "league": league["league_id"],
                        "season": league["season"],
                        "last": 20,
                    },
                )
            except Exception:
                logging.exception("[stats_collector] error revisando terminados de %s", league["name"])
                continue

            for raw in data.get("response", []) or []:
                status = ((raw.get("fixture") or {}).get("status") or {}).get("short")
                if status not in FINISHED_STATUSES:
                    continue
                if _upsert_fixture(session, raw, league["name"]):
                    fixture_id = (raw.get("fixture") or {}).get("id")
                    if fixture_id is not None:
                        fixture_ids.add(int(fixture_id))
        session.commit()

        db_fixture_ids = session.scalars(
            select(Fixture.id).where(
                Fixture.status.in_(FINISHED_STATUSES),
                Fixture.date >= cutoff,
            )
        ).all()
        fixture_ids.update(int(fid) for fid in db_fixture_ids)

    processed = 0
    for fixture_id in fixture_ids:
        try:
            collect_match_stats(int(fixture_id))
            processed += 1
        except Exception:
            logging.exception("[stats_collector] error procesando fixture %s", fixture_id)

    return processed
