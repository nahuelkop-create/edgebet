import json
import unicodedata
from datetime import datetime

from flask import Flask, jsonify, render_template, request
from sqlalchemy import distinct, func, or_, select

from db.connection import get_session
from db.models import (
    Fixture,
    InjuryReport,
    ModelPerformance,
    OddsSnapshot,
    Player,
    PlayerStat,
    Prediction,
    Team,
    TeamStat,
)
from services.database import get_connection, initialize_db
from services.evaluation_engine import (
    get_model_performance,
    get_model_performance_history,
    get_postgres_stats,
    get_recent_predictions,
    get_value_bet_performance,
    get_value_bets,
)

app = Flask(__name__)
FINISHED_STATUSES = {"FT", "AET", "PEN"}
UPCOMING_STATUSES = {"TBD", "NS", "PST", "SCHEDULED", "TIMED"}


def _money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _parse_dt(value: str):
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower()


def _iso(value):
    return value.isoformat() if value else None


def _sort_timestamp(value, fallback: float = 0.0) -> float:
    if not value:
        return fallback
    try:
        return value.timestamp()
    except (AttributeError, OSError, ValueError):
        return fallback


def _na(value):
    return value if value not in (None, "") else "No disponible"


def _bool_arg(name: str) -> bool | None:
    value = request.args.get(name)
    if value is None:
        return None
    return str(value).lower() in {"1", "true", "yes", "si", "sí"}


def _pagination(default_limit: int = 50, max_limit: int = 200) -> tuple[int, int]:
    try:
        limit = min(max(int(request.args.get("limit", default_limit)), 1), max_limit)
    except ValueError:
        limit = default_limit
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        offset = 0
    return limit, offset


def _pg_session():
    try:
        return get_session(), None
    except RuntimeError as exc:
        return None, str(exc)


def _pg_unavailable(error: str):
    return jsonify({"available": False, "error": error, "items": []})


def _fixture_score(fixture: Fixture) -> dict:
    if not fixture.result or "-" not in fixture.result:
        return {"home": None, "away": None}
    home, away = fixture.result.split("-", 1)
    try:
        return {"home": int(home), "away": int(away)}
    except ValueError:
        return {"home": home, "away": away}


def _team_stats_payload(stats: list[TeamStat]) -> list[dict]:
    return [
        {
            "team_id": stat.team_id,
            "corners": stat.corners,
            "possession": stat.possession,
            "shots": stat.shots,
            "shots_on_target": None,
            "fouls": stat.fouls,
            "yellow_cards": None,
            "red_cards": None,
            "offsides": None,
            "passes": None,
            "xg": None,
        }
        for stat in stats
    ]


def _latest_odds_payload(session, fixture_id: int) -> list[dict]:
    rows = session.scalars(
        select(OddsSnapshot)
        .where(OddsSnapshot.fixture_id == fixture_id)
        .order_by(OddsSnapshot.timestamp.desc())
        .limit(20)
    ).all()
    return [
        {
            "bookmaker": row.bookmaker,
            "market": row.market,
            "home_odds": row.home_odds,
            "draw_odds": row.draw_odds,
            "away_odds": row.away_odds,
            "over_odds": row.over_odds,
            "under_odds": row.under_odds,
            "timestamp": _iso(row.timestamp),
        }
        for row in rows
    ]


def _prediction_payload(row: Prediction) -> dict:
    return {
        "id": row.id,
        "fixture_id": row.fixture_id,
        "market": row.market,
        "model_name": row.model_name,
        "league": row.league,
        "probability": row.probability,
        "implied_probability": row.implied_probability,
        "fair_odds": row.fair_odds,
        "real_odds": row.real_odds,
        "expected_value": row.expected_value,
        "edge": row.edge,
        "value_score": row.value_score,
        "confidence": row.confidence,
        "recommended": row.recommended,
        "pick_type": row.pick_type,
        "correct": row.correct,
        "profit": row.profit,
        "clv": row.clv,
        "predicted_at": _iso(row.predicted_at),
        "settled_at": _iso(row.settled_at),
    }


def _fixture_payload(session, fixture: Fixture, include_details: bool = False) -> dict:
    stats = list(session.scalars(select(TeamStat).where(TeamStat.fixture_id == fixture.id)).all())
    prediction_rows = session.scalars(select(Prediction).where(Prediction.fixture_id == fixture.id)).all()
    payload = {
        "id": fixture.id,
        "date": _iso(fixture.date),
        "league": fixture.league,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "result": fixture.result,
        "score": _fixture_score(fixture),
        "status": fixture.status,
        "has_stats": bool(stats),
        "has_predictions": bool(prediction_rows),
        "stats": _team_stats_payload(stats),
        "predictions": [_prediction_payload(row) for row in prediction_rows],
    }
    if include_details:
        payload["odds"] = _latest_odds_payload(session, fixture.id)
    return payload


def _result_emoji(result: str) -> str:
    if result == "ganada":
        return "✅"
    if result == "perdida":
        return "❌"
    return "⏳"


def _classify_bet(bet: dict) -> str:
    text = _normalize(f"{bet.get('market', '')} {bet.get('pick', '')}")
    if any(word in text for word in ("corner", "corners")):
        return "corners"
    if any(word in text for word in ("jugador", "remate", "faltas", "atajadas", "tarjeta")):
        return "jugadores"
    if any(word in text for word in ("resultado", "ganador", "empate", "winner", "1x2")):
        return "resultado"
    if any(word in text for word in ("gol", "goles", "over", "under", "btts", "ambos")):
        return "goles"
    return "resultado"


def _current_streak(resolved: list[dict]) -> str:
    ordered = sorted(
        resolved,
        key=lambda b: (_parse_dt(b.get("resolved_at") or b.get("created_at")), b.get("id") or 0),
        reverse=True,
    )
    if not ordered:
        return "Sin apuestas cerradas"

    streak_type = ordered[0].get("result")
    count = 0
    for bet in ordered:
        if bet.get("result") != streak_type:
            break
        count += 1

    label = "ganadas" if streak_type == "ganada" else "perdidas"
    return f"{count} {label}"


def _summary(bets: list[dict]) -> dict:
    resolved = [b for b in bets if b.get("result") in ("ganada", "perdida")]
    won = [b for b in resolved if b.get("result") == "ganada"]
    lost = [b for b in resolved if b.get("result") == "perdida"]
    pending = [b for b in bets if b.get("result") is None]
    total_profit = round(sum(_money(b.get("profit")) for b in resolved), 2)
    decided = len(won) + len(lost)

    return {
        "total_profit": total_profit,
        "win_rate": round(len(won) / decided * 100, 1) if decided else 0.0,
        "total": len(bets),
        "won": len(won),
        "lost": len(lost),
        "pending": len(pending),
        "streak": _current_streak(resolved),
    }


def _bankroll_series(bets: list[dict]) -> dict:
    bankroll = 0.0
    labels = []
    values = []
    resolved = [b for b in bets if b.get("result") in ("ganada", "perdida")]
    resolved.sort(key=lambda b: (_parse_dt(b.get("resolved_at") or b.get("created_at")), b.get("id") or 0))

    for bet in resolved:
        bankroll = round(bankroll + _money(bet.get("profit")), 2)
        dt = _parse_dt(bet.get("resolved_at") or bet.get("created_at"))
        labels.append(dt.strftime("%d/%m"))
        values.append(bankroll)

    if not labels:
        labels = ["Sin apuestas"]
        values = [0]

    return {"labels": labels, "values": values}


def _category_stats(bets: list[dict]) -> dict:
    stats = {
        "goles": {"label": "Goles", "won": 0, "lost": 0, "rate": 0.0},
        "corners": {"label": "Corners", "won": 0, "lost": 0, "rate": 0.0},
        "jugadores": {"label": "Jugadores", "won": 0, "lost": 0, "rate": 0.0},
        "resultado": {"label": "Resultado", "won": 0, "lost": 0, "rate": 0.0},
    }

    for bet in bets:
        if bet.get("result") not in ("ganada", "perdida"):
            continue
        category = _classify_bet(bet)
        bucket = stats[category]
        if bet.get("result") == "ganada":
            bucket["won"] += 1
        else:
            bucket["lost"] += 1

    for bucket in stats.values():
        total = bucket["won"] + bucket["lost"]
        bucket["rate"] = round(bucket["won"] / total * 100, 1) if total else 0.0
        bucket["total"] = total

    return stats


def _display_bets(bets: list[dict]) -> list[dict]:
    ordered = sorted(bets, key=lambda b: (_parse_dt(b.get("created_at")), b.get("id") or 0), reverse=True)
    display = []
    for bet in ordered:
        row = dict(bet)
        dt = _parse_dt(row.get("created_at"))
        row["date_label"] = dt.strftime("%d/%m/%Y %H:%M") if dt != datetime.min else "N/D"
        row["match_label"] = row.get("match_name") or row.get("league") or "Apuesta"
        row["result_label"] = _result_emoji(row.get("result"))
        row["profit_value"] = _money(row.get("profit")) if row.get("result") else None
        display.append(row)
    return display


def _load_bets() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM bets ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _load_performance_dashboard() -> dict:
    return {
        "stats": get_postgres_stats(),
        "performance_rows": get_model_performance_history(limit=50),
        "computed_performance": get_model_performance(persist=False),
        "recent_predictions": get_recent_predictions(limit=50),
        "value_bet_performance": get_value_bet_performance(),
        "value_bets": get_value_bets(limit=50),
        "matches": _dashboard_matches(),
    }


def _dashboard_matches() -> dict:
    session, error = _pg_session()
    if session is None:
        return {"today": [], "upcoming": [], "finished": [], "error": error}
    today = datetime.utcnow().date()
    with session:
        all_rows = session.scalars(select(Fixture).order_by(Fixture.date.desc()).limit(300)).all()
        today_rows = [row for row in all_rows if row.date and row.date.date() == today]
        upcoming = [row for row in sorted(all_rows, key=lambda item: _sort_timestamp(item.date, float("inf"))) if row.status in UPCOMING_STATUSES][:12]
        finished = [row for row in all_rows if row.status in FINISHED_STATUSES][:12]
        return {
            "today": [_fixture_payload(session, row) for row in today_rows[:12]],
            "upcoming": [_fixture_payload(session, row) for row in upcoming],
            "finished": [_fixture_payload(session, row) for row in finished],
            "error": None,
        }


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/api/dashboard/summary")
def api_dashboard_summary():
    stats = get_postgres_stats()
    matches = _dashboard_matches()
    value_perf = get_value_bet_performance()
    return jsonify({
        "postgres": stats,
        "matches": {
            "today": len(matches["today"]),
            "upcoming": len(matches["upcoming"]),
            "finished": len(matches["finished"]),
        },
        "value_bets": value_perf,
        "performance": get_model_performance(persist=False),
    })


@app.get("/api/stats/overview")
def api_stats_overview():
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    with session:
        leagues = session.scalars(select(distinct(Fixture.league)).where(Fixture.league.is_not(None))).all()
        teams = session.scalar(select(func.count(Team.id))) or 0
        players = session.scalar(select(func.count(Player.id))) or 0
        team_stats = session.scalar(select(func.count(TeamStat.id))) or 0
        player_stats = session.scalar(select(func.count(PlayerStat.id))) or 0
        odds = session.scalar(select(func.count(OddsSnapshot.id))) or 0
        injuries = session.scalar(select(func.count(InjuryReport.id))) or 0
        return jsonify({
            "available": True,
            "leagues": [league for league in leagues if league],
            "teams": int(teams),
            "players": int(players),
            "team_stats": int(team_stats),
            "player_stats": int(player_stats),
            "odds_snapshots": int(odds),
            "injury_reports": int(injuries),
            **get_postgres_stats(),
        })


@app.get("/api/matches")
def api_matches():
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    limit, offset = _pagination()
    with session:
        query = select(Fixture)
        if request.args.get("date"):
            query = query.where(func.date(Fixture.date) == request.args["date"])
        if request.args.get("league"):
            query = query.where(Fixture.league == request.args["league"])
        if request.args.get("status"):
            query = query.where(Fixture.status == request.args["status"])
        if request.args.get("team"):
            term = f"%{request.args['team']}%"
            query = query.where(or_(Fixture.home_team.ilike(term), Fixture.away_team.ilike(term)))
        query = query.order_by(Fixture.date.desc())
        rows = session.scalars(query.offset(offset).limit(limit)).all()
        items = [_fixture_payload(session, row) for row in rows]
        market = request.args.get("market")
        if market:
            items = [item for item in items if any(pred.get("market") == market for pred in item["predictions"])]
        has_prediction = _bool_arg("has_prediction")
        if has_prediction is not None:
            items = [item for item in items if item["has_predictions"] is has_prediction]
        return jsonify({"available": True, "limit": limit, "offset": offset, "items": items})


@app.get("/api/matches/<int:fixture_id>")
def api_match_detail(fixture_id: int):
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    with session:
        fixture = session.get(Fixture, fixture_id)
        if not fixture:
            return jsonify({"available": True, "error": "match not found"}), 404
        return jsonify({"available": True, "match": _fixture_payload(session, fixture, include_details=True)})


@app.get("/api/players/search")
def api_players_search():
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    q = request.args.get("q", "").strip()
    limit, offset = _pagination(default_limit=20)
    with session:
        query = select(Player)
        if q:
            query = query.where(Player.name.ilike(f"%{q}%"))
        rows = session.scalars(query.order_by(Player.name.asc()).offset(offset).limit(limit)).all()
        items = [
            {
                "id": row.id,
                "name": row.name,
                "team_id": row.team_id,
                "position": row.position,
            }
            for row in rows
        ]
        return jsonify({"available": True, "limit": limit, "offset": offset, "items": items})


def _latest_player_stat(session, player_id: int) -> PlayerStat | None:
    return session.scalar(
        select(PlayerStat)
        .join(Fixture, Fixture.id == PlayerStat.fixture_id)
        .where(PlayerStat.player_id == player_id)
        .order_by(Fixture.date.desc())
        .limit(1)
    )


@app.get("/api/players/<int:player_id>")
def api_player_detail(player_id: int):
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    with session:
        player = session.get(Player, player_id)
        if not player:
            return jsonify({"available": True, "error": "player not found"}), 404
        team = session.get(Team, player.team_id) if player.team_id else None
        latest = _latest_player_stat(session, player_id)
        injuries = session.scalars(select(InjuryReport).where(InjuryReport.player_id == player_id).order_by(InjuryReport.reported_at.desc())).all()
        return jsonify({
            "available": True,
            "player": {
                "id": player.id,
                "name": player.name,
                "age": None,
                "nationality": player.nationality,
                "team": team.name if team else "No disponible",
                "team_id": player.team_id,
                "position": player.position,
                "height": None,
                "weight": None,
                "photo": None,
                "league": team.league if team else None,
                "season": None,
                "matches_played": None,
                "minutes": None,
                "goals": latest.goals if latest else None,
                "assists": latest.assists if latest else None,
                "shots": latest.shots if latest else None,
                "shots_on_target": latest.shots_on_target if latest else None,
                "passes": None,
                "key_passes": None,
                "fouls_committed": latest.fouls_committed if latest else None,
                "fouls_drawn": latest.fouls_drawn if latest else None,
                "yellow_cards": None,
                "red_cards": None,
                "corners_generated": None,
                "offsides": None,
                "rating": latest.rating if latest else None,
                "injuries": [
                    {
                        "fixture_id": row.fixture_id,
                        "reason": row.reason,
                        "status": row.status,
                        "reported_at": _iso(row.reported_at),
                    }
                    for row in injuries
                ],
                "team_history": [],
            },
        })


@app.get("/api/players/<int:player_id>/last-matches")
def api_player_last_matches(player_id: int):
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    try:
        limit = min(max(int(request.args.get("limit", 7)), 1), 20)
    except ValueError:
        limit = 7
    with session:
        player = session.get(Player, player_id)
        team = session.get(Team, player.team_id) if player and player.team_id else None
        rows = session.execute(
            select(PlayerStat, Fixture)
            .join(Fixture, Fixture.id == PlayerStat.fixture_id)
            .where(PlayerStat.player_id == player_id)
            .order_by(Fixture.date.desc())
            .limit(limit)
        ).all()
        items = []
        for stat, fixture in rows:
            if team and fixture.home_team == team.name:
                rival = fixture.away_team
                home_away = "Local"
            elif team and fixture.away_team == team.name:
                rival = fixture.home_team
                home_away = "Visitante"
            else:
                rival = "No disponible"
                home_away = "No disponible"
            items.append({
                "date": _iso(fixture.date),
                "rival": rival,
                "competition": fixture.league,
                "home_away": home_away,
                "minutes": None,
                "goals": stat.goals,
                "assists": stat.assists,
                "shots": stat.shots,
                "shots_on_target": stat.shots_on_target,
                "passes": None,
                "fouls": stat.fouls_committed,
                "fouls_drawn": stat.fouls_drawn,
                "cards": None,
                "rating": stat.rating,
                "result": fixture.result,
            })
        def avg(key):
            values = [item[key] for item in items if item.get(key) is not None]
            return round(sum(values) / len(values), 2) if values else None
        first_half = items[: max(1, len(items) // 2)]
        second_half = items[max(1, len(items) // 2):]
        recent_shots = avg("shots") or 0
        older_values = [item["shots"] for item in second_half if item.get("shots") is not None]
        older_shots = sum(older_values) / len(older_values) if older_values else recent_shots
        trend = "estable"
        if recent_shots > older_shots + 0.5:
            trend = "subiendo"
        elif recent_shots < older_shots - 0.5:
            trend = "bajando"
        return jsonify({
            "available": True,
            "items": items,
            "summary": {
                "avg_shots": avg("shots"),
                "avg_shots_on_target": avg("shots_on_target"),
                "avg_fouls": avg("fouls"),
                "avg_fouls_drawn": avg("fouls_drawn"),
                "avg_minutes": avg("minutes"),
                "goals_last_7": sum(item.get("goals") or 0 for item in items),
                "assists_last_7": sum(item.get("assists") or 0 for item in items),
                "trend": trend,
            },
        })


@app.get("/api/teams/search")
def api_teams_search():
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    q = request.args.get("q", "").strip()
    limit, offset = _pagination(default_limit=20)
    with session:
        query = select(Team)
        if q:
            query = query.where(Team.name.ilike(f"%{q}%"))
        rows = session.scalars(query.order_by(Team.name.asc()).offset(offset).limit(limit)).all()
        return jsonify({
            "available": True,
            "items": [{"id": row.id, "name": row.name, "league": row.league} for row in rows],
        })


@app.get("/api/teams/<int:team_id>")
def api_team_detail(team_id: int):
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    with session:
        team = session.get(Team, team_id)
        if not team:
            return jsonify({"available": True, "error": "team not found"}), 404
        players = session.scalars(select(Player).where(Player.team_id == team_id).order_by(Player.name.asc())).all()
        stats = session.scalars(select(TeamStat).where(TeamStat.team_id == team_id).limit(20)).all()
        fixtures = session.scalars(
            select(Fixture)
            .where(or_(Fixture.home_team == team.name, Fixture.away_team == team.name))
            .order_by(Fixture.date.desc())
            .limit(20)
        ).all()
        predictions = session.scalars(
            select(Prediction)
            .join(Fixture, Fixture.id == Prediction.fixture_id)
            .where(or_(Fixture.home_team == team.name, Fixture.away_team == team.name))
            .order_by(Prediction.predicted_at.desc())
            .limit(20)
        ).all()
        return jsonify({
            "available": True,
            "team": {
                "id": team.id,
                "name": team.name,
                "country": None,
                "league": team.league,
                "crest": None,
                "squad": [
                    {
                        "id": player.id,
                        "name": player.name,
                        "position": player.position,
                        "nationality": player.nationality,
                    }
                    for player in players
                ],
                "upcoming_matches": [_fixture_payload(session, row) for row in fixtures if row.status in UPCOMING_STATUSES][:5],
                "last_matches": [_fixture_payload(session, row) for row in fixtures if row.status in FINISHED_STATUSES][:5],
                "stats": {
                    "goals_for": None,
                    "goals_against": None,
                    "corners_for": round(sum(row.corners or 0 for row in stats) / len(stats), 2) if stats else None,
                    "corners_against": None,
                    "shots": round(sum(row.shots or 0 for row in stats) / len(stats), 2) if stats else None,
                    "shots_against": None,
                    "cards": None,
                    "recent_form": None,
                },
                "predictions": [_prediction_payload(row) for row in predictions],
            },
        })


@app.get("/api/performance")
def api_performance():
    return jsonify({
        "stats": get_postgres_stats(),
        "computed": get_model_performance(persist=False),
        "history": get_model_performance_history(limit=50),
    })


@app.get("/api/predictions")
def api_predictions():
    session, error = _pg_session()
    if session is None:
        return _pg_unavailable(error)
    limit, offset = _pagination()
    with session:
        query = select(Prediction)
        if request.args.get("league"):
            query = query.where(Prediction.league == request.args["league"])
        if request.args.get("market"):
            query = query.where(Prediction.market == request.args["market"])
        if request.args.get("model"):
            query = query.where(Prediction.model_name == request.args["model"])
        if request.args.get("date"):
            query = query.where(func.date(Prediction.predicted_at) == request.args["date"])
        if request.args.get("min_confidence"):
            try:
                query = query.where(Prediction.confidence >= float(request.args["min_confidence"]))
            except ValueError:
                pass
        value_positive = _bool_arg("value_positive")
        if value_positive is not None:
            query = query.where(Prediction.value_score > 0 if value_positive else or_(Prediction.value_score <= 0, Prediction.value_score.is_(None)))
        correct = _bool_arg("correct")
        if correct is not None:
            query = query.where(Prediction.correct.is_(correct))
        rows = session.scalars(query.order_by(Prediction.predicted_at.desc()).offset(offset).limit(limit)).all()
        return jsonify({"available": True, "limit": limit, "offset": offset, "predictions": [_prediction_payload(row) for row in rows]})


@app.get("/api/value-bets")
def api_value_bets():
    session, error = _pg_session()
    if session is None:
        return jsonify({"performance": get_value_bet_performance(), "picks": [], "available": False, "error": error})
    limit, offset = _pagination()
    with session:
        query = select(Prediction).where(Prediction.value_score > 0)
        if request.args.get("league"):
            query = query.where(Prediction.league == request.args["league"])
        if request.args.get("market"):
            query = query.where(Prediction.market == request.args["market"])
        if request.args.get("model"):
            query = query.where(Prediction.model_name == request.args["model"])
        rows = session.scalars(query.order_by(Prediction.predicted_at.desc()).offset(offset).limit(limit)).all()
        return jsonify({
            "available": True,
            "performance": get_value_bet_performance(),
            "limit": limit,
            "offset": offset,
            "picks": [_prediction_payload(row) for row in rows],
        })


@app.route("/")
@app.route("/dashboard")
def dashboard():
    bets = _load_bets()
    bankroll = _bankroll_series(bets)
    performance = _load_performance_dashboard()
    return render_template(
        "dashboard.html",
        summary=_summary(bets),
        bets=_display_bets(bets),
        category_stats=_category_stats(bets),
        postgres_stats=performance["stats"],
        model_performance=performance["performance_rows"],
        computed_model_performance=performance["computed_performance"].get("models", []),
        recent_predictions=performance["recent_predictions"],
        value_bet_performance=performance["value_bet_performance"],
        value_bets=performance["value_bets"],
        dashboard_matches=performance["matches"],
        chart_labels=json.dumps(bankroll["labels"]),
        chart_values=json.dumps(bankroll["values"]),
    )


if __name__ == "__main__":
    initialize_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
