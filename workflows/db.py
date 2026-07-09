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


_UNIQUE_CONSTRAINT = "scrape_results_feature_url_keyword_key"
_CRAWLED_PAGES_CONSTRAINT = "crawled_pages_feature_url_key"


def init_db() -> None:
    """Create tables if they don't exist and migrate older tables in place.

    Called once per crawl run. Enforces latest-snapshot semantics: one row per
    (feature_id, url, keyword) for hits and per (feature_id, url) for coverage,
    so re-crawling a feature upserts rather than appending.
    """
    with _cursor() as cur:
        # Keyword hits. Fresh installs get the full definition, incl. unique key.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_results (
                id         SERIAL PRIMARY KEY,
                run_id     TEXT        NOT NULL,
                feature_id INTEGER     NOT NULL,
                url        TEXT        NOT NULL,
                keyword    TEXT        NOT NULL,
                count      INTEGER     NOT NULL,
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT scrape_results_feature_url_keyword_key
                    UNIQUE NULLS NOT DISTINCT (feature_id, url, keyword)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_scrape_results_run_id ON scrape_results (run_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_scrape_results_feature ON scrape_results (feature_id)")

        # Crawl coverage: one row per (feature, page) tracking the fetch outcome.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crawled_pages (
                id             SERIAL      PRIMARY KEY,
                run_id         TEXT        NOT NULL,
                feature_id     INTEGER     NOT NULL,
                domain         TEXT        NOT NULL,   -- registrable domain of the seed
                url            TEXT        NOT NULL,
                ok             BOOLEAN     NOT NULL DEFAULT TRUE,
                status_code    INTEGER,
                blocked_reason TEXT,                   -- e.g. 'cf-mitigated' / 'robots-disallowed'
                crawled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT crawled_pages_feature_url_key UNIQUE (feature_id, url)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_feature ON crawled_pages (feature_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain ON crawled_pages (domain)")

        _migrate_scrape_results(cur)
        _migrate_crawled_pages(cur)
    logger.info("Database ready")


def _migrate_scrape_results(cur) -> None:
    """Bring pre-existing scrape_results tables up to the current shape."""
    cur.execute("ALTER TABLE scrape_results ADD COLUMN IF NOT EXISTS feature_id INTEGER")
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (_UNIQUE_CONSTRAINT,))
    if not cur.fetchone():
        cur.execute("""
            DELETE FROM scrape_results a
            USING scrape_results b
            WHERE a.id < b.id
              AND a.url = b.url
              AND a.keyword = b.keyword
              AND a.feature_id IS NOT DISTINCT FROM b.feature_id
        """)
        cur.execute(f"""
            ALTER TABLE scrape_results
            ADD CONSTRAINT {_UNIQUE_CONSTRAINT}
            UNIQUE NULLS NOT DISTINCT (feature_id, url, keyword)
        """)


def _migrate_crawled_pages(cur) -> None:
    """Add feature_id and switch the unique key from (url) to (feature_id, url)."""
    cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS feature_id INTEGER")
    cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS blocked_reason TEXT")
    # Drop the legacy UNIQUE(url) constraint if it's still there.
    cur.execute("ALTER TABLE crawled_pages DROP CONSTRAINT IF EXISTS crawled_pages_url_key")
    cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (_CRAWLED_PAGES_CONSTRAINT,))
    if not cur.fetchone():
        cur.execute(f"""
            ALTER TABLE crawled_pages
            ADD CONSTRAINT {_CRAWLED_PAGES_CONSTRAINT} UNIQUE (feature_id, url)
        """)


def get_feature_keywords(feature_id: int) -> list[str]:
    """Return the keyword list configured for a feature."""
    with _cursor() as cur:
        cur.execute(
            "SELECT keyword FROM feature_keywords WHERE feature_id = %s ORDER BY keyword",
            (feature_id,),
        )
        return [r[0] for r in cur.fetchall()]


def save_result(
    run_id: str, feature_id: int, url: str, keyword_counts: dict[str, int]
) -> None:
    """Upsert keyword counts for one page, scoped to a feature."""
    rows = [(run_id, feature_id, url, kw, cnt) for kw, cnt in keyword_counts.items()]
    if not rows:
        return
    with _cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO scrape_results (run_id, feature_id, url, keyword, count)
            VALUES %s
            ON CONFLICT ON CONSTRAINT scrape_results_feature_url_keyword_key
            DO UPDATE SET count      = EXCLUDED.count,
                          run_id     = EXCLUDED.run_id,
                          scraped_at = NOW()
            """,
            rows,
        )
    logger.info(f"Upserted {len(rows)} rows for {url}")


def record_page(
    run_id: str,
    feature_id: int,
    domain: str,
    url: str,
    ok: bool,
    status_code: int | None,
    blocked_reason: str | None = None,
) -> None:
    """Upsert a crawled page into crawled_pages for a feature."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawled_pages
                (run_id, feature_id, domain, url, ok, status_code, blocked_reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT crawled_pages_feature_url_key DO UPDATE
                SET run_id         = EXCLUDED.run_id,
                    domain         = EXCLUDED.domain,
                    ok             = EXCLUDED.ok,
                    status_code    = EXCLUDED.status_code,
                    blocked_reason = EXCLUDED.blocked_reason,
                    crawled_at     = NOW()
            """,
            (run_id, feature_id, domain, url, ok, status_code, blocked_reason),
        )
