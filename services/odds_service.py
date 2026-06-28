import os
import unicodedata
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEYS = ("soccer", "soccer_fifa_world_cup")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").replace(" ", "").strip()


def _norm(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


def _team_match_score(event: Dict[str, Any], home_team: str, away_team: str) -> int:
    home = _norm(home_team)
    away = _norm(away_team)
    event_home = _norm(event.get("home_team"))
    event_away = _norm(event.get("away_team"))
    teams = [_norm(t) for t in event.get("teams", [])]

    score = 0
    if home and home == event_home:
        score += 4
    elif home and (home in event_home or event_home in home):
        score += 2
    elif home in teams:
        score += 1

    if away and away == event_away:
        score += 4
    elif away and (away in event_away or event_away in away):
        score += 2
    elif away in teams:
        score += 1

    return score


def _find_event(events: list[Dict[str, Any]], home_team: str, away_team: str) -> Optional[Dict[str, Any]]:
    if not events:
        return None
    best = max(events, key=lambda event: _team_match_score(event, home_team, away_team))
    return best if _team_match_score(best, home_team, away_team) >= 4 else None


def _empty_odds(home_team: str, away_team: str, error: str = None) -> Dict[str, Any]:
    return {
        "home_team": home_team,
        "away_team": away_team,
        "bookmaker": None,
        "h2h": {},
        "totals": {},
        "btts": {},
        "error": error,
    }


def _set_best(target: Dict[str, Any], key: str, price, bookmaker: Dict[str, Any], label: str):
    if price is None:
        return
    try:
        price = float(price)
    except (TypeError, ValueError):
        return
    current = target.get(key)
    if current is None or price > current.get("price", 0):
        target[key] = {
            "price": price,
            "bookmaker": bookmaker.get("title") or bookmaker.get("key"),
            "label": label,
        }


def _extract_best_available(event: Dict[str, Any], home_team: str, away_team: str) -> Dict[str, Any]:
    result = _empty_odds(home_team, away_team)
    result["home_team"] = event.get("home_team") or home_team
    result["away_team"] = event.get("away_team") or away_team

    bookmakers = event.get("bookmakers", []) or []
    result["bookmaker"] = "bet365 o mejor disponible" if bookmakers else None

    def _bookmaker_rank(bookmaker: Dict[str, Any]) -> int:
        return 0 if bookmaker.get("key") == "bet365" else 1

    for bookmaker in sorted(bookmakers, key=_bookmaker_rank):
        for market in bookmaker.get("markets", []) or []:
            market_key = market.get("key")
            for outcome in market.get("outcomes", []) or []:
                name = outcome.get("name")
                price = outcome.get("price")
                point = outcome.get("point")
                norm_name = _norm(name)

                if market_key == "h2h":
                    if norm_name == _norm(event.get("home_team")) or norm_name == _norm(home_team):
                        _set_best(result["h2h"], "home", price, bookmaker, name)
                    elif norm_name == _norm(event.get("away_team")) or norm_name == _norm(away_team):
                        _set_best(result["h2h"], "away", price, bookmaker, name)
                    elif norm_name in ("draw", "empate"):
                        _set_best(result["h2h"], "draw", price, bookmaker, name)

                elif market_key == "totals" and float(point or 0) == 2.5:
                    if norm_name == "over":
                        _set_best(result["totals"], "over_2_5", price, bookmaker, "Over 2.5 goles")
                    elif norm_name == "under":
                        _set_best(result["totals"], "under_2_5", price, bookmaker, "Under 2.5 goles")

                elif market_key == "btts":
                    if norm_name in ("yes", "si", "sí"):
                        _set_best(result["btts"], "yes", price, bookmaker, "BTTS Sí")
                    elif norm_name in ("no",):
                        _set_best(result["btts"], "no", price, bookmaker, "BTTS No")

    return result


def _fetch_events(sport_key: str, params: Dict[str, Any]):
    response = requests.get(f"{BASE_URL}/sports/{sport_key}/odds", params=params, timeout=20)
    if response.status_code == 422 and "btts" in str(params.get("markets", "")):
        fallback = dict(params)
        fallback["markets"] = "h2h,totals"
        response = requests.get(f"{BASE_URL}/sports/{sport_key}/odds", params=fallback, timeout=20)
    response.raise_for_status()
    return response.json()


def get_match_odds(home_team: str, away_team: str) -> Dict[str, Any]:
    """Return bet365 odds, or best available odds, for 1X2, O/U 2.5 and BTTS."""
    if not ODDS_API_KEY:
        return _empty_odds(home_team, away_team, "ODDS_API_KEY no definido")

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu",
        "markets": "h2h,totals,btts",
        "oddsFormat": "decimal",
    }
    last_error = None
    for sport_key in SPORT_KEYS:
        try:
            events = _fetch_events(sport_key, params)
        except Exception as exc:
            last_error = str(exc)
            continue

        event = _find_event(events if isinstance(events, list) else [], home_team, away_team)
        if event:
            odds = _extract_best_available(event, home_team, away_team)
            odds["sport_key"] = sport_key
            return odds

    return _empty_odds(home_team, away_team, last_error or "Partido no encontrado en The Odds API")
