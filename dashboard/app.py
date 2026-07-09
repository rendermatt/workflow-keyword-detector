import os
import threading
from flask import Flask, jsonify, render_template, request
import psycopg2
import psycopg2.extras

from seed_data import SEED_FEATURES

DB_URL = os.environ["DATABASE_URL"]

app = Flask(__name__)


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _parse_keywords(raw) -> list[str]:
    """Normalize keyword input (list, or comma/newline-separated string) into a
    de-duplicated, order-preserving list of trimmed keywords."""
    if isinstance(raw, str):
        raw = raw.replace("\n", ",").split(",")
    seen, result = set(), []
    for kw in raw or []:
        kw = (kw or "").strip()
        key = kw.lower()
        if kw and key not in seen:
            seen.add(key)
            result.append(kw)
    return result


def init_features_schema() -> None:
    """Create the features tables if missing and seed a starter feature."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS features (
                    id                SERIAL      PRIMARY KEY,
                    name              TEXT        NOT NULL UNIQUE,
                    documentation_url TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS feature_keywords (
                    id         SERIAL  PRIMARY KEY,
                    feature_id INTEGER NOT NULL REFERENCES features (id) ON DELETE CASCADE,
                    keyword    TEXT    NOT NULL,
                    UNIQUE (feature_id, keyword)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_feature_keywords_feature_id
                ON feature_keywords (feature_id)
            """)

            # Crawl-coverage table (also created by the workflow's db.init_db);
            # ensured here so the dashboard works before the first crawl runs.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS crawled_pages (
                    id             SERIAL      PRIMARY KEY,
                    run_id         TEXT        NOT NULL,
                    feature_id     INTEGER     NOT NULL,
                    domain         TEXT        NOT NULL,
                    url            TEXT        NOT NULL,
                    ok             BOOLEAN     NOT NULL DEFAULT TRUE,
                    status_code    INTEGER,
                    blocked_reason TEXT,
                    crawled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT crawled_pages_feature_url_key UNIQUE (feature_id, url)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_feature ON crawled_pages (feature_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain ON crawled_pages (domain)")
            # Migrate older crawled_pages tables in place.
            cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS blocked_reason TEXT")
            cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS feature_id INTEGER")
            cur.execute("ALTER TABLE crawled_pages DROP CONSTRAINT IF EXISTS crawled_pages_url_key")
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'crawled_pages_feature_url_key'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE crawled_pages "
                    "ADD CONSTRAINT crawled_pages_feature_url_key UNIQUE (feature_id, url)"
                )

            # Seed a starter feature only if none exist yet.
            cur.execute("SELECT COUNT(*) AS n FROM features")
            if cur.fetchone()["n"] == 0:
                for feat in SEED_FEATURES:
                    cur.execute(
                        "INSERT INTO features (name, documentation_url) VALUES (%s, %s) RETURNING id",
                        (feat["name"], feat.get("documentation_url")),
                    )
                    fid = cur.fetchone()["id"]
                    kws = _parse_keywords(feat.get("keywords"))
                    if kws:
                        psycopg2.extras.execute_values(
                            cur,
                            "INSERT INTO feature_keywords (feature_id, keyword) VALUES %s",
                            [(fid, kw) for kw in kws],
                        )


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/features")
def features_page():
    return render_template("features.html")


@app.route("/docs")
def docs_page():
    """Explain what the crawler fetches and list the pages currently in the data."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url,
                       COUNT(DISTINCT keyword) AS keywords_matched,
                       SUM(count)              AS total_hits,
                       MAX(scraped_at)         AS last_scraped
                FROM scrape_results
                GROUP BY url
                ORDER BY url
            """)
            pages = [dict(r) for r in cur.fetchall()]
    return render_template("docs.html", pages=pages)


def _selected_feature_id():
    """The feature the dashboard is scoped to: the requested ?feature_id, or the
    first feature as a default. None only if no features exist yet."""
    feature_id = request.args.get("feature_id", type=int)
    if feature_id:
        return feature_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM features ORDER BY id LIMIT 1")
            row = cur.fetchone()
            return row["id"] if row else None


def _feature_filter():
    """A (sql_condition, params) pair scoping single-table (scrape_results or
    crawled_pages) queries to the selected feature via its feature_id column.
    Both tables now store feature_id, so re-use this in a WHERE clause:
        f_cond, f_params = _feature_filter()
        cur.execute(f"... WHERE {f_cond} ...", f_params)
    """
    fid = _selected_feature_id()
    if fid is None:
        return ("FALSE", [])
    return ("feature_id = %s", [fid])


@app.route("/api/summary")
def api_summary():
    f_cond, f_params = _feature_filter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT SUM(count) FROM scrape_results WHERE {f_cond}", f_params)
            total_hits = cur.fetchone()["sum"] or 0
            cur.execute(f"SELECT MAX(scraped_at) AS last_run FROM scrape_results WHERE {f_cond}", f_params)
            last_run = cur.fetchone()["last_run"]
            # Coverage for the selected feature. Pages skipped by robots.txt
            # weren't fetched, so exclude them from the crawled count.
            cur.execute(f"SELECT COUNT(DISTINCT domain) AS n FROM crawled_pages WHERE {f_cond}", f_params)
            total_domains = cur.fetchone()["n"]
            cur.execute(
                f"SELECT COUNT(*) AS n FROM crawled_pages "
                f"WHERE {f_cond} AND blocked_reason IS DISTINCT FROM 'robots-disallowed'",
                f_params,
            )
            pages_crawled = cur.fetchone()["n"]
    return jsonify(
        total_domains=total_domains,
        pages_crawled=pages_crawled,
        total_hits=total_hits,
        last_run_at=last_run.isoformat() if last_run else None,
    )


@app.route("/api/keywords")
def api_keywords():
    """Top keywords by total hit count across all runs."""
    f_cond, f_params = _feature_filter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT keyword, SUM(count) AS total
                FROM scrape_results
                WHERE {f_cond}
                GROUP BY keyword
                ORDER BY total DESC, keyword
            """, f_params)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/urls")
def api_urls():
    """Top URLs by total hit count across all runs."""
    f_cond, f_params = _feature_filter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT url, SUM(count) AS total
                FROM scrape_results
                WHERE {f_cond}
                GROUP BY url
                ORDER BY total DESC, url
                LIMIT 30
            """, f_params)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/domains")
def api_domains():
    """Crawl coverage per domain for the selected feature: pages crawled, total
    keyword hits, and when it was last crawled."""
    fid = _selected_feature_id()
    if fid is None:
        return jsonify([])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cp.domain,
                       COUNT(DISTINCT cp.url) FILTER (
                           WHERE cp.blocked_reason IS DISTINCT FROM 'robots-disallowed'
                       )                                             AS pages_crawled,
                       COUNT(DISTINCT cp.url) FILTER (WHERE cp.ok)   AS pages_ok,
                       COUNT(DISTINCT cp.url) FILTER (
                           WHERE cp.blocked_reason = 'robots-disallowed'
                       )                                             AS pages_robots,
                       MAX(cp.crawled_at)                            AS last_crawled,
                       COALESCE(SUM(sr.count), 0)                    AS total_hits
                FROM crawled_pages cp
                LEFT JOIN scrape_results sr
                       ON sr.url = cp.url AND sr.feature_id = cp.feature_id
                WHERE cp.feature_id = %s
                GROUP BY cp.domain
                ORDER BY pages_crawled DESC, cp.domain
            """, (fid,))
            rows = cur.fetchall()
    return jsonify([
        {**dict(r), "last_crawled": r["last_crawled"].isoformat() if r["last_crawled"] else None}
        for r in rows
    ])


@app.route("/api/domains/<path:domain>/pages")
def api_domain_pages(domain):
    """Every page crawled for a domain (for the selected feature), with its hit
    total and fetch status."""
    fid = _selected_feature_id()
    if fid is None:
        return jsonify([])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT cp.url,
                       cp.ok,
                       cp.status_code,
                       cp.blocked_reason,
                       cp.crawled_at,
                       COALESCE(SUM(sr.count), 0) AS hits
                FROM crawled_pages cp
                LEFT JOIN scrape_results sr
                       ON sr.url = cp.url AND sr.feature_id = cp.feature_id
                WHERE cp.feature_id = %s AND cp.domain = %s
                GROUP BY cp.url, cp.ok, cp.status_code, cp.blocked_reason, cp.crawled_at
                ORDER BY cp.url
            """, (fid, domain))
            rows = cur.fetchall()
    return jsonify([
        {**dict(r), "crawled_at": r["crawled_at"].isoformat() if r["crawled_at"] else None}
        for r in rows
    ])


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    """Kick off a crawl for a feature over a provided list of domains by
    triggering the workflow's run_crawl task."""
    data = request.get_json(silent=True) or {}
    try:
        feature_id = int(data.get("feature_id"))
    except (TypeError, ValueError):
        return jsonify(error="Select a feature to crawl for."), 400
    domains = [
        d.strip() for d in (data.get("domains") or [])
        if isinstance(d, str) and d.strip()
    ]
    if not domains:
        return jsonify(error="Provide at least one domain (upload a CSV)."), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM features WHERE id = %s", (feature_id,))
            feature = cur.fetchone()
            if not feature:
                return jsonify(error="Feature not found."), 404
            cur.execute(
                "SELECT COUNT(*) AS n FROM feature_keywords WHERE feature_id = %s",
                (feature_id,),
            )
            if cur.fetchone()["n"] == 0:
                return jsonify(error="This feature has no keywords to search for."), 400

    slug = os.environ.get("WORKFLOW_SLUG")
    if not slug or not os.environ.get("RENDER_API_KEY"):
        return jsonify(
            error="Crawl triggering isn't configured — set RENDER_API_KEY and "
                  "WORKFLOW_SLUG on the dashboard service."
        ), 503
    try:
        from render_sdk import Render
    except Exception:
        return jsonify(error="render_sdk is not installed on the dashboard service."), 503

    task_slug = f"{slug}/run_crawl"

    def _trigger():
        try:
            Render().workflows.run_task(task_slug, [feature_id, domains])
        except Exception:
            app.logger.exception("Failed to trigger crawl for feature %s", feature_id)

    # Fire-and-forget: the crawl runs on the workflow service and streams its
    # results into the DB; the dashboard will reflect them on refresh.
    threading.Thread(target=_trigger, daemon=True).start()
    return jsonify(status="started", feature=feature["name"], domains=len(domains)), 202


@app.route("/api/heatmap")
def api_heatmap():
    """
    Returns a matrix of url × keyword counts (only rows/cols with any hit).
    Shape: { urls: [...], keywords: [...], matrix: [[count, ...], ...] }
    """
    f_cond, f_params = _feature_filter()
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Only URLs that had at least one hit
            cur.execute(f"""
                SELECT DISTINCT url
                FROM scrape_results
                WHERE count > 0 AND {f_cond}
                ORDER BY url
            """, f_params)
            urls = [r["url"] for r in cur.fetchall()]

            # Only keywords that had at least one hit
            cur.execute(f"""
                SELECT DISTINCT keyword
                FROM scrape_results
                WHERE count > 0 AND {f_cond}
                ORDER BY keyword
            """, f_params)
            keywords = [r["keyword"] for r in cur.fetchall()]

            if not urls or not keywords:
                return jsonify(urls=[], keywords=[], matrix=[])

            cur.execute("""
                SELECT url, keyword, SUM(count) AS total
                FROM scrape_results
                WHERE url = ANY(%s) AND keyword = ANY(%s)
                GROUP BY url, keyword
            """, (urls, keywords))
            rows = cur.fetchall()

    lookup = {(r["url"], r["keyword"]): int(r["total"]) for r in rows}
    matrix = [
        [lookup.get((url, kw), 0) for kw in keywords]
        for url in urls
    ]
    return jsonify(urls=urls, keywords=keywords, matrix=matrix)


# --- Feature management API ---------------------------------------------------

def _fetch_features(cur):
    """Return all features with their keywords as a list, newest first."""
    cur.execute("""
        SELECT id, name, documentation_url, created_at, updated_at
        FROM features
        ORDER BY name
    """)
    features = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT feature_id, keyword FROM feature_keywords ORDER BY keyword")
    by_feature: dict[int, list[str]] = {}
    for r in cur.fetchall():
        by_feature.setdefault(r["feature_id"], []).append(r["keyword"])
    for f in features:
        f["keywords"] = by_feature.get(f["id"], [])
    return features


@app.route("/api/features", methods=["GET"])
def api_list_features():
    with get_conn() as conn:
        with conn.cursor() as cur:
            return jsonify(_fetch_features(cur))


@app.route("/api/features", methods=["POST"])
def api_create_feature():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="Name is required"), 400
    doc_url = (data.get("documentation_url") or "").strip() or None
    keywords = _parse_keywords(data.get("keywords"))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM features WHERE LOWER(name) = LOWER(%s)", (name,))
            if cur.fetchone():
                return jsonify(error=f"A feature named “{name}” already exists"), 409
            cur.execute(
                "INSERT INTO features (name, documentation_url) VALUES (%s, %s) RETURNING id",
                (name, doc_url),
            )
            fid = cur.fetchone()["id"]
            if keywords:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO feature_keywords (feature_id, keyword) VALUES %s",
                    [(fid, kw) for kw in keywords],
                )
    return jsonify(id=fid), 201


@app.route("/api/features/<int:feature_id>", methods=["PUT"])
def api_update_feature(feature_id):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(error="Name is required"), 400
    doc_url = (data.get("documentation_url") or "").strip() or None
    keywords = _parse_keywords(data.get("keywords"))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM features WHERE id = %s", (feature_id,))
            if not cur.fetchone():
                return jsonify(error="Feature not found"), 404
            cur.execute(
                "SELECT 1 FROM features WHERE LOWER(name) = LOWER(%s) AND id <> %s",
                (name, feature_id),
            )
            if cur.fetchone():
                return jsonify(error=f"A feature named “{name}” already exists"), 409

            cur.execute(
                "UPDATE features SET name = %s, documentation_url = %s, updated_at = NOW() WHERE id = %s",
                (name, doc_url, feature_id),
            )
            # Replace keyword set.
            cur.execute("DELETE FROM feature_keywords WHERE feature_id = %s", (feature_id,))
            if keywords:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO feature_keywords (feature_id, keyword) VALUES %s",
                    [(feature_id, kw) for kw in keywords],
                )
    return jsonify(id=feature_id)


@app.route("/api/features/<int:feature_id>", methods=["DELETE"])
def api_delete_feature(feature_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM features WHERE id = %s", (feature_id,))
            if cur.rowcount == 0:
                return jsonify(error="Feature not found"), 404
    return jsonify(ok=True)


# Ensure the features schema exists whenever the app is imported (e.g. by
# gunicorn) or run directly.
init_features_schema()


if __name__ == "__main__":
    app.run(debug=True, port=5050)
