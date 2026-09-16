"""Database engine, session factory and declarative base.

PostgreSQL in production (Railway), SQLite for local dev and tests — the
same SQLAlchemy models work on both.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

# Deterministic constraint names make schema errors readable and migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def normalize_database_url(url: str) -> str:
    """Railway/Heroku hand out postgres:// URLs; SQLAlchemy + psycopg3 wants postgresql+psycopg://."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def build_engine(url: str) -> Engine:
    url = normalize_database_url(url)
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


engine: Engine = build_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def configure_engine(url: str) -> None:
    """Re-point the app at another database (used by tests)."""
    global engine
    engine.dispose()
    engine = build_engine(url)
    SessionLocal.configure(bind=engine)


def init_db(retries: int = 5, delay_seconds: float = 2.0) -> None:
    """Create tables, retrying briefly so a DB that boots slower than the app doesn't crash it."""
    from app import models  # noqa: F401  (register models on the metadata)

    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            Base.metadata.create_all(bind=engine)
            logger.info("database ready (%s)", engine.url.get_backend_name())
            return
        except Exception as exc:  # pragma: no cover - depends on infra
            logger.warning("database not ready (attempt %s/%s): %s", attempt, retries, exc)
            if attempt == retries:
                raise
            time.sleep(delay_seconds)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
