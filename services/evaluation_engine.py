import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import distinct, func, select

from db.connection import get_session
from db.models import Fixture, ModelPerformance, Player, PlayerStat, Prediction, Team, TeamStat


def _safe_probability(value: float | None) -> float:
    if value is None:
        return 0.5
    return min(max(float(value), 1e-15), 1 - 1e-15)


def _prediction_rows(model_name: str | None = None, league: str | None = None) -> list[Prediction]:
    session = get_session()
    with session:
        query = select(Prediction).where(Prediction.correct.is_not(None))
        if model_name:
            query = query.where(Prediction.model_name == model_name)
        if league:
            query = query.where(Prediction.league == league)
        return list(session.scalars(query).all())


def _serialize_prediction(prediction: Prediction) -> dict[str, Any]:
    return {
        "id": prediction.id,
        "fixture_id": prediction.fixture_id,
        "model_name": prediction.model_name,
        "league": prediction.league,
        "market": prediction.market,
        "probability": prediction.probability,
        "implied_probability": prediction.implied_probability,
        "real_odds": prediction.real_odds,
        "expected_value": prediction.expected_value,
        "edge": prediction.edge,
        "value_score": prediction.value_score,
        "confidence": prediction.confidence,
        "recommended": prediction.recommended,
        "pick_type": prediction.pick_type,
        "correct": prediction.correct,
        "profit": prediction.profit,
        "closing_odds": prediction.closing_odds,
        "clv": prediction.clv,
        "predicted_at": prediction.predicted_at.isoformat() if prediction.predicted_at else None,
        "settled_at": prediction.settled_at.isoformat() if prediction.settled_at else None,
    }


def _serialize_performance(row: ModelPerformance) -> dict[str, Any]:
    return {
        "id": row.id,
        "model_name": row.model_name,
        "market": row.market,
        "league": row.league,
        "hit_rate": row.hit_rate,
        "roi": row.roi,
        "yield_rate": row.yield_rate,
        "brier_score": row.brier_score,
        "log_loss": row.log_loss,
        "sample_size": row.sample_size,
        "total_picks": row.total_picks,
        "best_league": row.best_league,
        "best_market": row.best_market,
        "last_updated": row.last_updated.isoformat() if row.last_updated else None,
    }


def _metrics(rows: list[Prediction]) -> dict[str, Any]:
    total_picks = len(rows)
    if not total_picks:
        return {
            "hit_rate": 0.0,
            "roi": 0.0,
            "yield_rate": 0.0,
            "brier_score": 0.0,
            "log_loss": 0.0,
            "total_picks": 0,
        }

    hits = sum(1 for row in rows if row.correct is True)
    roi = round(sum(float(row.profit or 0.0) for row in rows), 4)
    brier_score = round(
        sum((_safe_probability(row.probability) - (1 if row.correct else 0)) ** 2 for row in rows) / total_picks,
        4,
    )
    log_loss = round(
        -sum(
            (1 if row.correct else 0) * math.log(_safe_probability(row.probability))
            + (0 if row.correct else 1) * math.log(1 - _safe_probability(row.probability))
            for row in rows
        )
        / total_picks,
        4,
    )
    return {
        "hit_rate": round(hits / total_picks, 4),
        "roi": roi,
        "yield_rate": round(roi / total_picks, 4),
        "brier_score": brier_score,
        "log_loss": log_loss,
        "total_picks": total_picks,
    }


def _best_bucket(rows: list[Prediction], attr: str) -> str | None:
    grouped: dict[str, list[Prediction]] = defaultdict(list)
    for row in rows:
        value = getattr(row, attr, None)
        if value:
            grouped[str(value)].append(row)
    if not grouped:
        return None
    return max(grouped.items(), key=lambda item: (_metrics(item[1])["hit_rate"], len(item[1])))[0]


def _market_metrics(rows: list[Prediction]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Prediction]] = defaultdict(list)
    for row in rows:
        grouped[row.market or "unknown"].append(row)
    return [
        {"market": market, **_metrics(market_rows)}
        for market, market_rows in sorted(grouped.items())
    ]


def _league_metrics(rows: list[Prediction]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Prediction]] = defaultdict(list)
    for row in rows:
        grouped[row.league or "N/D"].append(row)
    return [
        {"league": league, **_metrics(league_rows)}
        for league, league_rows in sorted(grouped.items())
    ]


def _save_model_performance(
    model_name: str,
    market: str,
    league: str | None,
    metrics: dict[str, Any],
    best_league: str | None,
    best_market: str | None,
) -> None:
    session = get_session()
    with session:
        session.add(
            ModelPerformance(
                model_name=model_name,
                market=market,
                league=league or "ALL",
                hit_rate=metrics["hit_rate"],
                roi=metrics["roi"],
                yield_rate=metrics["yield_rate"],
                brier_score=metrics["brier_score"],
                log_loss=metrics["log_loss"],
                sample_size=metrics["total_picks"],
                total_picks=metrics["total_picks"],
                best_league=best_league,
                best_market=best_market,
                last_updated=datetime.now(timezone.utc),
            )
        )
        session.commit()


def get_model_performance(
    model_name: str | None = None,
    league: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Evaluate settled predictions and persist model-level performance snapshots."""
    try:
        rows = _prediction_rows(model_name=model_name, league=league)
    except RuntimeError as exc:
        return {"available": False, "error": str(exc), "models": []}

    grouped: dict[str, list[Prediction]] = defaultdict(list)
    for row in rows:
        grouped[row.model_name or "unknown"].append(row)

    models = []
    for name, model_rows in sorted(grouped.items()):
        metrics = _metrics(model_rows)
        best_league = _best_bucket(model_rows, "league")
        best_market = _best_bucket(model_rows, "market")
        model_result = {
            "model_name": name,
            "league": league or "ALL",
            **metrics,
            "best_league": best_league,
            "best_market": best_market,
            "markets": _market_metrics(model_rows),
            "leagues": _league_metrics(model_rows),
        }
        models.append(model_result)
        if persist:
            try:
                _save_model_performance(name, "ALL", league, metrics, best_league, best_market)
            except RuntimeError:
                logging.info("[evaluation_engine] DATABASE_URL no configurado; no se guardan métricas.")
            except Exception:
                logging.exception("[evaluation_engine] no se pudo guardar performance de %s", name)

    return {
        "available": True,
        "model_name": model_name,
        "league": league,
        "models": models,
        "total_predictions": len(rows),
    }


def get_value_bet_performance() -> dict[str, Any]:
    try:
        rows = _prediction_rows()
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)}

    value_bets = [row for row in rows if (row.value_score or 0) > 0]
    normal_picks = [row for row in rows if not ((row.value_score or 0) > 0)]
    value_metrics = _metrics(value_bets)
    normal_metrics = _metrics(normal_picks)
    detects_real_value = value_metrics["hit_rate"] > normal_metrics["hit_rate"]
    return {
        "available": True,
        "value_bets": value_metrics,
        "normal_picks": normal_metrics,
        "detects_real_value": detects_real_value,
        "hit_rate_delta": round(value_metrics["hit_rate"] - normal_metrics["hit_rate"], 4),
    }


def get_clv_analysis() -> dict[str, Any]:
    try:
        rows = [row for row in _prediction_rows() if row.clv is not None]
    except RuntimeError as exc:
        return {"available": False, "error": str(exc)}

    grouped: dict[str, list[Prediction]] = defaultdict(list)
    for row in rows:
        grouped[row.market or "unknown"].append(row)

    markets = []
    for market, market_rows in sorted(grouped.items()):
        avg_clv = round(sum(float(row.clv or 0.0) for row in market_rows) / len(market_rows), 4)
        logging.info("[evaluation_engine] CLV promedio market=%s avg_clv=%s picks=%s", market, avg_clv, len(market_rows))
        markets.append({
            "market": market,
            "avg_clv": avg_clv,
            "total_picks": len(market_rows),
            "positive_clv_rate": round(sum(1 for row in market_rows if (row.clv or 0) > 0) / len(market_rows), 4),
        })

    avg_clv_all = round(sum(float(row.clv or 0.0) for row in rows) / len(rows), 4) if rows else 0.0
    return {
        "available": True,
        "avg_clv": avg_clv_all,
        "markets": markets,
        "total_picks": len(rows),
    }


def get_recent_predictions(limit: int = 50) -> list[dict[str, Any]]:
    try:
        session = get_session()
    except RuntimeError:
        return []
    with session:
        rows = session.scalars(
            select(Prediction)
            .order_by(Prediction.predicted_at.desc())
            .limit(limit)
        ).all()
        return [_serialize_prediction(row) for row in rows]


def get_value_bets(limit: int = 50) -> list[dict[str, Any]]:
    try:
        session = get_session()
    except RuntimeError:
        return []
    with session:
        rows = session.scalars(
            select(Prediction)
            .where(Prediction.value_score > 0)
            .order_by(Prediction.predicted_at.desc())
            .limit(limit)
        ).all()
        return [_serialize_prediction(row) for row in rows]


def get_postgres_stats() -> dict[str, Any]:
    try:
        session = get_session()
    except RuntimeError as exc:
        return {
            "available": False,
            "error": str(exc),
            "fixtures": 0,
            "fixtures_with_stats": 0,
            "teams": 0,
            "players": 0,
            "player_stats": 0,
            "predictions_total": 0,
            "predictions_settled": 0,
        }

    with session:
        fixtures = session.scalar(select(func.count(Fixture.id))) or 0
        fixtures_with_stats = session.scalar(select(func.count(distinct(TeamStat.fixture_id)))) or 0
        teams = session.scalar(select(func.count(Team.id))) or 0
        players = session.scalar(select(func.count(Player.id))) or 0
        player_stats = session.scalar(select(func.count(PlayerStat.id))) or 0
        predictions_total = session.scalar(select(func.count(Prediction.id))) or 0
        predictions_settled = session.scalar(
            select(func.count(Prediction.id)).where(Prediction.correct.is_not(None))
        ) or 0
        return {
            "available": True,
            "fixtures": int(fixtures),
            "fixtures_with_stats": int(fixtures_with_stats),
            "teams": int(teams),
            "players": int(players),
            "player_stats": int(player_stats),
            "predictions_total": int(predictions_total),
            "predictions_settled": int(predictions_settled),
        }


def get_model_performance_history(limit: int = 50) -> list[dict[str, Any]]:
    try:
        session = get_session()
    except RuntimeError:
        return []
    with session:
        rows = session.scalars(
            select(ModelPerformance)
            .order_by(ModelPerformance.last_updated.desc())
            .limit(limit)
        ).all()
        return [_serialize_performance(row) for row in rows]


def run_daily_evaluation() -> dict[str, Any]:
    performance = get_model_performance()
    value_bets = get_value_bet_performance()
    clv = get_clv_analysis()
    logging.info(
        "[evaluation_engine] daily evaluation: models=%s value_delta=%s avg_clv=%s",
        len(performance.get("models", [])),
        value_bets.get("hit_rate_delta"),
        clv.get("avg_clv"),
    )
    return {
        "performance": performance,
        "value_bets": value_bets,
        "clv": clv,
    }
