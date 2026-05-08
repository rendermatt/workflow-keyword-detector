import logging
import os
from contextlib import contextmanager

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


@contextmanager
def _cursor():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Called once per process_csvs run."""
    with _cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_results (
                id         SERIAL PRIMARY KEY,
                run_id     TEXT        NOT NULL,
                url        TEXT        NOT NULL,
                keyword    TEXT        NOT NULL,
                count      INTEGER     NOT NULL,
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_scrape_results_run_id
            ON scrape_results (run_id)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_scrape_results_keyword
            ON scrape_results (keyword)
        """)
    logger.info("Database ready")


def save_result(run_id: str, url: str, keyword_counts: dict[str, int]) -> None:
    rows = [(run_id, url, kw, cnt) for kw, cnt in keyword_counts.items()]
    if not rows:
        return
    with _cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO scrape_results (run_id, url, keyword, count) VALUES %s",
            rows,
        )
    logger.info(f"Saved {len(rows)} rows for {url}")
