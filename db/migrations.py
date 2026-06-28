import logging

from db.connection import get_engine
from db.models import Base


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
    logging.info("Tablas PostgreSQL inicializadas.")
    return True
