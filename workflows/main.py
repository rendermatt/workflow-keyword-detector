import asyncio
import csv
import logging
import os
import uuid
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from render_sdk import Retry, Workflows

from db import init_db, record_page, save_result

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Max pages to fetch per seed domain in one crawl (tune via env var).
CRAWL_MAX_PAGES = int(os.environ.get("CRAWL_MAX_PAGES", "50"))
REQUEST_TIMEOUT = int(os.environ.get("CRAWL_REQUEST_TIMEOUT", "15"))

# File extensions that aren't crawlable HTML pages.
_SKIP_EXTENSIONS = (
    ".pdf", ".zip", ".gz", ".dmg", ".exe", ".css", ".js", ".json", ".xml",
    ".rss", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".mov", ".woff", ".woff2", ".ttf", ".eot",
)

app = Workflows(
    default_retry=Retry(max_retries=2, wait_duration_ms=2000, backoff_scaling=2.0),
    default_timeout=300,
    default_plan="standard",
)


def _ensure_scheme(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else "https://" + url


def _base_domain(host: str) -> str:
    """Registrable domain = last two labels of the host (e.g. docs.foo.ai -> foo.ai).

    A deliberately lightweight heuristic; it does not special-case multi-part
    public suffixes like .co.uk, but those don't appear in our seed list.
    """
    host = (host or "").lower().split(":")[0]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    return ".".join(parts[-2:])


def _same_domain(url: str, base: str) -> bool:
    try:
        return _base_domain(urlparse(url).netloc) == base
    except ValueError:
        return False


def _is_crawlable(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    return not parsed.path.lower().endswith(_SKIP_EXTENSIONS)


def _extract_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Absolute, fragment-stripped links from a page's <a href> tags."""
    links = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        absolute, _ = urldefrag(urljoin(page_url, href))
        links.append(absolute)
    return links


@app.task
def crawl_domain(seed_url: str, keywords: list[str], run_id: str) -> dict:
    """Crawl a seed URL and every same-domain link reachable from it (BFS),
    counting keyword occurrences on each page and recording crawl coverage.
    Bounded by CRAWL_MAX_PAGES."""
    seed = _ensure_scheme(seed_url.strip())
    base = _base_domain(urlparse(seed).netloc)
    logger.info(f"Crawling {base} from {seed} (max {CRAWL_MAX_PAGES} pages)")

    session = requests.Session()
    session.headers.update(
        {"User-Agent": "Mozilla/5.0 (compatible; RenderKeywordBot/1.0)"}
    )

    enqueued = {seed}
    queue: deque[str] = deque([seed])
    pages_crawled = 0
    pages_ok = 0

    while queue and pages_crawled < CRAWL_MAX_PAGES:
        url = queue.popleft()
        pages_crawled += 1

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            status = response.status_code
            response.raise_for_status()
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.warning(f"Failed to fetch {url}: {exc}")
            record_page(run_id, base, url, ok=False, status_code=status)
            continue

        if "html" not in response.headers.get("Content-Type", "").lower():
            record_page(run_id, base, url, ok=True, status_code=status)
            pages_ok += 1
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        text = soup.get_text(separator=" ").lower()

        keyword_counts = {kw: text.count(kw.lower()) for kw in keywords}
        save_result(run_id=run_id, url=url, keyword_counts=keyword_counts)
        record_page(run_id, base, url, ok=True, status_code=status)
        pages_ok += 1

        for link in _extract_links(soup, url):
            if (
                link not in enqueued
                and _same_domain(link, base)
                and _is_crawlable(link)
            ):
                enqueued.add(link)
                queue.append(link)

    logger.info(f"Crawled {pages_crawled} pages for {base} ({pages_ok} ok)")
    return {
        "domain": base,
        "seed": seed,
        "pages_crawled": pages_crawled,
        "pages_ok": pages_ok,
    }


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
    the workflows root directory), then fans out a crawl_domain task per seed
    URL. Each task crawls that seed's whole domain (same-domain links).
    """
    keywords_file = os.environ["KEYWORDS_CSV"]
    urls_file = os.environ["URLS_CSV"]

    run_id = str(uuid.uuid4())
    logger.info(f"Starting run {run_id} (keywords={keywords_file}, urls={urls_file})")

    if not os.environ.get("LOCAL_OUTPUT_CSV"):
        init_db()

    keywords = _read_csv_column(keywords_file, "keyword")
    seeds = _read_csv_column(urls_file, "url")

    logger.info(f"{len(keywords)} keywords, {len(seeds)} seed URLs")

    results = await asyncio.gather(
        *[crawl_domain(seed, keywords, run_id) for seed in seeds]
    )

    return {
        "run_id": run_id,
        "seeds": len(seeds),
        "pages_crawled": sum(r["pages_crawled"] for r in results),
        "keywords_tracked": len(keywords),
        "results": list(results),
    }


if __name__ == "__main__":
    app.start()  # required — registers tasks with Render and starts the task server
