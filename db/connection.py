import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

load_dotenv()


def get_database_url() -> str | None:
    url = os.getenv("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url or None


DATABASE_URL = get_database_url()

engine: Engine | None = (
    create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    if DATABASE_URL
    else None
)

SessionLocal = (
    sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    if engine
    else None
)


def get_engine() -> Engine | None:
    return engine


def get_session():
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL no está configurado")
    return SessionLocal()
