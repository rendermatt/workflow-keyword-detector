import os
import threading
from flask import Flask, jsonify, render_template, request
import psycopg2
import psycopg2.extras

from seed_data import DEFAULT_FIT_PROMPT, SEED_FEATURES

DB_URL = os.environ["DATABASE_URL"]

app = Flask(__name__)


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_schema() -> None:
    """Create the tables the dashboard reads/writes, seed a starter feature, and
    seed the default fit prompt. Idempotent — safe to run on every import."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS features (
                    id                SERIAL      PRIMARY KEY,
                    name              TEXT        NOT NULL UNIQUE,
                    documentation_url TEXT,
                    fit_prompt        TEXT,
                    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            # Per-feature prompt override (added in place for pre-existing tables).
            cur.execute("ALTER TABLE features ADD COLUMN IF NOT EXISTS fit_prompt TEXT")

            # Crawl coverage (also created by the workflow's db.init_db); ensured
            # here so the dashboard works before the first crawl runs. The
            # ALTER ... ADD COLUMN migrations run BEFORE the indexes/constraints
            # that reference them, since on a pre-existing table the CREATE above
            # is a no-op and the columns won't exist yet.
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
            cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS feature_id INTEGER")
            cur.execute("ALTER TABLE crawled_pages ADD COLUMN IF NOT EXISTS blocked_reason TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_feature ON crawled_pages (feature_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain ON crawled_pages (domain)")
            cur.execute("ALTER TABLE crawled_pages DROP CONSTRAINT IF EXISTS crawled_pages_url_key")
            cur.execute("SELECT 1 FROM pg_constraint WHERE conname = 'crawled_pages_feature_url_key'")
            if not cur.fetchone():
                cur.execute(
                    "ALTER TABLE crawled_pages "
                    "ADD CONSTRAINT crawled_pages_feature_url_key UNIQUE (feature_id, url)"
                )

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
                    error          TEXT,
                    assessed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT fit_assessments_feature_domain_key UNIQUE (feature_id, domain)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fit_assessments_feature ON fit_assessments (feature_id)")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key        TEXT        PRIMARY KEY,
                    value      TEXT        NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            # Seed a starter feature (with its own copy of the default prompt)
            # only if none exist yet.
            cur.execute("SELECT COUNT(*) AS n FROM features")
            if cur.fetchone()["n"] == 0:
                for feat in SEED_FEATURES:
                    cur.execute(
                        "INSERT INTO features (name, documentation_url, fit_prompt) "
                        "VALUES (%s, %s, %s)",
                        (feat["name"], feat.get("documentation_url"), DEFAULT_FIT_PROMPT),
                    )

            # Seed the default fit prompt only if not present.
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES ('fit_prompt', %s) "
                "ON CONFLICT (key) DO NOTHING",
                (DEFAULT_FIT_PROMPT,),
            )


# --- Pages -------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/features")
def features_page():
    return render_template("features.html")


@app.route("/prompt")
def prompt_page():
    return render_template("prompt.html")


@app.route("/docs")
def docs_page():
    """Explain what the crawler + LLM assessment does, and list the pages that
    have been crawled so far."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url,
                       bool_and(ok)       AS ok,
                       MAX(crawled_at)    AS last_crawled
                FROM crawled_pages
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


# --- Fit APIs ----------------------------------------------------------------

@app.route("/api/summary")
def api_summary():
    """Headline numbers for the selected feature."""
    fid = _selected_feature_id()
    if fid is None:
        return jsonify(customers=0, strong=0, avg_score=None, last_run_at=None)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE fit_score IS NOT NULL)       AS customers,
                       COUNT(*) FILTER (WHERE fit_score >= 75)             AS strong,
                       AVG(fit_score)                                      AS avg_score,
                       MAX(assessed_at)                                    AS last_run
                FROM fit_assessments
                WHERE feature_id = %s
            """, (fid,))
            r = cur.fetchone()
    return jsonify(
        customers=r["customers"],
        strong=r["strong"],
        avg_score=round(float(r["avg_score"]), 1) if r["avg_score"] is not None else None,
        last_run_at=r["last_run"].isoformat() if r["last_run"] else None,
    )


@app.route("/api/fit")
def api_fit():
    """All fit assessments for the selected feature, best fit first."""
    fid = _selected_feature_id()
    if fid is None:
        return jsonify([])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT domain, fit_score, tier, summary, signals, recommendation,
                       pages_analyzed, model, error, assessed_at
                FROM fit_assessments
                WHERE feature_id = %s
                ORDER BY fit_score DESC NULLS LAST, domain
            """, (fid,))
            rows = cur.fetchall()
    return jsonify([
        {**dict(r), "assessed_at": r["assessed_at"].isoformat() if r["assessed_at"] else None}
        for r in rows
    ])


@app.route("/api/domains/<path:domain>/pages")
def api_domain_pages(domain):
    """Every page crawled for a domain (for the selected feature) and its status."""
    fid = _selected_feature_id()
    if fid is None:
        return jsonify([])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url, ok, status_code, blocked_reason, crawled_at
                FROM crawled_pages
                WHERE feature_id = %s AND domain = %s
                ORDER BY url
            """, (fid, domain))
            rows = cur.fetchall()
    return jsonify([
        {**dict(r), "crawled_at": r["crawled_at"].isoformat() if r["crawled_at"] else None}
        for r in rows
    ])


@app.route("/api/crawl", methods=["POST"])
def api_crawl():
    """Kick off a crawl + fit assessment for a feature over a list of domains by
    triggering the workflow's run_crawl task."""
    data = request.get_json(silent=True) or {}
    try:
        feature_id = int(data.get("feature_id"))
    except (TypeError, ValueError):
        return jsonify(error="Select a feature to assess."), 400
    domains = [
        d.strip() for d in (data.get("domains") or [])
        if isinstance(d, str) and d.strip()
    ]
    if not domains:
        return jsonify(error="Provide at least one domain (upload a CSV)."), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name, documentation_url FROM features WHERE id = %s", (feature_id,))
            feature = cur.fetchone()
            if not feature:
                return jsonify(error="Feature not found."), 404
            if not feature["documentation_url"]:
                return jsonify(
                    error="This feature needs a documentation URL before it can be assessed."
                ), 400

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

    # Fire-and-forget: the crawl + assessment runs on the workflow service and
    # streams results into the DB; the dashboard reflects them on refresh.
    threading.Thread(target=_trigger, daemon=True).start()
    return jsonify(status="started", feature=feature["name"], domains=len(domains)), 202


# --- Feature management API --------------------------------------------------

def _default_prompt(cur):
    """The editable global default prompt (fallback for features without one)."""
    cur.execute("SELECT value FROM app_settings WHERE key = 'fit_prompt'")
    row = cur.fetchone()
    return row["value"] if row else DEFAULT_FIT_PROMPT


def _fetch_features(cur):
    cur.execute("""
        SELECT id, name, documentation_url, fit_prompt, created_at, updated_at
        FROM features
        ORDER BY name
    """)
    return [dict(r) for r in cur.fetchall()]


@app.route("/api/features", methods=["GET"])
def api_list_features():
    with get_conn() as conn:
        with conn.cursor() as cur:
            return jsonify(_fetch_features(cur))


@app.route("/api/features", methods=["POST"])
def api_create_feature():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    doc_url = (data.get("documentation_url") or "").strip()
    prompt = (data.get("fit_prompt") or "").strip()
    if not name:
        return jsonify(error="Name is required"), 400
    if not doc_url:
        return jsonify(error="A documentation URL is required — it's what fit is judged against"), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM features WHERE LOWER(name) = LOWER(%s)", (name,))
            if cur.fetchone():
                return jsonify(error=f"A feature named “{name}” already exists"), 409
            # Blank prompt → start from the global default so every feature has one.
            cur.execute(
                "INSERT INTO features (name, documentation_url, fit_prompt) "
                "VALUES (%s, %s, %s) RETURNING id",
                (name, doc_url, prompt or _default_prompt(cur)),
            )
            fid = cur.fetchone()["id"]
    return jsonify(id=fid), 201


@app.route("/api/features/<int:feature_id>", methods=["PUT"])
def api_update_feature(feature_id):
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    doc_url = (data.get("documentation_url") or "").strip()
    prompt = (data.get("fit_prompt") or "").strip()
    if not name:
        return jsonify(error="Name is required"), 400
    if not doc_url:
        return jsonify(error="A documentation URL is required — it's what fit is judged against"), 400

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
                "UPDATE features SET name = %s, documentation_url = %s, "
                "fit_prompt = %s, updated_at = NOW() WHERE id = %s",
                (name, doc_url, prompt or _default_prompt(cur), feature_id),
            )
    return jsonify(id=feature_id)


@app.route("/api/features/<int:feature_id>", methods=["DELETE"])
def api_delete_feature(feature_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM features WHERE id = %s", (feature_id,))
            if cur.rowcount == 0:
                return jsonify(error="Feature not found"), 404
            # No FK cascade — clean up the feature's derived data explicitly.
            cur.execute("DELETE FROM fit_assessments WHERE feature_id = %s", (feature_id,))
            cur.execute("DELETE FROM crawled_pages WHERE feature_id = %s", (feature_id,))
    return jsonify(ok=True)


# --- Prompt settings API -----------------------------------------------------

@app.route("/api/settings/prompt", methods=["GET"])
def api_get_prompt():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = 'fit_prompt'")
            row = cur.fetchone()
    prompt = row["value"] if row else DEFAULT_FIT_PROMPT
    return jsonify(prompt=prompt, default=DEFAULT_FIT_PROMPT)


@app.route("/api/settings/prompt", methods=["PUT"])
def api_set_prompt():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify(error="The prompt can't be empty"), 400
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app_settings (key, value) VALUES ('fit_prompt', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
                (prompt,),
            )
    return jsonify(ok=True)


# Ensure the schema exists whenever the app is imported (e.g. by gunicorn).
init_schema()


if __name__ == "__main__":
    app.run(debug=True, port=5050)
