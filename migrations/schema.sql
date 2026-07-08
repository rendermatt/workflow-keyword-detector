-- Keyword scraper results table
-- Run this once against your Render PostgreSQL database before first use.

CREATE TABLE IF NOT EXISTS scrape_results (
    id         SERIAL      PRIMARY KEY,
    run_id     TEXT        NOT NULL,          -- UUID of the run that last wrote this row
    feature_id INTEGER,                       -- feature this keyword belongs to (NULL until the scraper is feature-aware)
    url        TEXT        NOT NULL,
    keyword    TEXT        NOT NULL,
    count      INTEGER     NOT NULL,
    scraped_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Latest-snapshot semantics: re-scraping the same feature/url/keyword
    -- upserts the existing row instead of appending, so hits are never double
    -- counted. NULLS NOT DISTINCT makes a NULL feature_id dedupe on (url,
    -- keyword) today, and tighten to per-feature grain once feature_id is set.
    CONSTRAINT scrape_results_feature_url_keyword_key
        UNIQUE NULLS NOT DISTINCT (feature_id, url, keyword)
);

CREATE INDEX IF NOT EXISTS idx_scrape_results_run_id  ON scrape_results (run_id);
CREATE INDEX IF NOT EXISTS idx_scrape_results_url     ON scrape_results (url);
CREATE INDEX IF NOT EXISTS idx_scrape_results_keyword ON scrape_results (keyword);

-- Handy views ----------------------------------------------------------------

-- Totals per keyword across all runs
CREATE OR REPLACE VIEW keyword_totals AS
SELECT keyword, SUM(count) AS total_occurrences, COUNT(DISTINCT url) AS urls_matched
FROM scrape_results
GROUP BY keyword
ORDER BY total_occurrences DESC;

-- Per-run summary
CREATE OR REPLACE VIEW run_summary AS
SELECT run_id,
       COUNT(DISTINCT url)                 AS urls_scraped,
       COUNT(DISTINCT keyword)             AS keywords_tracked,
       MIN(scraped_at)                     AS started_at,
       MAX(scraped_at)                     AS finished_at
FROM scrape_results
GROUP BY run_id
ORDER BY started_at DESC;

-- Features ---------------------------------------------------------------------
-- A "feature" is a thing we look for on a prospect's site to identify a
-- potential user (e.g. "Workflows"). Each feature bundles a documentation link
-- and a set of keywords that signal the feature is in use. Future scrape runs
-- will iterate over features instead of a single flat keyword list.

CREATE TABLE IF NOT EXISTS features (
    id                SERIAL      PRIMARY KEY,
    name              TEXT        NOT NULL UNIQUE,
    documentation_url TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feature_keywords (
    id         SERIAL  PRIMARY KEY,
    feature_id INTEGER NOT NULL REFERENCES features (id) ON DELETE CASCADE,
    keyword    TEXT    NOT NULL,
    UNIQUE (feature_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_feature_keywords_feature_id
    ON feature_keywords (feature_id);
