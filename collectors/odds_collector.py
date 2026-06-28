import logging
import os
import unicodedata
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from sqlalchemy import select

from db.connection import get_session
from db.models import Fixture, OddsSnapshot


load_dotenv()

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEYS = ("soccer", "soccer_fifa_world_cup")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").replace(" ", "").strip()


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


def _price(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _event_key(home: str, away: str) -> tuple[str, str]:
    return (_norm(home), _norm(away))


def _fetch_events(sport_key: str) -> list[dict[str, Any]]:
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h,totals,btts,totals_corners,corners",
        "oddsFormat": "decimal",
    }
    response = requests.get(f"{BASE_URL}/sports/{sport_key}/odds", params=params, timeout=25)
    if response.status_code == 422:
        params["markets"] = "h2h,totals,btts"
        response = requests.get(f"{BASE_URL}/sports/{sport_key}/odds", params=params, timeout=25)
    if response.status_code == 422:
        params["markets"] = "h2h,totals"
        response = requests.get(f"{BASE_URL}/sports/{sport_key}/odds", params=params, timeout=25)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _build_fixture_index(session) -> dict[tuple[str, str], int]:
    rows = session.execute(
        select(Fixture.id, Fixture.home_team, Fixture.away_team).where(
            Fixture.status.in_(("TBD", "NS", "PST", "SCHEDULED", "TIMED"))
        )
    ).all()
    return {_event_key(row.home_team, row.away_team): int(row.id) for row in rows}


def _best_bookmakers(bookmakers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(bookmakers or [], key=lambda b: 0 if b.get("key") == "bet365" else 1)


def _snapshot_for_market(fixture_id: int, event: dict[str, Any], bookmaker: str, market: dict[str, Any]):
    market_key = market.get("key")
    prices = {outcome.get("name"): _price(outcome.get("price")) for outcome in market.get("outcomes", []) or []}
    timestamp = datetime.now(timezone.utc)

    if market_key == "h2h":
        home_name = next((name for name in prices if _norm(name) == _norm(event.get("home_team"))), None)
        away_name = next((name for name in prices if _norm(name) == _norm(event.get("away_team"))), None)
        draw_name = next((name for name in prices if _norm(name) in ("draw", "empate")), None)
        return OddsSnapshot(
            fixture_id=fixture_id,
            bookmaker=bookmaker,
            market="h2h",
            home_odds=prices.get(home_name),
            draw_odds=prices.get(draw_name),
            away_odds=prices.get(away_name),
            timestamp=timestamp,
        )

    if market_key == "totals":
        over = under = None
        for outcome in market.get("outcomes", []) or []:
            if float(outcome.get("point") or 0) != 2.5:
                continue
            if _norm(outcome.get("name")) == "over":
                over = _price(outcome.get("price"))
            elif _norm(outcome.get("name")) == "under":
                under = _price(outcome.get("price"))
        if over is None and under is None:
            return None
        return OddsSnapshot(
            fixture_id=fixture_id,
            bookmaker=bookmaker,
            market="totals_2_5",
            over_odds=over,
            under_odds=under,
            timestamp=timestamp,
        )

    if market_key in ("totals_corners", "corners"):
        over = under = None
        for outcome in market.get("outcomes", []) or []:
            if float(outcome.get("point") or 0) != 9.5:
                continue
            if _norm(outcome.get("name")) == "over":
                over = _price(outcome.get("price"))
            elif _norm(outcome.get("name")) == "under":
                under = _price(outcome.get("price"))
        if over is None and under is None:
            return None
        return OddsSnapshot(
            fixture_id=fixture_id,
            bookmaker=bookmaker,
            market="corners_9_5",
            over_odds=over,
            under_odds=under,
            timestamp=timestamp,
        )

    if market_key == "btts":
        yes = next((v for k, v in prices.items() if _norm(k) in ("yes", "si", "sí")), None)
        no = next((v for k, v in prices.items() if _norm(k) == "no"), None)
        return OddsSnapshot(
            fixture_id=fixture_id,
            bookmaker=bookmaker,
            market="btts",
            over_odds=yes,
            under_odds=no,
            timestamp=timestamp,
        )

    return None


def collect_odds() -> int:
    """Collect odds snapshots for upcoming fixtures already stored in PostgreSQL."""
    if not ODDS_API_KEY:
        logging.info("[odds_collector] ODDS_API_KEY no definido; collector deshabilitado.")
        return 0

    try:
        session = get_session()
    except RuntimeError:
        logging.info("[odds_collector] DATABASE_URL no configurado; collector deshabilitado.")
        return 0

    with session:
        fixture_index = _build_fixture_index(session)
        saved = 0

        for sport_key in SPORT_KEYS:
            try:
                events = _fetch_events(sport_key)
            except Exception:
                logging.exception("[odds_collector] error trayendo odds para %s", sport_key)
                continue

            for event in events:
                fixture_id = fixture_index.get(_event_key(event.get("home_team"), event.get("away_team")))
                if fixture_id is None:
                    continue

                for bookmaker in _best_bookmakers(event.get("bookmakers", [])):
                    bookmaker_name = bookmaker.get("title") or bookmaker.get("key")
                    for market in bookmaker.get("markets", []) or []:
                        snapshot = _snapshot_for_market(fixture_id, event, bookmaker_name, market)
                        if snapshot is not None:
                            session.add(snapshot)
                            saved += 1
                    if bookmaker.get("key") == "bet365":
                        break

        session.commit()

    logging.info("[odds_collector] snapshots guardados: %s", saved)
    return saved
