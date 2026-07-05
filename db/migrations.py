import logging

from sqlalchemy import text

from db.connection import get_engine
from db.models import Base


def _apply_incremental_migrations(engine) -> None:
    """Bring already-existing PostgreSQL tables up to date.

    Base.metadata.create_all() only creates missing tables; it never alters
    existing ones. These idempotent statements add the new column and unique
    index introduced after the tables were first created in production.
    """
    with engine.begin() as conn:
        # Bets live only in SQLite; remove the dead PostgreSQL table if an old
        # deploy created it.
        conn.execute(text("DROP TABLE IF EXISTS bets"))
        # model_performance.total_picks (real count of evaluated picks).
        conn.execute(
            text(
                "ALTER TABLE model_performance "
                "ADD COLUMN IF NOT EXISTS total_picks INTEGER"
            )
        )
        # Prediction settlement fields populated once final stats are collected.
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS correct BOOLEAN"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS profit DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS settled_at TIMESTAMP WITH TIME ZONE"))
        # Prediction tracking metadata. Existing rows are backfilled to the
        # current corners model so the new unique key can be enforced safely.
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS model_name VARCHAR(255)"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS league VARCHAR(255)"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS implied_probability DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS edge DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS value_score DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS pick_type VARCHAR(50)"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS predicted_at TIMESTAMP WITH TIME ZONE"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS closing_odds DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS closing_implied_probability DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE predictions ADD COLUMN IF NOT EXISTS clv DOUBLE PRECISION"))
        conn.execute(text("UPDATE predictions SET model_name = 'corners_logistic_regression_v1' WHERE model_name IS NULL"))
        conn.execute(text("UPDATE predictions SET predicted_at = NOW() WHERE predicted_at IS NULL"))
        conn.execute(text("ALTER TABLE predictions ALTER COLUMN model_name SET DEFAULT 'unknown'"))
        conn.execute(text("ALTER TABLE predictions ALTER COLUMN model_name SET NOT NULL"))
        conn.execute(text("ALTER TABLE predictions ALTER COLUMN predicted_at SET DEFAULT NOW()"))
        conn.execute(text("ALTER TABLE predictions ALTER COLUMN predicted_at SET NOT NULL"))
        # Extra model performance fields for evaluation history.
        conn.execute(text("ALTER TABLE model_performance ADD COLUMN IF NOT EXISTS yield_rate DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE model_performance ADD COLUMN IF NOT EXISTS brier_score DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE model_performance ADD COLUMN IF NOT EXISTS log_loss DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE model_performance ADD COLUMN IF NOT EXISTS league VARCHAR(255)"))
        # Drop pre-existing duplicate predictions (keep the latest row per
        # fixture+market) before enforcing uniqueness, otherwise the index
        # creation would fail on legacy data.
        conn.execute(
            text(
                "DELETE FROM predictions p "
                "USING predictions q "
                "WHERE p.fixture_id = q.fixture_id "
                "AND p.market = q.market "
                "AND p.model_name = q.model_name "
                "AND p.id < q.id"
            )
        )
        # Unique index backing the predict() upsert (fixture_id + market + model).
        conn.execute(text("ALTER TABLE predictions DROP CONSTRAINT IF EXISTS uq_predictions_fixture_market"))
        conn.execute(text("DROP INDEX IF EXISTS uq_predictions_fixture_market"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_fixture_market_model "
                "ON predictions (fixture_id, market, model_name)"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_predictions_fixture_id ON predictions (fixture_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_predictions_market_model ON predictions (market, model_name)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_predictions_predicted_at ON predictions (predicted_at)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_odds_snapshots_fixture_market_time ON odds_snapshots (fixture_id, market, timestamp DESC)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_team_stats_fixture_team ON team_stats (fixture_id, team_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_player_stats_fixture_player ON player_stats (fixture_id, player_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_fixtures_status_date ON fixtures (status, date)"))
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_injury_fixture_player_reason "
                "ON injury_reports (fixture_id, player_id, reason)"
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_injury_reports_fixture_id ON injury_reports (fixture_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_injury_reports_team_id ON injury_reports (team_id)"))


def initialize_postgres() -> bool:
    """Create PostgreSQL tables when DATABASE_URL is configured.

    Returns True when tables were checked/created, False when PostgreSQL is not
    configured. This keeps the existing SQLite bot path untouched.
    """
    engine = get_engine()
    if engine is None:
        logging.info("DATABASE_URL no configurado: PostgreSQL deshabilitado.")
        return False

    Base.metadata.create_all(bind=engine)
    try:
        _apply_incremental_migrations(engine)
    except Exception:
        logging.exception("No se pudieron aplicar las migraciones incrementales de PostgreSQL.")
    logging.info("Tablas PostgreSQL inicializadas.")
    return True
