import logging
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.connection import get_session
from db.models import Fixture, ModelPerformance, OddsSnapshot, Prediction, Team, TeamStat
from services.value_bets import analyze_value


MODEL_NAME = "corners_logistic_regression_v1"
MARKET = "corners_over_9_5"
CORNER_LINE = 9.5
MIN_TRAINING_ROWS = 20
MIN_NEW_MATCHES_FOR_RETRAIN = 50
TEST_FRACTION = 0.2  # last 20% of fixtures (chronological) used for evaluation
MODEL_CACHE_TTL = timedelta(hours=24)
MODEL_PATH = Path(os.getenv("CORNERS_MODEL_PATH", "/data/corners_model.pkl"))

# In-memory cache for the trained production model so predict() does not retrain
# the whole logistic regression on every call. Guarded by a lock because
# predictions can be requested from several scheduler/bot threads at once.
_MODEL_CACHE: dict[str, Any] = {
    "model": None,
    "feature_columns": [],
    "trained_at": None,
    "row_count": -1,
}
_MODEL_CACHE_LOCK = threading.Lock()
FEATURE_COLUMNS = [
    "home_corners_avg_5",
    "away_corners_avg_5",
    "home_possession_avg_5",
    "away_possession_avg_5",
    "home_shots_avg_5",
    "away_shots_avg_5",
    "home_is_home",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_model_bundle() -> dict[str, Any] | None:
    if not MODEL_PATH.exists():
        return None
    try:
        joblib = __import__("joblib")
        bundle = joblib.load(MODEL_PATH)
        if not bundle.get("model") or not bundle.get("feature_columns"):
            logging.warning("[corners_model] modelo en disco invÃ¡lido: %s", MODEL_PATH)
            return None
        return bundle
    except Exception:
        logging.exception("[corners_model] no se pudo cargar el modelo desde %s", MODEL_PATH)
        return None


def _save_model_bundle(bundle: dict[str, Any]) -> None:
    joblib = __import__("joblib")
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, MODEL_PATH)


def _cache_model_bundle(bundle: dict[str, Any]) -> None:
    _MODEL_CACHE.update({
        "model": bundle["model"],
        "feature_columns": list(bundle["feature_columns"]),
        "trained_at": bundle.get("trained_at") or _utcnow(),
        "row_count": int(bundle.get("row_count") or 0),
    })


def _avg(values: list[float]) -> float | None:
    values = [float(v) for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def _team_id_by_name(session, name: str | None) -> int | None:
    if not name:
        return None
    row = session.scalar(select(Team).where(Team.name == name).limit(1))
    return row.id if row else None


def _fixture_team_stats(session, fixture_id: int) -> list[TeamStat]:
    return list(session.scalars(select(TeamStat).where(TeamStat.fixture_id == fixture_id)).all())


def _stat_for_team(stats: list[TeamStat], team_id: int | None) -> TeamStat | None:
    if team_id is None:
        return None
    for stat in stats:
        if stat.team_id == team_id:
            return stat
    return None


def _recent_team_stats(session, team_id: int, before_date: datetime | None, limit: int = 5) -> list[TeamStat]:
    if team_id is None:
        return []
    query = (
        select(TeamStat)
        .join(Fixture, Fixture.id == TeamStat.fixture_id)
        .where(TeamStat.team_id == team_id)
        .order_by(Fixture.date.desc())
        .limit(limit)
    )
    if before_date is not None:
        query = query.where(Fixture.date < before_date)
    return list(session.scalars(query).all())


def _feature_values_for_fixture(session, fixture: Fixture) -> dict[str, Any] | None:
    home_id = _team_id_by_name(session, fixture.home_team)
    away_id = _team_id_by_name(session, fixture.away_team)
    if home_id is None or away_id is None:
        return None

    home_recent = _recent_team_stats(session, home_id, fixture.date)
    away_recent = _recent_team_stats(session, away_id, fixture.date)
    if len(home_recent) < 2 or len(away_recent) < 2:
        return None

    return {
        "fixture_id": fixture.id,
        "league": fixture.league or "N/D",
        "home_corners_avg_5": _avg([s.corners for s in home_recent]),
        "away_corners_avg_5": _avg([s.corners for s in away_recent]),
        "home_possession_avg_5": _avg([s.possession for s in home_recent]),
        "away_possession_avg_5": _avg([s.possession for s in away_recent]),
        "home_shots_avg_5": _avg([s.shots for s in home_recent]),
        "away_shots_avg_5": _avg([s.shots for s in away_recent]),
        "home_is_home": 1,
    }


def _latest_corner_odds(session, fixture_id: int) -> tuple[float | None, float | None]:
    snapshot = session.scalar(
        select(OddsSnapshot)
        .where(
            OddsSnapshot.fixture_id == fixture_id,
            OddsSnapshot.market.in_(("corners_9_5", "corners", "corners_total_9_5")),
        )
        .order_by(OddsSnapshot.timestamp.desc())
        .limit(1)
    )
    if not snapshot:
        return None, None
    return snapshot.over_odds, snapshot.under_odds


def _prepare_matrix(dataset, feature_cols: list[str] | None = None):
    pd = __import__("pandas")
    df = dataset.copy()
    if "league" in df.columns:
        df = pd.get_dummies(df, columns=["league"], dummy_na=False)
    if feature_cols is None:
        feature_cols = [c for c in df.columns if c in FEATURE_COLUMNS or c.startswith("league_")]
    else:
        for col in feature_cols:
            if col not in df.columns:
                df[col] = 0
    return df, feature_cols


def _fit_model(dataset):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    if dataset.empty or len(dataset) < MIN_TRAINING_ROWS:
        return {
            "model": None,
            "feature_columns": [],
            "error": f"histÃ³rico insuficiente ({len(dataset)} filas)",
        }
    if dataset["target_over_9_5"].nunique() < 2:
        return {
            "model": None,
            "feature_columns": [],
            "error": "histÃ³rico con una sola clase para corners",
        }

    matrix, feature_cols = _prepare_matrix(dataset)
    X = matrix[feature_cols].fillna(0)
    y = matrix["target_over_9_5"]
    model = Pipeline(
        steps=[
            ("scaler", StandardScaler()),
            ("logreg", LogisticRegression(max_iter=1000, random_state=42)),
        ]
    )
    model.fit(X, y)
    return {"model": model, "feature_columns": feature_cols, "error": None}


def build_dataset():
    """Build the corners dataset from PostgreSQL team_stats collector data."""
    pd = __import__("pandas")
    try:
        session = get_session()
    except RuntimeError:
        logging.info("[corners_model] DATABASE_URL no configurado; dataset vacío.")
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    with session:
        fixtures = session.scalars(
            select(Fixture)
            .where(Fixture.status.in_(("FT", "AET", "PEN")))
            .order_by(Fixture.date.asc())
        ).all()

        for fixture in fixtures:
            stats = _fixture_team_stats(session, fixture.id)
            if len(stats) < 2:
                continue

            home_id = _team_id_by_name(session, fixture.home_team)
            away_id = _team_id_by_name(session, fixture.away_team)
            home_stat = _stat_for_team(stats, home_id)
            away_stat = _stat_for_team(stats, away_id)
            if not home_stat or not away_stat:
                continue
            if home_stat.corners is None or away_stat.corners is None:
                continue

            features = _feature_values_for_fixture(session, fixture)
            if not features or any(features.get(col) is None for col in FEATURE_COLUMNS):
                continue

            total_corners = int(home_stat.corners or 0) + int(away_stat.corners or 0)
            row = dict(features)
            row.update({
                "total_corners": total_corners,
                "target_over_9_5": 1 if total_corners > CORNER_LINE else 0,
            })
            rows.append(row)

    return pd.DataFrame(rows)


def train_model():
    """Train a logistic regression model for Over 9.5 corners."""
    dataset = build_dataset()
    trained = _fit_model(dataset)
    trained["dataset"] = dataset
    trained["trained_at"] = _utcnow()
    trained["row_count"] = len(dataset)
    return trained


def _get_cached_trained_model() -> dict[str, Any]:
    now = _utcnow()
    with _MODEL_CACHE_LOCK:
        trained_at = _MODEL_CACHE.get("trained_at")
        if _MODEL_CACHE.get("model") is not None and trained_at and now - trained_at < MODEL_CACHE_TTL:
            return {
                "model": _MODEL_CACHE["model"],
                "feature_columns": list(_MODEL_CACHE["feature_columns"]),
                "error": None,
            }

        bundle = _load_model_bundle()
        if bundle is not None:
            _cache_model_bundle(bundle)
            return {
                "model": bundle["model"],
                "feature_columns": list(bundle["feature_columns"]),
                "error": None,
            }

        trained = train_model()
        if not trained.get("error"):
            bundle = {
                "model": trained["model"],
                "feature_columns": list(trained["feature_columns"]),
                "trained_at": trained["trained_at"],
                "row_count": trained["row_count"],
                "model_name": MODEL_NAME,
                "market": MARKET,
            }
            try:
                _save_model_bundle(bundle)
            except Exception:
                logging.exception("[corners_model] no se pudo guardar el modelo inicial en %s", MODEL_PATH)
            _cache_model_bundle(bundle)
        return trained


def _prediction_features(session, fixture_id: int, feature_cols: list[str]):
    pd = __import__("pandas")
    fixture = session.get(Fixture, fixture_id)
    if not fixture:
        return None
    features = _feature_values_for_fixture(session, fixture)
    if not features:
        return None
    row = pd.DataFrame([features])
    row = pd.get_dummies(row, columns=["league"], dummy_na=False)
    for col in feature_cols:
        if col not in row.columns:
            row[col] = 0
    return row[feature_cols].fillna(0)


def _confidence(probability_over: float) -> int:
    distance = abs(probability_over - 0.5)
    return int(round(min(100, 50 + distance * 100)))


def predict(fixture_id: int) -> dict[str, Any]:
    """Return Over/Under 9.5 corners probabilities and value information."""
    try:
        trained = _get_cached_trained_model()
    except Exception as exc:
        logging.exception("[corners_model] error entrenando modelo")
        return {"available": False, "fixture_id": fixture_id, "error": str(exc)}

    if trained.get("error"):
        return {"available": False, "fixture_id": fixture_id, "error": trained["error"]}

    try:
        session = get_session()
    except RuntimeError:
        return {"available": False, "fixture_id": fixture_id, "error": "DATABASE_URL no configurado"}

    with session:
        X = _prediction_features(session, fixture_id, trained["feature_columns"])
        if X is None:
            return {"available": False, "fixture_id": fixture_id, "error": "features insuficientes"}

        probability_over = float(trained["model"].predict_proba(X)[0][1])
        probability_under = 1.0 - probability_over
        fair_over = round(1 / probability_over, 2) if probability_over > 0 else None
        fair_under = round(1 / probability_under, 2) if probability_under > 0 else None
        real_over, real_under = _latest_corner_odds(session, fixture_id)
        selected_is_over = probability_over >= probability_under
        real_odds = real_over if selected_is_over else real_under
        model_probability = max(probability_over, probability_under)
        fair_odds = fair_over if selected_is_over else fair_under
        selected_market = MARKET if selected_is_over else "corners_under_9_5"
        expected_value = (model_probability * real_odds - 1) if real_odds else None
        confidence = _confidence(probability_over)
        value = analyze_value(model_probability, real_odds, confidence)
        fixture = session.get(Fixture, fixture_id)

        prediction_values = {
            "fixture_id": fixture_id,
            "market": selected_market,
            "model_name": MODEL_NAME,
            "league": fixture.league if fixture else None,
            "probability": round(model_probability, 4),
            "implied_probability": value.implied_probability,
            "fair_odds": fair_odds,
            "real_odds": real_odds,
            "edge": value.edge,
            "value_score": value.value_score,
            "expected_value": value.expected_value,
            "confidence": confidence,
            "recommended": value.recommended,
            "pick_type": value.pick_type,
            "predicted_at": _utcnow(),
        }
        stmt = pg_insert(Prediction).values(**prediction_values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["fixture_id", "market", "model_name"],
                set_={
                    key: value
                    for key, value in prediction_values.items()
                    if key not in {"fixture_id", "market", "model_name"}
                },
            )
        )
        session.commit()

    return {
        "available": True,
        "fixture_id": fixture_id,
        "probability_over_9_5": round(probability_over, 4),
        "probability_under_9_5": round(probability_under, 4),
        "fair_odds_over": fair_over,
        "fair_odds_under": fair_under,
        "real_odds_over": real_over,
        "real_odds_under": real_under,
        "edge_over": round(probability_over - (1 / real_over), 4) if real_over else None,
        "edge_under": round(probability_under - (1 / real_under), 4) if real_under else None,
        "expected_value": value.expected_value,
        "implied_probability": value.implied_probability,
        "edge": value.edge,
        "value_score": value.value_score,
        "confidence": confidence,
        "recommended": value.recommended,
        "pick_type": value.pick_type,
    }


def evaluate_model() -> dict[str, Any]:
    """Evaluate hit rate and simulated ROI on the most recent 20% only."""
    dataset = build_dataset()
    if dataset.empty or len(dataset) < MIN_TRAINING_ROWS:
        return {"available": False, "error": f"histÃ³rico insuficiente ({len(dataset)} filas)"}

    split_index = max(1, int(len(dataset) * (1 - TEST_FRACTION)))
    if split_index >= len(dataset):
        split_index = len(dataset) - 1

    train_dataset = dataset.iloc[:split_index].copy()
    test_dataset = dataset.iloc[split_index:].copy()
    trained = _fit_model(train_dataset)
    if trained.get("error"):
        return {"available": False, "error": trained["error"]}

    test_matrix, feature_cols = _prepare_matrix(test_dataset, trained["feature_columns"])
    X_test = test_matrix[feature_cols].fillna(0)
    y_test = test_matrix["target_over_9_5"]
    probabilities = trained["model"].predict_proba(X_test)[:, 1]
    brier_score = round(
        sum((float(probability) - int(actual)) ** 2 for probability, actual in zip(probabilities, y_test))
        / len(y_test),
        4,
    )
    clipped = [min(max(float(probability), 1e-15), 1 - 1e-15) for probability in probabilities]
    log_loss = round(
        -sum(
            int(actual) * math.log(probability) + (1 - int(actual)) * math.log(1 - probability)
            for probability, actual in zip(clipped, y_test)
        )
        / len(y_test),
        4,
    )

    profit = 0.0
    stake = 1.0
    total_picks = 0
    hits = 0
    try:
        session = get_session()
    except RuntimeError:
        session = None

    if session is not None:
        with session:
            for fixture_id, probability, actual in zip(test_dataset["fixture_id"], probabilities, y_test):
                over_odds, under_odds = _latest_corner_odds(session, int(fixture_id))
                if probability >= 0.5 and over_odds:
                    profit += (over_odds - 1) * stake if actual == 1 else -stake
                    total_picks += 1
                    hits += 1 if actual == 1 else 0
                elif probability < 0.5 and under_odds:
                    profit += (under_odds - 1) * stake if actual == 0 else -stake
                    total_picks += 1
                    hits += 1 if actual == 0 else 0
            hit_rate = round(float(hits / total_picks), 4) if total_picks else 0.0
            roi = round(profit / total_picks, 4) if total_picks else 0.0
            session.add(
                ModelPerformance(
                    model_name=MODEL_NAME,
                    market=MARKET,
                    league="ALL",
                    hit_rate=hit_rate,
                    roi=roi,
                    yield_rate=roi,
                    brier_score=brier_score,
                    log_loss=log_loss,
                    sample_size=int(len(test_dataset)),
                    total_picks=total_picks,
                    last_updated=_utcnow(),
                )
            )
            session.commit()
    else:
        hit_rate = 0.0
        roi = 0.0
        total_picks = 0

    return {
        "available": True,
        "model_name": MODEL_NAME,
        "market": MARKET,
        "hit_rate": hit_rate,
        "roi": roi,
        "yield_rate": roi,
        "brier_score": brier_score,
        "log_loss": log_loss,
        "sample_size": int(len(test_dataset)),
        "train_size": int(len(train_dataset)),
        "test_size": int(len(test_dataset)),
        "total_picks": total_picks,
    }


def _latest_performance(session) -> ModelPerformance | None:
    return session.scalar(
        select(ModelPerformance)
        .where(
            ModelPerformance.model_name == MODEL_NAME,
            ModelPerformance.market == MARKET,
        )
        .order_by(ModelPerformance.last_updated.desc())
        .limit(1)
    )


def retrain_if_needed(now: datetime | None = None) -> dict[str, Any]:
    """Weekly retraining entrypoint, intended for Sundays at 03:00 UTC."""
    now = now or _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now.weekday() != 6 or now.hour != 3:
        return {
            "retrained": False,
            "reason": "fuera de ventana semanal domingo 03:00 UTC",
        }

    dataset = build_dataset()
    row_count = len(dataset)
    previous_bundle = _load_model_bundle()
    previous_row_count = int(previous_bundle.get("row_count") or 0) if previous_bundle else 0
    new_matches = row_count - previous_row_count
    if new_matches < MIN_NEW_MATCHES_FOR_RETRAIN:
        logging.info(
            "[corners_model] reentrenamiento omitido: %s partidos nuevos desde el Ãºltimo entrenamiento (mÃ­nimo %s)",
            new_matches,
            MIN_NEW_MATCHES_FOR_RETRAIN,
        )
        return {
            "retrained": False,
            "reason": "partidos nuevos insuficientes",
            "new_matches": new_matches,
            "row_count": row_count,
        }

    trained = _fit_model(dataset)
    if trained.get("error"):
        logging.info("[corners_model] reentrenamiento no disponible: %s", trained["error"])
        return {
            "retrained": False,
            "reason": trained["error"],
            "new_matches": new_matches,
            "row_count": row_count,
        }

    bundle = {
        "model": trained["model"],
        "feature_columns": list(trained["feature_columns"]),
        "trained_at": now,
        "row_count": row_count,
        "model_name": MODEL_NAME,
        "market": MARKET,
    }
    _save_model_bundle(bundle)
    with _MODEL_CACHE_LOCK:
        _cache_model_bundle(bundle)

    previous_metrics = None
    try:
        session = get_session()
    except RuntimeError:
        session = None
    if session is not None:
        with session:
            previous = _latest_performance(session)
            if previous:
                previous_metrics = {
                    "hit_rate": previous.hit_rate,
                    "roi": previous.roi,
                    "total_picks": previous.total_picks,
                    "last_updated": previous.last_updated,
                }

    metrics = evaluate_model()
    if metrics.get("available"):
        logging.info(
            (
                "[corners_model] reentrenado %s filas (+%s), guardado en %s. "
                "Nuevo: hit_rate=%s roi=%s total_picks=%s. Anterior: %s"
            ),
            row_count,
            new_matches,
            MODEL_PATH,
            metrics.get("hit_rate"),
            metrics.get("roi"),
            metrics.get("total_picks"),
            previous_metrics or "sin mÃ©tricas previas",
        )
    else:
        logging.info(
            "[corners_model] reentrenado %s filas (+%s), pero la evaluaciÃ³n no estÃ¡ disponible: %s",
            row_count,
            new_matches,
            metrics.get("error"),
        )

    return {
        "retrained": True,
        "model_path": str(MODEL_PATH),
        "new_matches": new_matches,
        "row_count": row_count,
        "metrics": metrics,
        "previous_metrics": previous_metrics,
    }
