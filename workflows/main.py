import asyncio
import csv
import io
import logging
import uuid

import requests
from bs4 import BeautifulSoup
from render_sdk import Retry, Workflows

from db import init_db, save_result

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

app = Workflows(
    default_retry=Retry(max_retries=2, wait_duration_ms=2000, backoff_scaling=2.0),
    default_timeout=300,
    default_plan="standard",
)


@app.task
def scrape_url(url: str, keywords: list[str], run_id: str) -> dict:
    """Scrape a single URL and count keyword occurrences, saving results to DB."""
    logger.info(f"Scraping {url}")

    try:
        response = requests.get(
            url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator=" ").lower()
    except Exception as exc:
        logger.error(f"Failed to fetch {url}: {exc}")
        return {"url": url, "error": str(exc), "keyword_counts": {}}

    keyword_counts = {kw: text.count(kw.lower()) for kw in keywords}
    save_result(run_id=run_id, url=url, keyword_counts=keyword_counts)

    logger.info(f"Done {url}: {keyword_counts}")
    return {"url": url, "keyword_counts": keyword_counts}


@app.task
async def process_csvs(keywords_csv: str, urls_csv: str) -> dict:
    """
    Entry-point task. Accepts the raw CSV text for keywords and URLs,
    then fans out a scrape_url task for every URL in parallel.
    """
    run_id = str(uuid.uuid4())
    logger.info(f"Starting run {run_id}")

    init_db()

    def _first_value(row: dict) -> str:
        return (row.get("keyword") or row.get("url") or next(iter(row.values()), "")).strip()

    keywords = [
        row.get("keyword", "").strip() or next(iter(row.values()), "").strip()
        for row in csv.DictReader(io.StringIO(keywords_csv))
        if any(row.values())
    ]

    urls = [
        row.get("url", "").strip() or next(iter(row.values()), "").strip()
        for row in csv.DictReader(io.StringIO(urls_csv))
        if any(row.values())
    ]

    logger.info(f"{len(keywords)} keywords, {len(urls)} URLs")

    results = await asyncio.gather(*[scrape_url(url, keywords, run_id) for url in urls])

    return {
        "run_id": run_id,
        "urls_scraped": len(urls),
        "keywords_tracked": len(keywords),
        "results": list(results),
    }
