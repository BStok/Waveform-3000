from contextlib import contextmanager
import logging

import psycopg2
from psycopg2.pool import SimpleConnectionPool

from app.config import Config


logger = logging.getLogger(__name__)
db_pool = None


def init_db_pool():
    global db_pool
    db_pool = SimpleConnectionPool(
        minconn=2,
        maxconn=10,
        connect_timeout=5,
        **Config.DB_CONFIG,
    )
    logger.info("Database pool initialized: %s@%s", Config.DB_CONFIG["database"], Config.DB_CONFIG["host"])
    return db_pool


@contextmanager
def get_db():
    if not Config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    conn = psycopg2.connect(Config.DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema():
    from schema import init_schema as schema_init

    with get_db() as conn:
        schema_init(conn)
    logger.info("Schema initialized")
