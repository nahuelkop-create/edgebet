import os
from dotenv import load_dotenv
import requests
from datetime import date, timedelta, datetime
from typing import List, Dict, Any, Optional

load_dotenv()
BASE_URL = "https://v3.football.api-sports.io"
API_KEY = os.getenv("API_FOOTBALL_KEY", "").replace(" ", "").strip()

HEADERS = {"x-apisports-key": API_KEY} if API_KEY else {}

# World Cup 2026 league ID
WORLD_CUP_LEAGUE_ID = 1

COMPETITIONS = {
    "Mundial 2026": "1",
    "Premier League": "39",
    "La Liga": "140",
    "Copa Libertadores": "144",
    "Liga Profesional Argentina": "128",
}


def _get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY no está definido")
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, params=params, timeout=15)
    response.raise_for_status()
    return response.json()


def get_fixtures_by_date(date_str: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get fixtures for a specific date"""
    if date_str is None:
        date_str = (datetime.utcnow() - timedelta(hours=3)).date().isoformat()

    fixtures: List[Dict[str, Any]] = []
    try:
        # API v3 endpoint for fixtures
        params = {
            "date": date_str,
            "league": WORLD_CUP_LEAGUE_ID,
            "season": 2026
        }
        data = _get("/fixtures", params=params)
        
        for fixture in data.get("response", []):
            try:
                fixture_data = {
                    "id": fixture.get("fixture", {}).get("id"),
                    "homeTeam": {
                        "name": fixture.get("teams", {}).get("home", {}).get("name"),
                        "id": fixture.get("teams", {}).get("home", {}).get("id"),
                    },
                    "awayTeam": {
                        "name": fixture.get("teams", {}).get("away", {}).get("name"),
                        "id": fixture.get("teams", {}).get("away", {}).get("id"),
                    },
                    "competition": {
                        "name": fixture.get("league", {}).get("name"),
                        "id": fixture.get("league", {}).get("id"),
                    },
                    "utcDate": fixture.get("fixture", {}).get("date"),
                    "referee": fixture.get("fixture", {}).get("referee"),
                    "status": fixture.get("fixture", {}).get("status", {}).get("short"),
                    "stage": fixture.get("league", {}).get("round"),
                    "group": "",
                    "matchday": 1,
                    "score": {
                        "fullTime": {
                            "home": fixture.get("goals", {}).get("home"),
                            "away": fixture.get("goals", {}).get("away"),
                        }
                    }
                }
                fixtures.append(fixture_data)
            except Exception:
                continue
                
    except requests.HTTPError as e:
        pass
    except Exception as e:
        pass

    return fixtures


def get_fixtures_today() -> List[Dict[str, Any]]:
    """Get today's fixtures"""
    return get_fixtures_by_date()


# Statuses that mean the match has NOT been played yet (no per-fixture player
# data exists). For these we fall back to tournament-wide leaders.
PRE_MATCH_STATUSES = {"TBD", "NS", "TIMED", "SCHEDULED", "PST"}


def _is_pre_match(status: Optional[str]) -> bool:
    return bool(status) and str(status).upper() in PRE_MATCH_STATUSES


def _team_leader(leaders: List[Dict[str, Any]], team_id: Any) -> Optional[Dict[str, Any]]:
    """Top player of a single team from a leaderboard list."""
    candidates = [l for l in leaders if l.get("team_id") == team_id]
    candidates.sort(key=lambda l: l.get("value", 0), reverse=True)
    return candidates[0] if candidates else None


def _prematch_player_stats(
    teams: List[Dict[str, Any]],
    league_id: int = WORLD_CUP_LEAGUE_ID,
    season: int = 2026,
) -> Dict[str, Any]:
    """Pre-match player profile: per team show the tournament leaders (top
    scorer, top assistant, most fouls committed, most yellow cards).

    `teams` is a list of {"id", "name"} dicts (home first, away second).
    """
    leaders = get_tournament_player_leaders(league_id, season)
    scorers = leaders.get("scorers", [])
    assists = leaders.get("assists", [])
    fouls = leaders.get("fouls", [])
    yellow = leaders.get("yellow_cards", [])

    profiles: Dict[str, Any] = {}
    for team in teams:
        tid = team.get("id")
        tname = team.get("name", "Unknown")
        profile = {
            "top_scorer": _team_leader(scorers, tid),
            "top_assist": _team_leader(assists, tid),
            "top_fouls": _team_leader(fouls, tid),
            "top_cards": _team_leader(yellow, tid),
        }
        # The global /players/top* lists only cover the tournament-wide top ~20,
        # so many squads are absent. Fill any gaps with this team's own leaders.
        if tid is not None and any(v is None for v in profile.values()):
            squad = get_team_player_leaders(tid, league_id, season)
            for k in profile:
                if profile[k] is None:
                    profile[k] = squad.get(k)
            profile["top_keeper"] = squad.get("top_keeper")
        profiles[tname] = profile

    return {"mode": "pre_match", "profiles": profiles}


def get_player_stats(
    fixture_id: int,
    status: Optional[str] = None,
    teams: Optional[List[Dict[str, Any]]] = None,
    league_id: int = WORLD_CUP_LEAGUE_ID,
    season: int = 2026,
) -> Dict[str, Any]:
    """Get player statistics for a fixture.

    - If the match has NOT been played yet (status in PRE_MATCH_STATUSES), there
      is no per-fixture player data, so we return tournament-wide leaders per team
      via `get_tournament_player_leaders()` (mode='pre_match').
    - If the match is live or finished, we use the real per-fixture player data
      from /fixtures/players (mode='in_play').
    """
    if _is_pre_match(status):
        return _prematch_player_stats(teams or [], league_id, season)

    try:
        params = {"fixture": fixture_id}
        data = _get("/fixtures/players", params=params)

        player_stats = {}
        for team_data in data.get("response", []):
            team_name = team_data.get("team", {}).get("name", "Unknown")
            players = []
            
            for player in team_data.get("players", []):
                try:
                    stats = player.get("statistics", [{}])[0] if player.get("statistics") else {}
                    games = stats.get("games", {})
                    
                    # Extract stats, handling None values
                    goals = stats.get("goals", {})
                    shots = stats.get("shots", {})
                    passes = stats.get("passes", {})
                    tackles = stats.get("tackles", {})
                    fouls = stats.get("fouls", {})
                    cards = stats.get("cards", {})
                    
                    minutes = int(games.get("minutes", 0) or 0)
                    
                    player_info = {
                        "name": player.get("player", {}).get("name", "Unknown"),
                        "number": games.get("number"),
                        "position": games.get("position", "N/A"),
                        "rating": games.get("rating"),
                        "minutes": minutes,
                        "goals": int(goals.get("total") or 0),
                        "assists": int(goals.get("assists") or 0),
                        "shots": int(shots.get("total") or 0),
                        "shots_on_target": int(shots.get("on") or 0),
                        "fouls_committed": int(fouls.get("committed") or 0),
                        "fouls_drawn": int(fouls.get("drawn") or 0),
                        "cards": {
                            "yellow": int(cards.get("yellow", 0) or 0),
                            "red": int(cards.get("red", 0) or 0),
                        },
                        "saves": int(goals.get("saves") or 0),
                        "tackles": int(tackles.get("total") or 0),
                        "passes": int(passes.get("total") or 0),
                        "pass_accuracy": str(passes.get("accuracy", "0") or "0"),
                        "interceptions": int(tackles.get("interceptions") or 0),
                        "blocks": int(tackles.get("blocks") or 0),
                    }
                    # Only include players who actually played (have stats)
                    if minutes > 0:
                        players.append(player_info)
                except Exception as ex:
                    # Skip individual player parsing errors
                    continue
            
            # Sort by rating (if available) to get best performers first
            try:
                players.sort(key=lambda x: float(str(x.get("rating", "0")).replace(",", ".")) if x.get("rating") else 0, reverse=True)
            except Exception as sort_ex:
                # If sorting fails, keep original order
                pass
            
            player_stats[team_name] = players

        return {"mode": "in_play", "teams": player_stats}
    except requests.HTTPError as e:
        return {"mode": "in_play", "teams": {}}
    except Exception as e:
        return {"mode": "in_play", "teams": {}}


def _tournament_leaders(path: str, value_getter, league_id: int, season: int) -> List[Dict[str, Any]]:
    """Fetch a /players/top* leaderboard and flatten it to a simple list.

    Each entry: {name, team, team_id, value}. `value_getter` pulls the relevant
    number (goals / assists / yellow cards) out of an API statistics block.
    """
    try:
        data = _get(path, params={"league": league_id, "season": season})
    except Exception:
        return []

    leaders: List[Dict[str, Any]] = []
    for item in data.get("response", []):
        try:
            player = item.get("player", {})
            stats = item.get("statistics", [{}])
            stat = stats[0] if stats else {}
            team = stat.get("team", {})
            value = value_getter(stat) or 0
            leaders.append({
                "name": player.get("name", "Unknown"),
                "team": team.get("name", "Unknown"),
                "team_id": team.get("id"),
                "value": int(value),
                # Fouls committed are carried in every /players/top* stat block,
                # so we piggyback them to derive a fouls leaderboard below.
                "fouls": int((stat.get("fouls") or {}).get("committed") or 0),
            })
        except Exception:
            continue
    return leaders


def _merge_foul_leaders(*leader_lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a 'most fouls committed' leaderboard from the players that appear in
    the other leaderboards. API-Football has no /players/topfouls endpoint, so we
    reuse the fouls figure embedded in each player's stat block (deduped by player).
    """
    seen: Dict[Any, Dict[str, Any]] = {}
    for lst in leader_lists:
        for l in lst:
            key = (l.get("name"), l.get("team_id"))
            fouls = l.get("fouls", 0)
            if key not in seen or fouls > seen[key]["value"]:
                seen[key] = {
                    "name": l.get("name"),
                    "team": l.get("team"),
                    "team_id": l.get("team_id"),
                    "value": fouls,
                }
    return sorted(seen.values(), key=lambda x: x.get("value", 0), reverse=True)


def get_tournament_player_leaders(league_id: int = WORLD_CUP_LEAGUE_ID, season: int = 2026) -> Dict[str, Any]:
    """Tournament-wide (cumulative) player leaderboards: top scorers, top
    assists, most yellow cards and most fouls committed. These come from the
    /players/top* endpoints since per-fixture data only covers a single match.
    """
    scorers = _tournament_leaders(
        "/players/topscorers",
        lambda s: s.get("goals", {}).get("total"),
        league_id, season,
    )
    assists = _tournament_leaders(
        "/players/topassists",
        lambda s: s.get("goals", {}).get("assists"),
        league_id, season,
    )
    yellow_cards = _tournament_leaders(
        "/players/topyellowcards",
        lambda s: s.get("cards", {}).get("yellow"),
        league_id, season,
    )
    return {
        "scorers": scorers,
        "assists": assists,
        "yellow_cards": yellow_cards,
        "fouls": _merge_foul_leaders(scorers, assists, yellow_cards),
    }


def _top_player(players: List[Dict[str, Any]], value_getter) -> Optional[Dict[str, Any]]:
    """Pick the player with the highest value (>0) from a list of {name, stat}."""
    best = None
    best_val = 0
    for p in players:
        val = value_getter(p) or 0
        if val > best_val:
            best_val = val
            best = {"name": p.get("name"), "team": p.get("team"),
                    "team_id": p.get("team_id"), "value": int(val)}
    return best


def get_team_player_leaders(
    team_id: int,
    league_id: int = WORLD_CUP_LEAGUE_ID,
    season: int = 2026,
    max_pages: int = 5,
) -> Dict[str, Any]:
    """A single team's tournament leaders (top scorer / assistant / fouls / cards
    / saves), aggregated from /players. Unlike the global /players/top* lists,
    this covers every squad, not just the tournament-wide top 20.
    """
    players: List[Dict[str, Any]] = []
    try:
        page = 1
        total_pages = 1
        while page <= total_pages and page <= max_pages:
            data = _get(
                "/players",
                params={"team": team_id, "league": league_id, "season": season, "page": page},
            )
            total_pages = data.get("paging", {}).get("total", 1) or 1
            for item in data.get("response", []):
                stat = (item.get("statistics") or [{}])[0]
                team = stat.get("team", {})
                goals = stat.get("goals", {}) or {}
                fouls = stat.get("fouls", {}) or {}
                cards = stat.get("cards", {}) or {}
                players.append({
                    "name": item.get("player", {}).get("name", "Unknown"),
                    "team": team.get("name", "Unknown"),
                    "team_id": team.get("id", team_id),
                    "goals": int(goals.get("total") or 0),
                    "assists": int(goals.get("assists") or 0),
                    "saves": int(goals.get("saves") or 0),
                    "fouls": int(fouls.get("committed") or 0),
                    "yellow": int(cards.get("yellow") or 0),
                })
            page += 1
    except Exception:
        pass

    return {
        "top_scorer": _top_player(players, lambda p: p.get("goals")),
        "top_assist": _top_player(players, lambda p: p.get("assists")),
        "top_fouls": _top_player(players, lambda p: p.get("fouls")),
        "top_cards": _top_player(players, lambda p: p.get("yellow")),
        "top_keeper": _top_player(players, lambda p: p.get("saves")),
    }


def _parse_percent(value: Any) -> Optional[float]:
    """Turn an API value like '49%' into 49.0; return None if not parseable."""
    if value is None:
        return None
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _parse_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except (ValueError, TypeError):
        return None


def _avg(values: List[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def _finished_fixture(raw_fixture: Dict[str, Any]) -> bool:
    return raw_fixture.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")


def get_fixture_details(fixture_id: int) -> Dict[str, Any]:
    """Fetch the canonical fixture payload, including referee when available."""
    try:
        data = _get("/fixtures", params={"id": fixture_id})
        response = data.get("response", [])
        return response[0] if response else {}
    except requests.HTTPError:
        return {}
    except Exception:
        return {}


def _sum_fixture_stat(fixture_id: int, stat_name: str) -> Optional[float]:
    """Sum a stat across both teams for one fixture."""
    try:
        data = _get("/fixtures/statistics", params={"fixture": fixture_id})
    except Exception:
        return None

    total = 0.0
    found = False
    for team_data in data.get("response", []):
        flat = {s.get("type"): s.get("value") for s in team_data.get("statistics", [])}
        value = _parse_number(flat.get(stat_name))
        if value is not None:
            total += value
            found = True
    return total if found else None


def _fixture_score(raw_fixture: Dict[str, Any]) -> Dict[str, Optional[int]]:
    goals = raw_fixture.get("goals", {}) or {}
    return {"home": goals.get("home"), "away": goals.get("away")}


def _fixture_winner(raw_fixture: Dict[str, Any]) -> str:
    teams = raw_fixture.get("teams", {}) or {}
    home = teams.get("home", {}) or {}
    away = teams.get("away", {}) or {}
    score = _fixture_score(raw_fixture)
    home_goals = score.get("home")
    away_goals = score.get("away")
    if home_goals is None or away_goals is None:
        return "N/D"
    if home_goals > away_goals:
        return home.get("name") or "Local"
    if away_goals > home_goals:
        return away.get("name") or "Visitante"
    return "Empate"


def get_referee_profile(fixture_id: int, season: int = 2026) -> Dict[str, Any]:
    """Return referee name plus strictness profile from his recent fixtures.

    API-Football exposes the referee in /fixtures?id=...; cards/fouls are derived
    from /fixtures/statistics for the referee's finished fixtures in that season.
    """
    fixture = get_fixture_details(fixture_id)
    referee = fixture.get("fixture", {}).get("referee") or ""
    profile: Dict[str, Any] = {
        "name": referee,
        "matches_analyzed": 0,
        "yellow_cards_per_match": None,
        "fouls_per_match": None,
        "style": "N/D",
    }
    if not referee:
        return profile

    try:
        data = _get("/fixtures", params={"referee": referee, "season": season})
        history = [f for f in data.get("response", []) if _finished_fixture(f)]
    except Exception:
        history = []

    yellow_cards: List[float] = []
    fouls: List[float] = []
    for item in history[:12]:
        fid = item.get("fixture", {}).get("id")
        if not fid:
            continue
        cards_total = _sum_fixture_stat(fid, "Yellow Cards")
        fouls_total = _sum_fixture_stat(fid, "Fouls")
        if cards_total is not None:
            yellow_cards.append(cards_total)
        if fouls_total is not None:
            fouls.append(fouls_total)

    avg_yellow = _avg(yellow_cards)
    avg_fouls = _avg(fouls)
    style = "N/D"
    if avg_yellow is not None or avg_fouls is not None:
        if (avg_yellow is not None and avg_yellow >= 4.5) or (avg_fouls is not None and avg_fouls >= 27):
            style = "estricto"
        elif (avg_yellow is not None and avg_yellow <= 3.0) and (avg_fouls is None or avg_fouls <= 22):
            style = "permisivo"
        else:
            style = "moderado"

    profile.update({
        "matches_analyzed": max(len(yellow_cards), len(fouls)),
        "yellow_cards_per_match": avg_yellow,
        "fouls_per_match": avg_fouls,
        "style": style,
    })
    return profile


def get_head_to_head(team1_id: int, team2_id: int, last: int = 10) -> Dict[str, Any]:
    """Get H2H history and derive goal/foul tendencies from the latest meetings."""
    try:
        data = _get(
            "/fixtures/headtohead",
            params={"h2h": f"{team1_id}-{team2_id}", "last": last},
        )
        fixtures = data.get("response", [])
    except Exception:
        fixtures = []

    recent = [f for f in fixtures if _finished_fixture(f)][:5]
    matches: List[Dict[str, Any]] = []
    goal_totals: List[float] = []
    foul_totals: List[float] = []

    for item in recent:
        fid = item.get("fixture", {}).get("id")
        teams = item.get("teams", {}) or {}
        home = teams.get("home", {}) or {}
        away = teams.get("away", {}) or {}
        score = _fixture_score(item)
        home_goals = score.get("home")
        away_goals = score.get("away")
        if home_goals is not None and away_goals is not None:
            goal_totals.append(float(home_goals + away_goals))

        fouls_total = _sum_fixture_stat(fid, "Fouls") if fid else None
        if fouls_total is not None:
            foul_totals.append(fouls_total)

        matches.append({
            "id": fid,
            "date": item.get("fixture", {}).get("date"),
            "home": home.get("name"),
            "away": away.get("name"),
            "score": score,
            "winner": _fixture_winner(item),
            "fouls": fouls_total,
        })

    avg_goals = _avg(goal_totals)
    avg_fouls = _avg(foul_totals)
    foul_tendency = "N/D"
    if avg_fouls is not None:
        if avg_fouls >= 28:
            foul_tendency = "muchas faltas"
        elif avg_fouls <= 20:
            foul_tendency = "pocas faltas"
        else:
            foul_tendency = "faltas moderadas"

    return {
        "matches": matches,
        "avg_goals": avg_goals,
        "avg_fouls": avg_fouls,
        "foul_tendency": foul_tendency,
    }


def get_fixture_lineups(fixture_id: int) -> Dict[str, Any]:
    """Return official starting XIs when API-Football has published lineups."""
    try:
        data = _get("/fixtures/lineups", params={"fixture": fixture_id})
        response = data.get("response", [])
    except Exception:
        response = []

    if not response:
        return {"confirmed": False, "message": "Alineación no confirmada", "teams": []}

    teams = []
    for team_data in response:
        team = team_data.get("team", {}) or {}
        start_xi = []
        for entry in team_data.get("startXI", []) or []:
            player = entry.get("player", {}) or {}
            start_xi.append({
                "name": player.get("name"),
                "number": player.get("number"),
                "position": player.get("pos"),
            })
        teams.append({
            "team": team.get("name"),
            "formation": team_data.get("formation"),
            "startXI": start_xi,
        })

    return {"confirmed": True, "message": "Alineación oficial disponible", "teams": teams}


def _fixture_team_statistics(fixture_id: int, team_id: int) -> Dict[str, Any]:
    """Return the {type: value} statistics map for one team in one fixture."""
    try:
        data = _get("/fixtures/statistics", params={"fixture": fixture_id, "team": team_id})
        response = data.get("response", [])
        if not response:
            return {}
        stat_list = response[0].get("statistics", [])
        return {s.get("type"): s.get("value") for s in stat_list}
    except requests.HTTPError:
        return {}
    except Exception:
        return {}


def get_team_stats(team_id: int, league_id: int = WORLD_CUP_LEAGUE_ID, season: int = 2026) -> Dict[str, Any]:
    """Get real team statistics: goals per match (from /teams/statistics) plus
    corners, possession, shots and fouls per match aggregated from the team's
    recent finished fixtures (via /fixtures/statistics).
    """
    stats: Dict[str, Any] = {"team": None, "team_id": team_id}

    # 1) Season aggregates from /teams/statistics (goals, played, form)
    try:
        data = _get(
            "/teams/statistics",
            params={"league": league_id, "season": season, "team": team_id},
        )
        resp = data.get("response") or {}
    except Exception:
        resp = {}

    fixtures = resp.get("fixtures", {})
    played = fixtures.get("played", {}).get("total") or 0
    goals = resp.get("goals", {})
    goals_for = goals.get("for", {}).get("total", {}).get("total") or 0
    goals_against = goals.get("against", {}).get("total", {}).get("total") or 0
    gpm = _parse_number(goals.get("for", {}).get("average", {}).get("total"))
    if gpm is None:
        gpm = (goals_for / played) if played else 0.0

    stats.update({
        "team": resp.get("team", {}).get("name"),
        "form": resp.get("form"),
        "matches_played": played,
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goals_per_match": gpm,
        "clean_sheets": resp.get("clean_sheet", {}).get("total"),
    })

    # 2) Per-match stats (corners/possession/shots/fouls) aggregated from recent
    #    finished fixtures, since /teams/statistics does not expose them.
    corners, possession, shots, fouls = [], [], [], []
    try:
        recent = _get("/fixtures", params={"team": team_id, "last": 7})
        finished = [
            f for f in recent.get("response", [])
            if f.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
        ][:5]
    except Exception:
        finished = []

    for f in finished:
        fid = f.get("fixture", {}).get("id")
        if not fid:
            continue
        fstats = _fixture_team_statistics(fid, team_id)
        if not fstats:
            continue
        c = _parse_number(fstats.get("Corner Kicks"))
        p = _parse_percent(fstats.get("Ball Possession"))
        s = _parse_number(fstats.get("Total Shots"))
        fl = _parse_number(fstats.get("Fouls"))
        if c is not None:
            corners.append(c)
        if p is not None:
            possession.append(p)
        if s is not None:
            shots.append(s)
        if fl is not None:
            fouls.append(fl)

    stats.update({
        "corners_per_match": _avg(corners),
        "possession_avg": _avg(possession),
        "shots_per_match": _avg(shots),
        "fouls_per_match": _avg(fouls),
        "per_match_sample": len(corners),
    })

    return stats


def get_team_recent_matches(team_id: int, limit: int = 5, date_to: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get the team's most recent matches across all competitions (real data).

    Uses the `last` filter so we get genuine recent form, not only World Cup games.
    """
    try:
        params = {
            "team": team_id,
            "last": limit,
        }
        data = _get("/fixtures", params=params)
        
        matches = []
        for fixture in data.get("response", []):
            try:
                match = {
                    "id": fixture.get("fixture", {}).get("id"),
                    "homeTeam": {
                        "name": fixture.get("teams", {}).get("home", {}).get("name"),
                        "id": fixture.get("teams", {}).get("home", {}).get("id"),
                    },
                    "awayTeam": {
                        "name": fixture.get("teams", {}).get("away", {}).get("name"),
                        "id": fixture.get("teams", {}).get("away", {}).get("id"),
                    },
                    "utcDate": fixture.get("fixture", {}).get("date"),
                    "status": fixture.get("fixture", {}).get("status", {}).get("short"),
                    "score": {
                        "fullTime": {
                            "home": fixture.get("goals", {}).get("home"),
                            "away": fixture.get("goals", {}).get("away"),
                        }
                    }
                }
                matches.append(match)
            except Exception:
                continue
        
        return matches
    except requests.HTTPError:
        return []
    except Exception:
        return []


def get_group_standings(competition_id: str, group_code: str) -> Dict[str, Any]:
    """Get group standings (World Cup groups)"""
    try:
        params = {
            "league": WORLD_CUP_LEAGUE_ID,
            "season": 2026,
        }
        data = _get("/standings", params=params)

        for standing in data.get("response", []):
            # API-Football returns `standings` as a list of group tables
            # (each is a list of rows), not a dict. The group name lives in
            # each row's `group` field (e.g. "Group A").
            groups = standing.get("league", {}).get("standings", [])
            for group_table in groups:
                if not group_table:
                    continue
                group_name = group_table[0].get("group", "")
                if group_code.upper() in group_name.upper():
                    return {
                        "group_name": group_name,
                        "table": [
                            {
                                "position": row.get("rank"),
                                "team": {
                                    "id": row.get("team", {}).get("id"),
                                    "name": row.get("team", {}).get("name"),
                                },
                                "points": row.get("points"),
                                "playedGames": row.get("all", {}).get("played", 0),
                                "won": row.get("all", {}).get("win", 0),
                                "draw": row.get("all", {}).get("draw", 0),
                                "lost": row.get("all", {}).get("lose", 0),
                                "goalsFor": row.get("all", {}).get("goals", {}).get("for", 0),
                                "goalsAgainst": row.get("all", {}).get("goals", {}).get("against", 0),
                            }
                            for row in group_table
                        ]
                    }
        
        return {}
    except requests.HTTPError:
        return {}
    except Exception:
        return {}


def get_match_stats(match_id: int) -> Dict[str, Any]:
    """Get complete per-team match statistics for a fixture.

    The API returns `statistics` as a list of {type, value} entries, so we
    flatten each team's list into a keyed map.
    """
    try:
        data = _get("/fixtures/statistics", params={"fixture": match_id})

        stats: Dict[str, Any] = {}
        for team_data in data.get("response", []):
            team_name = team_data.get("team", {}).get("name", "Unknown")
            flat = {s.get("type"): s.get("value") for s in team_data.get("statistics", [])}
            stats[team_name] = {
                "team": team_name,
                "shots_on_goal": flat.get("Shots on Goal"),
                "total_shots": flat.get("Total Shots"),
                "possession": flat.get("Ball Possession"),
                "passes": flat.get("Total passes"),
                "pass_accuracy": flat.get("Passes %"),
                "fouls": flat.get("Fouls"),
                "corners": flat.get("Corner Kicks"),
                "offsides": flat.get("Offsides"),
                "yellow_cards": flat.get("Yellow Cards"),
                "red_cards": flat.get("Red Cards"),
                "saves": flat.get("Goalkeeper Saves"),
                "expected_goals": flat.get("expected_goals"),
            }

        return stats
    except requests.HTTPError:
        return {}
    except Exception:
        return {}
