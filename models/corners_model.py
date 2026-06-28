import logging
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from db.connection import get_session
from db.models import Fixture, ModelPerformance, OddsSnapshot, Prediction, Team, TeamStat


MODEL_NAME = "corners_logistic_regression_v1"
MARKET = "corners_over_9_5"
CORNER_LINE = 9.5
MIN_TRAINING_ROWS = 20
FEATURE_COLUMNS = [
    "home_corners_avg_5",
    "away_corners_avg_5",
    "home_possession_avg_5",
    "away_possession_avg_5",
    "home_shots_avg_5",
    "away_shots_avg_5",
    "home_is_home",
]


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


def _prepare_matrix(dataset):
    pd = __import__("pandas")
    df = dataset.copy()
    if "league" in df.columns:
        df = pd.get_dummies(df, columns=["league"], dummy_na=False)
    feature_cols = [c for c in df.columns if c in FEATURE_COLUMNS or c.startswith("league_")]
    return df, feature_cols


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
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    dataset = build_dataset()
    if dataset.empty or len(dataset) < MIN_TRAINING_ROWS:
        return {
            "model": None,
            "dataset": dataset,
            "feature_columns": [],
            "error": f"histórico insuficiente ({len(dataset)} filas)",
        }
    if dataset["target_over_9_5"].nunique() < 2:
        return {
            "model": None,
            "dataset": dataset,
            "feature_columns": [],
            "error": "histórico con una sola clase para corners",
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
    return {"model": model, "dataset": dataset, "feature_columns": feature_cols, "error": None}


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
        trained = train_model()
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
        expected_value = (model_probability * real_odds - 1) if real_odds else None
        confidence = _confidence(probability_over)
        recommended = bool(real_odds and expected_value and expected_value > 0 and confidence >= 60)

        session.add(
            Prediction(
                fixture_id=fixture_id,
                market=MARKET if selected_is_over else "corners_under_9_5",
                probability=round(model_probability, 4),
                fair_odds=fair_odds,
                real_odds=real_odds,
                expected_value=round(expected_value, 4) if expected_value is not None else None,
                confidence=confidence,
                recommended=recommended,
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
        "expected_value": round(expected_value, 4) if expected_value is not None else None,
        "confidence": confidence,
        "recommended": recommended,
    }


def evaluate_model() -> dict[str, Any]:
    """Evaluate hit rate and simulated ROI, then store model performance."""
    trained = train_model()
    dataset = trained.get("dataset")
    if trained.get("error"):
        return {"available": False, "error": trained["error"]}

    matrix, feature_cols = _prepare_matrix(dataset)
    X = matrix[feature_cols].fillna(0)
    y = matrix["target_over_9_5"]
    probabilities = trained["model"].predict_proba(X)[:, 1]
    picks = probabilities >= 0.5
    hits = (picks.astype(int) == y.to_numpy()).sum()
    hit_rate = round(float(hits / len(y)), 4) if len(y) else 0.0

    profit = 0.0
    stake = 1.0
    bets = 0
    try:
        session = get_session()
    except RuntimeError:
        session = None

    if session is not None:
        with session:
            for fixture_id, probability, actual in zip(dataset["fixture_id"], probabilities, y):
                over_odds, under_odds = _latest_corner_odds(session, int(fixture_id))
                if probability >= 0.5 and over_odds:
                    profit += (over_odds - 1) * stake if actual == 1 else -stake
                    bets += 1
                elif probability < 0.5 and under_odds:
                    profit += (under_odds - 1) * stake if actual == 0 else -stake
                    bets += 1
            roi = round(profit / bets, 4) if bets else 0.0
            session.execute(
                delete(ModelPerformance).where(
                    ModelPerformance.model_name == MODEL_NAME,
                    ModelPerformance.market == MARKET,
                )
            )
            session.add(
                ModelPerformance(
                    model_name=MODEL_NAME,
                    market=MARKET,
                    hit_rate=hit_rate,
                    roi=roi,
                    sample_size=int(len(dataset)),
                    last_updated=datetime.utcnow(),
                )
            )
            session.commit()
    else:
        roi = 0.0

    return {
        "available": True,
        "model_name": MODEL_NAME,
        "market": MARKET,
        "hit_rate": hit_rate,
        "roi": roi,
        "sample_size": int(len(dataset)),
    }
