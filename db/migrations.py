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
        # Drop pre-existing duplicate predictions (keep the latest row per
        # fixture+market) before enforcing uniqueness, otherwise the index
        # creation would fail on legacy data.
        conn.execute(
            text(
                "DELETE FROM predictions p "
                "USING predictions q "
                "WHERE p.fixture_id = q.fixture_id "
                "AND p.market = q.market "
                "AND p.id < q.id"
            )
        )
        # Unique index backing the predict() upsert (fixture_id + market).
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_predictions_fixture_market "
                "ON predictions (fixture_id, market)"
            )
        )


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
