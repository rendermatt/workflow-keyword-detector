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


_CRAWLED_PAGES_CONSTRAINT = "crawled_pages_feature_url_key"
_FIT_CONSTRAINT = "fit_assessments_feature_domain_key"


def init_db() -> None:
    """Create the tables the crawl + assessment pipeline needs, migrating older
    tables in place. Called once per run.

    Crawl coverage is one row per (feature_id, url); fit assessments are one row
    per (feature_id, domain), so re-crawling a feature upserts rather than
    appending.
    """
    with _cursor() as cur:
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
        # These column-adding migrations MUST run before the indexes/constraints
        # that reference them: on a pre-existing crawled_pages table the CREATE
        # above is a no-op, so the columns won't exist yet.
        cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS feature_id INTEGER")
        cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS blocked_reason TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_feature ON crawled_pages (feature_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain ON crawled_pages (domain)")
        cur.execute("ALTER TABLE crawled_pages DROP CONSTRAINT IF EXISTS crawled_pages_url_key")
        cur.execute("SELECT 1 FROM pg_constraint WHERE conname = %s", (_CRAWLED_PAGES_CONSTRAINT,))
        if not cur.fetchone():
            cur.execute(f"""
                ALTER TABLE crawled_pages
                ADD CONSTRAINT {_CRAWLED_PAGES_CONSTRAINT} UNIQUE (feature_id, url)
            """)

        # LLM fit assessments: one row per (feature, customer domain).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fit_assessments (
                id             SERIAL      PRIMARY KEY,
                run_id         TEXT        NOT NULL,
                feature_id     INTEGER     NOT NULL,
                domain         TEXT        NOT NULL,
                fit_score      INTEGER,
                tier           TEXT,
                summary        TEXT,
                signals        JSONB,
                recommendation TEXT,
                model          TEXT,
                pages_analyzed INTEGER,
                content_chars  INTEGER,
                error          TEXT,
                assessed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT fit_assessments_feature_domain_key UNIQUE (feature_id, domain)
            )
        """)
        cur.execute("ALTER TABLE fit_assessments ADD COLUMN IF NOT EXISTS content_chars INTEGER")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_fit_assessments_feature ON fit_assessments (feature_id)"
        )

        # Keyword counts per (feature, company domain) for the heatmap.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS keyword_hits (
                id         SERIAL      PRIMARY KEY,
                feature_id INTEGER     NOT NULL,
                run_id     TEXT        NOT NULL,
                domain     TEXT        NOT NULL,
                keyword    TEXT        NOT NULL,
                count      INTEGER     NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                CONSTRAINT keyword_hits_feature_domain_keyword_key UNIQUE (feature_id, domain, keyword)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_keyword_hits_feature ON keyword_hits (feature_id)")

        # Editable app settings (e.g. the default fit-assessment prompt).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key        TEXT        PRIMARY KEY,
                value      TEXT        NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        # Per-feature columns (the features table itself is owned by the
        # dashboard; add these defensively in case a crawl runs first).
        # doc_brief / doc_brief_hash cache the distilled documentation so we only
        # re-summarize when the docs change.
        cur.execute("SELECT to_regclass('public.features')")
        if cur.fetchone()[0] is not None:
            cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS additional_prompt TEXT")
            cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS doc_brief TEXT")
            cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS doc_brief_hash TEXT")
            cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS keywords JSONB")
            cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS keywords_hash TEXT")
    logger.info("Database ready")


def get_feature(feature_id: int) -> dict | None:
    """Return the feature's id, name, and documentation URL (created by the
    dashboard), or None if it doesn't exist."""
    with _cursor() as cur:
        cur.execute(
            "SELECT id, name, documentation_url, additional_prompt, doc_brief, doc_brief_hash, "
            "keywords, keywords_hash FROM features WHERE id = %s",
            (feature_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "documentation_url": row[2],
            "additional_prompt": row[3],
            "doc_brief": row[4],
            "doc_brief_hash": row[5],
            "keywords": row[6],
            "keywords_hash": row[7],
        }


def save_doc_brief(feature_id: int, brief: str, content_hash: str) -> None:
    """Cache the distilled documentation brief and the hash of the docs it was
    derived from, so future crawls skip re-distilling unchanged documentation."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE features SET doc_brief = %s, doc_brief_hash = %s WHERE id = %s",
            (brief, content_hash, feature_id),
        )


def save_keywords(feature_id: int, keywords: list[str], content_hash: str) -> None:
    """Cache the generated keyword list and the hash it was derived from."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE features SET keywords = %s, keywords_hash = %s WHERE id = %s",
            (psycopg2.extras.Json(keywords), content_hash, feature_id),
        )


def save_keyword_hits(feature_id: int, run_id: str, domain: str, counts: dict[str, int]) -> None:
    """Replace a company's keyword counts for a feature with this run's tallies."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM keyword_hits WHERE feature_id = %s AND domain = %s",
            (feature_id, domain),
        )
        rows = [
            (feature_id, run_id, domain, kw, cnt)
            for kw, cnt in counts.items()
            if cnt
        ]
        if rows:
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO keyword_hits (feature_id, run_id, domain, keyword, count) VALUES %s",
                rows,
            )


def get_setting(key: str, default: str | None = None) -> str | None:
    """Read an editable setting from app_settings (e.g. the fit prompt)."""
    with _cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else default


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


def save_fit_assessment(assessment: dict) -> None:
    """Upsert one company's fit assessment for a feature."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO fit_assessments
                (run_id, feature_id, domain, fit_score, tier, summary,
                 signals, recommendation, model, pages_analyzed, content_chars, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ON CONSTRAINT fit_assessments_feature_domain_key DO UPDATE
                SET run_id         = EXCLUDED.run_id,
                    fit_score      = EXCLUDED.fit_score,
                    tier           = EXCLUDED.tier,
                    summary        = EXCLUDED.summary,
                    signals        = EXCLUDED.signals,
                    recommendation = EXCLUDED.recommendation,
                    model          = EXCLUDED.model,
                    pages_analyzed = EXCLUDED.pages_analyzed,
                    content_chars  = EXCLUDED.content_chars,
                    error          = EXCLUDED.error,
                    assessed_at    = NOW()
            """,
            (
                assessment["run_id"],
                assessment["feature_id"],
                assessment["domain"],
                assessment.get("fit_score"),
                assessment.get("tier"),
                assessment.get("summary"),
                psycopg2.extras.Json(assessment.get("signals") or []),
                assessment.get("recommendation"),
                assessment.get("model"),
                assessment.get("pages_analyzed"),
                assessment.get("content_chars"),
                assessment.get("error"),
            ),
        )
    logger.info("Saved fit assessment for %s", assessment["domain"])
