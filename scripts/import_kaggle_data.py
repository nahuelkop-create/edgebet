import argparse
import csv
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from db.connection import get_session  # noqa: E402
from db.migrations import initialize_postgres  # noqa: E402
from db.models import Fixture, GameEvent, Player, PlayerAttribute, PlayerStat, Team  # noqa: E402


load_dotenv(ROOT_DIR / ".env")

TRANSFERMARKT_PLAYER_OFFSET = 100_000_000
TRANSFERMARKT_TEAM_OFFSET = 110_000_000
TRANSFERMARKT_FIXTURE_OFFSET = 120_000_000
SOCCER_PLAYER_OFFSET = 200_000_000
SOCCER_TEAM_OFFSET = 210_000_000
SOCCER_FIXTURE_OFFSET = 220_000_000
BATCH_SIZE = 1000


def _to_int(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(float(str(value).replace(",", ".").strip()))
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _text(value: Any, max_len: int | None = None) -> str | None:
    if value in (None, "", "None"):
        return None
    result = str(value).strip()
    if max_len and len(result) > max_len:
        return result[:max_len]
    return result


def _date(value: Any) -> datetime | None:
    if value in (None, "", "None"):
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw[:19] if fmt.endswith("%S") else raw[:10], fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _namespaced_id(value: Any, offset: int) -> int | None:
    raw = _to_int(value)
    if raw is None:
        return None
    return -offset - raw


def _height_cm(value: Any) -> str | None:
    height = _to_float(value)
    if height is None:
        return _text(value, 50)
    return f"{height:.0f} cm"


def _weight_kg(value: Any) -> str | None:
    weight = _to_float(value)
    if weight is None:
        return _text(value, 50)
    return f"{weight:.0f} kg"


def _age_from_birth(value: Any) -> int | None:
    birth = _date(value)
    if not birth:
        return None
    today = datetime.utcnow().date()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _csv_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _progress(label: str, count: int) -> None:
    if count and count % BATCH_SIZE == 0:
        print(f"  {label}: {count} registros procesados...")


def _commit_batch(session, count: int) -> None:
    if count and count % BATCH_SIZE == 0:
        session.commit()


def _run_rows(label: str, rows, handler: Callable[[dict[str, Any]], bool], session) -> tuple[int, int]:
    imported = 0
    errors = 0
    for idx, row in enumerate(rows, 1):
        try:
            with session.begin_nested():
                saved = handler(row)
            if saved:
                imported += 1
                _commit_batch(session, imported)
                _progress(label, imported)
        except Exception:
            errors += 1
            logging.exception("%s: error en fila %s", label, idx)
    session.commit()
    print(f"  {label}: importados={imported} errores={errors}")
    return imported, errors


def _upsert_team(session, values: dict[str, Any]) -> bool:
    if values.get("id") is None or not values.get("name"):
        return False
    stmt = pg_insert(Team).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "league": func.coalesce(stmt.excluded.league, Team.league),
        },
    )
    session.execute(stmt)
    return True


def _upsert_player(session, values: dict[str, Any]) -> bool:
    if values.get("id") is None or not values.get("name"):
        return False
    stmt = pg_insert(Player).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "name": stmt.excluded.name,
            "team_id": func.coalesce(stmt.excluded.team_id, Player.team_id),
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


def _upsert_fixture(session, values: dict[str, Any]) -> bool:
    if values.get("id") is None:
        return False
    values["home_team"] = values.get("home_team") or "Local"
    values["away_team"] = values.get("away_team") or "Visitante"
    stmt = pg_insert(Fixture).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={key: getattr(stmt.excluded, key) for key in values if key != "id"},
    )
    session.execute(stmt)
    return True


def _upsert_player_stat(session, values: dict[str, Any]) -> bool:
    if values.get("fixture_id") is None or values.get("player_id") is None:
        return False
    stmt = pg_insert(PlayerStat).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["fixture_id", "player_id"],
        set_={key: getattr(stmt.excluded, key) for key in values if key not in {"id", "fixture_id", "player_id"}},
    )
    session.execute(stmt)
    return True


def _upsert_game_event(session, values: dict[str, Any]) -> bool:
    if not values.get("source_event_id"):
        return False
    stmt = pg_insert(GameEvent).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_event_id"],
        set_={key: getattr(stmt.excluded, key) for key in values if key not in {"id", "source", "source_event_id"}},
    )
    session.execute(stmt)
    return True


def _upsert_player_attribute(session, values: dict[str, Any]) -> bool:
    if not values.get("source_attribute_id"):
        return False
    stmt = pg_insert(PlayerAttribute).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["source", "source_attribute_id"],
        set_={key: getattr(stmt.excluded, key) for key in values if key not in {"id", "source", "source_attribute_id"}},
    )
    session.execute(stmt)
    return True


def import_transfermarkt(root: Path, session) -> dict[str, int]:
    print(f"Importando Transfermarkt desde {root}")
    totals = {"teams": 0, "players": 0, "fixtures": 0, "player_stats": 0, "game_events": 0, "errors": 0}

    clubs_path = root / "clubs.csv"
    if clubs_path.exists():
        imported, errors = _run_rows(
            "clubs.csv -> teams",
            _csv_rows(clubs_path),
            lambda row: _upsert_team(
                session,
                {
                    "id": _namespaced_id(row.get("club_id"), TRANSFERMARKT_TEAM_OFFSET),
                    "name": _text(row.get("name"), 255) or _text(row.get("club_name"), 255),
                    "league": _text(row.get("domestic_competition_id"), 255),
                },
            ),
            session,
        )
        totals["teams"] += imported
        totals["errors"] += errors
    else:
        print(f"  clubs.csv no encontrado en {root}")

    players_path = root / "players.csv"
    if players_path.exists():
        imported, errors = _run_rows(
            "players.csv -> players",
            _csv_rows(players_path),
            lambda row: _upsert_player(
                session,
                {
                    "id": _namespaced_id(row.get("player_id"), TRANSFERMARKT_PLAYER_OFFSET),
                    "name": _text(row.get("name"), 255)
                    or " ".join(part for part in [_text(row.get("first_name")), _text(row.get("last_name"))] if part)
                    or "Unknown",
                    "team_id": _namespaced_id(row.get("current_club_id"), TRANSFERMARKT_TEAM_OFFSET),
                    "position": _text(row.get("sub_position") or row.get("position"), 50),
                    "nationality": _text(row.get("country_of_citizenship") or row.get("nationality"), 100),
                    "age": _age_from_birth(row.get("date_of_birth")) or _to_int(row.get("age")),
                    "height": _height_cm(row.get("height_in_cm") or row.get("height")),
                    "weight": _weight_kg(row.get("weight")),
                    "birth_date": _text(row.get("date_of_birth"), 50),
                    "birth_place": _text(row.get("city_of_birth") or row.get("country_of_birth"), 255),
                },
            ),
            session,
        )
        totals["players"] += imported
        totals["errors"] += errors
    else:
        print(f"  players.csv no encontrado en {root}")

    games_path = root / "games.csv"
    if games_path.exists():
        imported, errors = _run_rows(
            "games.csv -> fixtures",
            _csv_rows(games_path),
            lambda row: _upsert_fixture(
                session,
                {
                    "id": _namespaced_id(row.get("game_id"), TRANSFERMARKT_FIXTURE_OFFSET),
                    "home_team": _text(row.get("home_club_name"), 255),
                    "away_team": _text(row.get("away_club_name"), 255),
                    "league": _text(row.get("competition_id") or row.get("competition_type"), 255),
                    "date": _date(row.get("date")),
                    "status": "FT",
                    "result": (
                        f"{_to_int(row.get('home_club_goals'))}-{_to_int(row.get('away_club_goals'))}"
                        if _to_int(row.get("home_club_goals")) is not None and _to_int(row.get("away_club_goals")) is not None
                        else _text(row.get("aggregate"), 100)
                    ),
                },
            ),
            session,
        )
        totals["fixtures"] += imported
        totals["errors"] += errors
    else:
        print(f"  games.csv no encontrado en {root}")

    appearances_path = root / "appearances.csv"
    if appearances_path.exists():
        imported, errors = _run_rows(
            "appearances.csv -> player_stats",
            _csv_rows(appearances_path),
            lambda row: _upsert_player_stat(
                session,
                {
                    "fixture_id": _namespaced_id(row.get("game_id"), TRANSFERMARKT_FIXTURE_OFFSET),
                    "player_id": _namespaced_id(row.get("player_id"), TRANSFERMARKT_PLAYER_OFFSET),
                    "appearances": 1,
                    "minutes": _to_int(row.get("minutes_played") or row.get("minutes")),
                    "shots": None,
                    "shots_on_target": None,
                    "fouls_committed": None,
                    "fouls_drawn": None,
                    "saves": None,
                    "assists": _to_int(row.get("assists")),
                    "goals": _to_int(row.get("goals")),
                    "yellow_cards": _to_int(row.get("yellow_cards")),
                    "red_cards": _to_int(row.get("red_cards")),
                    "rating": None,
                },
            ),
            session,
        )
        totals["player_stats"] += imported
        totals["errors"] += errors
    else:
        print(f"  appearances.csv no encontrado en {root}")

    events_path = root / "game_events.csv"
    if events_path.exists():
        imported, errors = _run_rows(
            "game_events.csv -> game_events",
            _csv_rows(events_path),
            lambda row: _upsert_game_event(
                session,
                {
                    "source": "transfermarkt",
                    "source_event_id": _text(row.get("game_event_id") or f"{row.get('game_id')}:{row.get('minute')}:{row.get('type')}:{row.get('player_id')}", 100),
                    "fixture_id": _namespaced_id(row.get("game_id"), TRANSFERMARKT_FIXTURE_OFFSET),
                    "team_id": _namespaced_id(row.get("club_id"), TRANSFERMARKT_TEAM_OFFSET),
                    "player_id": _namespaced_id(row.get("player_id"), TRANSFERMARKT_PLAYER_OFFSET),
                    "related_player_id": _namespaced_id(row.get("player_in_id") or row.get("player_assist_id"), TRANSFERMARKT_PLAYER_OFFSET),
                    "minute": _to_int(row.get("minute")),
                    "event_type": _text(row.get("type"), 100),
                    "description": _text(row.get("description"), 500),
                    "event_date": _date(row.get("date")),
                },
            ),
            session,
        )
        totals["game_events"] += imported
        totals["errors"] += errors
    else:
        print(f"  game_events.csv no encontrado en {root}")

    return totals


def _sqlite_rows(conn: sqlite3.Connection, table: str):
    try:
        cursor = conn.execute(f'SELECT * FROM "{table}"')
    except sqlite3.Error:
        logging.exception("No se pudo leer tabla SQLite %s", table)
        return []
    return (dict(row) for row in cursor)


def import_soccer_db(path: Path, session) -> dict[str, int]:
    print(f"Importando European Soccer Database desde {path}")
    totals = {"teams": 0, "players": 0, "fixtures": 0, "player_attributes": 0, "errors": 0}
    if not path.exists():
        print(f"  database.sqlite no encontrado: {path}")
        totals["errors"] += 1
        return totals

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        team_names = {
            row["team_api_id"]: row["team_long_name"] or row["team_short_name"]
            for row in conn.execute('SELECT team_api_id, team_long_name, team_short_name FROM "Team"')
        }
        imported, errors = _run_rows(
            "Team -> teams",
            _sqlite_rows(conn, "Team"),
            lambda row: _upsert_team(
                session,
                {
                    "id": _namespaced_id(row.get("team_api_id") or row.get("id"), SOCCER_TEAM_OFFSET),
                    "name": _text(row.get("team_long_name") or row.get("team_short_name"), 255),
                    "league": "European Soccer Database",
                },
            ),
            session,
        )
        totals["teams"] += imported
        totals["errors"] += errors

        imported, errors = _run_rows(
            "Player -> players",
            _sqlite_rows(conn, "Player"),
            lambda row: _upsert_player(
                session,
                {
                    "id": _namespaced_id(row.get("player_api_id") or row.get("id"), SOCCER_PLAYER_OFFSET),
                    "name": _text(row.get("player_name"), 255) or "Unknown",
                    "team_id": None,
                    "position": None,
                    "nationality": None,
                    "age": _age_from_birth(row.get("birthday")),
                    "height": _height_cm(row.get("height")),
                    "weight": _weight_kg(row.get("weight")),
                    "birth_date": _text(row.get("birthday"), 50),
                    "birth_place": None,
                },
            ),
            session,
        )
        totals["players"] += imported
        totals["errors"] += errors

        imported, errors = _run_rows(
            "Match -> fixtures",
            _sqlite_rows(conn, "Match"),
            lambda row: _upsert_fixture(
                session,
                {
                    "id": _namespaced_id(row.get("match_api_id") or row.get("id"), SOCCER_FIXTURE_OFFSET),
                    "home_team": _text(team_names.get(row.get("home_team_api_id")), 255) or f"team:{row.get('home_team_api_id')}",
                    "away_team": _text(team_names.get(row.get("away_team_api_id")), 255) or f"team:{row.get('away_team_api_id')}",
                    "league": _text(row.get("league_id"), 255),
                    "date": _date(row.get("date")),
                    "status": "FT",
                    "result": (
                        f"{_to_int(row.get('home_team_goal'))}-{_to_int(row.get('away_team_goal'))}"
                        if _to_int(row.get("home_team_goal")) is not None and _to_int(row.get("away_team_goal")) is not None
                        else None
                    ),
                },
            ),
            session,
        )
        totals["fixtures"] += imported
        totals["errors"] += errors

        imported, errors = _run_rows(
            "Player_Attributes -> player_attributes",
            _sqlite_rows(conn, "Player_Attributes"),
            lambda row: _upsert_player_attribute(
                session,
                {
                    "source": "european_soccer",
                    "source_attribute_id": _text(row.get("id"), 100),
                    "player_id": _namespaced_id(row.get("player_api_id"), SOCCER_PLAYER_OFFSET),
                    "attribute_date": _date(row.get("date")),
                    "overall_rating": _to_int(row.get("overall_rating")),
                    "potential": _to_int(row.get("potential")),
                    "preferred_foot": _text(row.get("preferred_foot"), 50),
                    "attacking_work_rate": _text(row.get("attacking_work_rate"), 50),
                    "defensive_work_rate": _text(row.get("defensive_work_rate"), 50),
                },
            ),
            session,
        )
        totals["player_attributes"] += imported
        totals["errors"] += errors
    finally:
        conn.close()

    return totals


def _print_summary(summary: dict[str, dict[str, int]]) -> None:
    print("")
    print("Resumen de importación")
    for dataset, totals in summary.items():
        print(f"- {dataset}")
        for key, value in totals.items():
            print(f"  {key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Importa datasets de Kaggle a PostgreSQL.")
    parser.add_argument("--transfermarkt", type=Path, help="Directorio con CSVs de Transfermarkt.")
    parser.add_argument("--soccer-db", type=Path, help="Ruta al database.sqlite de European Soccer Database.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.transfermarkt and not args.soccer_db:
        parser.error("Indicá --transfermarkt, --soccer-db o ambos.")

    if not initialize_postgres():
        print("ERROR: DATABASE_URL no está configurado; no se puede importar a PostgreSQL.")
        return 1

    summary: dict[str, dict[str, int]] = {}
    with get_session() as session:
        if args.transfermarkt:
            try:
                summary["transfermarkt"] = import_transfermarkt(args.transfermarkt, session)
            except Exception:
                session.rollback()
                logging.exception("Falló la importación de Transfermarkt")
                summary["transfermarkt"] = {"errors": 1}

        if args.soccer_db:
            try:
                summary["european_soccer"] = import_soccer_db(args.soccer_db, session)
            except Exception:
                session.rollback()
                logging.exception("Falló la importación de European Soccer Database")
                summary["european_soccer"] = {"errors": 1}

    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
