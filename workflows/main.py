import asyncio
import csv
import logging
import os
import uuid
from pathlib import Path

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


def _read_csv_column(filename: str, column: str) -> list[str]:
    """Read a single column from a CSV file in the workflows root directory."""
    path = Path(__file__).parent / filename
    with path.open() as f:
        return [
            row.get(column, "").strip() or next(iter(row.values()), "").strip()
            for row in csv.DictReader(f)
            if any(row.values())
        ]


@app.task
async def process_csvs() -> dict:
    """
    Entry-point task. Reads keyword and URL CSVs from filenames set in
    KEYWORDS_CSV and URLS_CSV environment variables (resolved relative to
    the workflows root directory), then fans out a scrape_url task per URL.
    """
    keywords_file = os.environ["KEYWORDS_CSV"]
    urls_file = os.environ["URLS_CSV"]

    run_id = str(uuid.uuid4())
    logger.info(f"Starting run {run_id} (keywords={keywords_file}, urls={urls_file})")

    init_db()

    keywords = _read_csv_column(keywords_file, "keyword")
    urls = _read_csv_column(urls_file, "url")

    logger.info(f"{len(keywords)} keywords, {len(urls)} URLs")

    results = await asyncio.gather(*[scrape_url(url, keywords, run_id) for url in urls])

    return {
        "run_id": run_id,
        "urls_scraped": len(urls),
        "keywords_tracked": len(keywords),
        "results": list(results),
    }


if __name__ == "__main__":
    app.start()  # required — registers tasks with Render and starts the task server
