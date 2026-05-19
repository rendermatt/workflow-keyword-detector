import os
from flask import Flask, jsonify, render_template
import psycopg2
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]

app = Flask(__name__)


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/runs")
def api_runs():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id,
                       MIN(scraped_at) AS started_at,
                       MAX(scraped_at) AS finished_at,
                       COUNT(DISTINCT url) AS urls_scraped,
                       COUNT(DISTINCT keyword) AS keywords_tracked,
                       SUM(count) AS total_hits
                FROM scrape_results
                GROUP BY run_id
                ORDER BY started_at DESC
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/summary")
def api_summary():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(DISTINCT run_id) FROM scrape_results")
            total_runs = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(DISTINCT url) FROM scrape_results")
            total_urls = cur.fetchone()["count"]
            cur.execute("SELECT COUNT(DISTINCT keyword) FROM scrape_results")
            total_keywords = cur.fetchone()["count"]
            cur.execute("SELECT SUM(count) FROM scrape_results")
            total_hits = cur.fetchone()["sum"] or 0
    return jsonify(
        total_runs=total_runs,
        total_urls=total_urls,
        total_keywords=total_keywords,
        total_hits=total_hits,
    )


@app.route("/api/keywords")
def api_keywords():
    """Top keywords by total hit count across all runs."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT keyword, SUM(count) AS total
                FROM scrape_results
                GROUP BY keyword
                ORDER BY total DESC, keyword
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/urls")
def api_urls():
    """Top URLs by total hit count across all runs."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url, SUM(count) AS total
                FROM scrape_results
                GROUP BY url
                ORDER BY total DESC, url
                LIMIT 30
            """)
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/heatmap")
def api_heatmap():
    """
    Returns a matrix of url × keyword counts (only rows/cols with any hit).
    Shape: { urls: [...], keywords: [...], matrix: [[count, ...], ...] }
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Only URLs that had at least one hit
            cur.execute("""
                SELECT DISTINCT url
                FROM scrape_results
                WHERE count > 0
                ORDER BY url
            """)
            urls = [r["url"] for r in cur.fetchall()]

            # Only keywords that had at least one hit
            cur.execute("""
                SELECT DISTINCT keyword
                FROM scrape_results
                WHERE count > 0
                ORDER BY keyword
            """)
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


@app.route("/api/run_comparison")
def api_run_comparison():
    """Per-keyword totals broken out by run_id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT run_id, MIN(scraped_at) AS started_at
                FROM scrape_results
                GROUP BY run_id
                ORDER BY started_at
            """)
            runs = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT keyword, run_id, SUM(count) AS total
                FROM scrape_results
                GROUP BY keyword, run_id
                ORDER BY keyword
            """)
            rows = cur.fetchall()

    run_ids = [r["run_id"] for r in runs]
    run_labels = {
        r["run_id"]: r["started_at"].strftime("%b %d %H:%M") for r in runs
    }

    by_keyword: dict[str, dict] = {}
    for r in rows:
        kw = r["keyword"]
        if kw not in by_keyword:
            by_keyword[kw] = {rid: 0 for rid in run_ids}
        by_keyword[kw][r["run_id"]] = int(r["total"])

    # Only keywords with at least one hit in any run
    active = {kw: v for kw, v in by_keyword.items() if sum(v.values()) > 0}
    # Sort by total descending
    active = dict(sorted(active.items(), key=lambda x: -sum(x[1].values())))

    return jsonify(
        run_ids=run_ids,
        run_labels=run_labels,
        keywords=list(active.keys()),
        data=active,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5050)
