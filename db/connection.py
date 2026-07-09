import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

load_dotenv()


def get_database_url() -> str | None:
    url = ""
    for key in ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "PGDATABASE_URL"):
        url = os.getenv(key, "").strip()
        if url:
            break
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url or None


DATABASE_URL: str | None = None
engine: Engine | None = None
SessionLocal = None


def _ensure_engine() -> Engine | None:
    global DATABASE_URL, SessionLocal, engine
    url = get_database_url()
    if not url:
        return None
    if engine is None or DATABASE_URL != url:
        DATABASE_URL = url
        engine = create_engine(url, pool_pre_ping=True, future=True)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    return engine


def get_engine() -> Engine | None:
    return _ensure_engine()


def get_session():
    _ensure_engine()
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL/POSTGRES_URL no está configurado")
    return SessionLocal()
