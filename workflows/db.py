import csv
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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


def init_db() -> None:
    """Create tables if they don't exist and migrate older tables in place.

    Called once per process_csvs run. Enforces latest-snapshot semantics: one
    row per (feature_id, url, keyword), so re-scraping upserts rather than
    appending and hits are never double counted.
    """
    with _cursor() as cur:
        # Fresh installs get the full definition, including the unique key.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scrape_results (
                id         SERIAL PRIMARY KEY,
                run_id     TEXT        NOT NULL,
                feature_id INTEGER,
                url        TEXT        NOT NULL,
                keyword    TEXT        NOT NULL,
                count      INTEGER     NOT NULL,
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT scrape_results_feature_url_keyword_key
                    UNIQUE NULLS NOT DISTINCT (feature_id, url, keyword)
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

        # One row per crawled page, tracking which domain it belongs to and
        # whether the fetch succeeded. This is the authoritative record of crawl
        # coverage (how many pages per domain, which paths/subdomains).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crawled_pages (
                id             SERIAL      PRIMARY KEY,
                run_id         TEXT        NOT NULL,
                domain         TEXT        NOT NULL,   -- registrable domain of the seed
                url            TEXT        NOT NULL UNIQUE,
                ok             BOOLEAN     NOT NULL DEFAULT TRUE,
                status_code    INTEGER,
                blocked_reason TEXT,                   -- e.g. 'cf-mitigated' on a Cloudflare 403
                crawled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain
            ON crawled_pages (domain)
        """)
        # Migrate tables created before blocked_reason existed.
        cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS blocked_reason TEXT")

        # Migrate pre-existing tables that predate feature_id / the unique key.
        cur.execute("ALTER TABLE scrape_results ADD COLUMN IF NOT EXISTS feature_id INTEGER")
        cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (_UNIQUE_CONSTRAINT,))
        if not cur.fetchone():
            # Collapse historical duplicates, keeping the most recent row
            # (highest id) per key, before the unique constraint can be added.
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
    logger.info("Database ready")


def save_result(run_id: str, url: str, keyword_counts: dict[str, int]) -> None:
    """Write results to PostgreSQL or a local CSV depending on LOCAL_OUTPUT_CSV."""
    local_csv = os.environ.get("LOCAL_OUTPUT_CSV")
    if local_csv:
        _save_result_csv(local_csv, run_id, url, keyword_counts)
    else:
        _save_result_db(run_id, url, keyword_counts)


def record_page(
    run_id: str,
    domain: str,
    url: str,
    ok: bool,
    status_code: int | None,
    blocked_reason: str | None = None,
) -> None:
    """Upsert a crawled page into crawled_pages (skipped in local CSV mode)."""
    if os.environ.get("LOCAL_OUTPUT_CSV"):
        return
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawled_pages (run_id, domain, url, ok, status_code, blocked_reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE
                SET run_id         = EXCLUDED.run_id,
                    domain         = EXCLUDED.domain,
                    ok             = EXCLUDED.ok,
                    status_code    = EXCLUDED.status_code,
                    blocked_reason = EXCLUDED.blocked_reason,
                    crawled_at     = NOW()
            """,
            (run_id, domain, url, ok, status_code, blocked_reason),
        )


def _save_result_db(run_id: str, url: str, keyword_counts: dict[str, int]) -> None:
    # feature_id is NULL until the scraper is made feature-aware; NULLS NOT
    # DISTINCT on the unique key means this upserts on (url, keyword) today.
    rows = [(run_id, None, url, kw, cnt) for kw, cnt in keyword_counts.items()]
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


_CSV_COLUMNS = ["run_id", "url", "keyword", "count", "scraped_at"]


def _save_result_csv(filepath: str, run_id: str, url: str, keyword_counts: dict[str, int]) -> None:
    path = Path(filepath)
    write_header = not path.exists()
    now = datetime.now(timezone.utc).isoformat()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for keyword, count in keyword_counts.items():
            writer.writerow({"run_id": run_id, "url": url, "keyword": keyword, "count": count, "scraped_at": now})
    logger.info(f"Wrote {len(keyword_counts)} rows to {filepath} for {url}")
