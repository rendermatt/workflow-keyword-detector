import asyncio
import json
import logging
import os
import uuid
from collections import deque
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import anthropic
import requests
from bs4 import BeautifulSoup
from render_sdk import Retry, Workflows

from db import get_feature, get_setting, init_db, record_page, save_fit_assessment

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Max pages to fetch per seed domain in one crawl (tune via env var).
CRAWL_MAX_PAGES = int(os.environ.get("CRAWL_MAX_PAGES", "50"))
REQUEST_TIMEOUT = int(os.environ.get("CRAWL_REQUEST_TIMEOUT", "15"))
USER_AGENT = "Mozilla/5.0 (compatible; RenderKeywordBot/1.0)"
# Product token matched against robots.txt User-agent groups (RobotFileParser
# keys on the part before the first "/", so pass the bare token, not USER_AGENT).
ROBOTS_AGENT = "RenderKeywordBot"

# blocked_reason value recorded when robots.txt disallows a URL.
ROBOTS_DISALLOWED = "robots-disallowed"

# Character budgets to keep the LLM prompt (and cost/latency) bounded.
CRAWL_CONTENT_CHARS = int(os.environ.get("CRAWL_CONTENT_CHARS", "60000"))
DOC_CONTENT_CHARS = int(os.environ.get("DOC_CONTENT_CHARS", "20000"))

# The model that scores fit. Defaults to Anthropic's most capable Opus tier.
FIT_MODEL = os.environ.get("FIT_MODEL", "claude-opus-4-8")

# Structured-output schema the fit assessment must conform to.
FIT_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer"},
        "tier": {"type": "string", "enum": ["strong", "promising", "weak", "unlikely"]},
        "summary": {"type": "string"},
        "signals": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
    },
    "required": ["fit_score", "tier", "summary", "signals", "recommendation"],
    "additionalProperties": False,
}

# Fallback used only if the editable base prompt is missing from app_settings.
DEFAULT_FIT_PROMPT = """\
You are a go-to-market analyst deciding whether a company would be a good fit for \
a specific product feature.

The feature you are evaluating fit for is "{{FEATURE_NAME}}".

Here is the feature's official documentation:
<feature_documentation>
{{FEATURE_DOCUMENTATION}}
</feature_documentation>

Additional guidance specific to this feature (may be empty):
<feature_specific_guidance>
{{FEATURE_INSTRUCTIONS}}
</feature_specific_guidance>

You are evaluating the company at the domain "{{CUSTOMER_DOMAIN}}". Below is text \
crawled from across their public website:
<crawled_content>
{{CRAWLED_CONTENT}}
</crawled_content>

Based only on the evidence above, assess how strong a fit this company is for the \
"{{FEATURE_NAME}}" feature. Return fit_score (0-100 integer), tier ("strong" 75-100, \
"promising" 50-74, "weak" 25-49, "unlikely" 0-24), a 2-4 sentence summary grounded in \
specific evidence, a list of concrete signals from the crawled content, and a one \
sentence recommendation for how a sales team should approach them. If the crawled \
content is empty or uninformative, return a low score and say so."""

# Shown in the base prompt when a feature provides no additional guidance.
_NO_FEATURE_INSTRUCTIONS = "(no additional guidance provided for this feature)"

# File extensions that aren't crawlable HTML pages.
_SKIP_EXTENSIONS = (
    ".pdf", ".zip", ".gz", ".dmg", ".exe", ".css", ".js", ".json", ".xml",
    ".rss", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".mp4", ".mp3", ".mov", ".woff", ".woff2", ".ttf", ".eot",
)

app = Workflows(
    default_retry=Retry(max_retries=2, wait_duration_ms=2000, backoff_scaling=2.0),
    default_timeout=600,
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


def _visible_text(html: str) -> tuple[str, BeautifulSoup]:
    """Return (visible text, parsed soup) for an HTML page, scripts/styles stripped.
    The soup is handed back so callers can also extract links without reparsing."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True), soup


# Cloudflare signals on a 403: bot mitigation vs. an Access-gated domain.
_CF_BLOCK_HEADERS = ("cf-mitigated", "cf-access-domain")


def _cf_block_reason(response) -> str | None:
    """If a 403 response carries a Cloudflare block header, return which one(s)."""
    if response is None or response.status_code != 403:
        return None
    reasons = [h for h in _CF_BLOCK_HEADERS if h in response.headers]
    return ", ".join(reasons) if reasons else None


def _robots_allows(session: requests.Session, url: str, cache: dict) -> bool:
    """Check the host's robots.txt (fetched once per host, cached) for this URL.

    Missing or unreadable robots.txt is treated as allow-all.
    """
    parsed = urlparse(url)
    host_key = parsed.netloc
    parser = cache.get(host_key)
    if parser is None:
        parser = RobotFileParser()
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            resp = session.get(robots_url, timeout=REQUEST_TIMEOUT)
            if 200 <= resp.status_code < 300:
                parser.parse(resp.text.splitlines())
            else:
                parser.parse([])  # no robots.txt -> nothing disallowed
        except Exception:
            parser.parse([])  # unreachable -> be permissive
        cache[host_key] = parser
    try:
        return parser.can_fetch(ROBOTS_AGENT, url)
    except Exception:
        return True


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    return session


def _fetch_doc_content(url: str) -> str:
    """Fetch the feature's documentation page and return its visible text,
    truncated to DOC_CONTENT_CHARS. Best-effort — returns '' on failure."""
    if not url:
        return ""
    session = _new_session()
    try:
        resp = session.get(_ensure_scheme(url), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        if "html" not in resp.headers.get("Content-Type", "").lower():
            return resp.text[:DOC_CONTENT_CHARS]
        text, _ = _visible_text(resp.text)
        return text[:DOC_CONTENT_CHARS]
    except Exception as exc:
        logger.warning("Could not fetch documentation %s: %s", url, exc)
        return ""


def _crawl_site(seed_url: str, run_id: str, feature_id: int) -> dict:
    """Crawl a seed URL and same-domain links (BFS), recording coverage in
    crawled_pages and accumulating visible page text up to CRAWL_CONTENT_CHARS.

    Returns {domain, pages_crawled, pages_ok, pages_skipped_robots, text}.
    """
    seed = _ensure_scheme(seed_url.strip())
    base = _base_domain(urlparse(seed).netloc)
    logger.info(f"Crawling {base} from {seed} (max {CRAWL_MAX_PAGES} pages)")

    session = _new_session()
    robots_cache: dict = {}
    enqueued = {seed}
    queue: deque[str] = deque([seed])
    pages_crawled = 0
    pages_ok = 0
    pages_skipped_robots = 0
    chunks: list[str] = []
    total_chars = 0

    while queue and pages_crawled < CRAWL_MAX_PAGES and total_chars < CRAWL_CONTENT_CHARS:
        url = queue.popleft()

        if not _robots_allows(session, url, robots_cache):
            logger.info(f"robots.txt disallows {url}")
            record_page(
                run_id, feature_id, base, url, ok=False,
                status_code=None, blocked_reason=ROBOTS_DISALLOWED,
            )
            pages_skipped_robots += 1
            continue

        pages_crawled += 1

        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            status = response.status_code
            response.raise_for_status()
        except Exception as exc:
            resp = getattr(exc, "response", None)
            status = getattr(resp, "status_code", None)
            blocked = _cf_block_reason(resp)
            if blocked:
                logger.warning(f"Cloudflare-blocked ({blocked}) {url}")
            else:
                logger.warning(f"Failed to fetch {url}: {exc}")
            record_page(
                run_id, feature_id, base, url, ok=False,
                status_code=status, blocked_reason=blocked,
            )
            continue

        if "html" not in response.headers.get("Content-Type", "").lower():
            record_page(run_id, feature_id, base, url, ok=True, status_code=status)
            pages_ok += 1
            continue

        text, soup = _visible_text(response.text)
        record_page(run_id, feature_id, base, url, ok=True, status_code=status)
        pages_ok += 1

        if text:
            remaining = CRAWL_CONTENT_CHARS - total_chars
            snippet = text[:remaining]
            chunks.append(f"\n\n## {url}\n{snippet}")
            total_chars += len(snippet) + len(url) + 5

        for link in _extract_links(soup, url):
            if (
                link not in enqueued
                and _same_domain(link, base)
                and _is_crawlable(link)
            ):
                enqueued.add(link)
                queue.append(link)

    logger.info(
        f"Crawled {pages_crawled} pages for {base} "
        f"({pages_ok} ok, {pages_skipped_robots} skipped by robots.txt, "
        f"{total_chars} chars gathered)"
    )
    return {
        "domain": base,
        "pages_crawled": pages_crawled,
        "pages_ok": pages_ok,
        "pages_skipped_robots": pages_skipped_robots,
        "text": "".join(chunks).strip(),
    }


# Placeholders whose value changes per company (everything before the first one
# is identical across a run, so it can be prompt-cached).
_CUSTOMER_TOKENS = ("{{CUSTOMER_DOMAIN}}", "{{CRAWLED_CONTENT}}")


def _build_user_content(
    template: str, feature_name: str, doc_content: str, domain: str, content: str
) -> list[dict]:
    """Fill the prompt template and split it into a cacheable prefix (feature
    name + documentation + instructions — identical for every company in a run)
    and a per-company suffix (this domain + its crawled text).

    Marking the prefix with cache_control means the documentation is billed at
    full price only for the first company in a run; every later company reads it
    from cache at ~10% cost, so we effectively pay for the docs once per crawl.
    """
    stable = (
        template
        .replace("{{FEATURE_NAME}}", feature_name or "")
        .replace("{{FEATURE_DOCUMENTATION}}", doc_content or "(no documentation could be fetched)")
    )

    def fill_customer(s: str) -> str:
        return (
            s.replace("{{CUSTOMER_DOMAIN}}", domain or "")
            .replace("{{CRAWLED_CONTENT}}", content or "(no content could be crawled from this site)")
        )

    positions = [p for p in (stable.find(t) for t in _CUSTOMER_TOKENS) if p != -1]
    if not positions:
        # Custom prompt with no per-company placeholders — nothing to cache.
        return [{"type": "text", "text": fill_customer(stable)}]

    split = min(positions)
    prefix, suffix = stable[:split], fill_customer(stable[split:])
    if not prefix.strip():
        return [{"type": "text", "text": suffix}]
    return [
        {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": suffix},
    ]


def _tier_for(score: int) -> str:
    """Derive the tier from the score so it's always consistent with fit_score."""
    if score >= 75:
        return "strong"
    if score >= 50:
        return "promising"
    if score >= 25:
        return "weak"
    return "unlikely"


def _assess_fit(
    feature_name: str, doc_content: str, domain: str, content: str, prompt_template: str
) -> dict:
    """Call Claude to score how well the crawled company fits the feature."""
    content_blocks = _build_user_content(
        prompt_template, feature_name, doc_content, domain, content
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=FIT_MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        # effort "medium" bounds thinking depth so the per-company assessment
        # stays cheap/fast and leaves room in max_tokens for the JSON output.
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": FIT_SCHEMA}},
        messages=[{"role": "user", "content": content_blocks}],
    )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError(f"Model returned no text (stop_reason={response.stop_reason})")
    data = json.loads(text)

    score = max(0, min(100, int(data["fit_score"])))
    return {
        "fit_score": score,
        "tier": _tier_for(score),  # normalize so tier always matches the score
        "summary": data.get("summary", ""),
        "signals": data.get("signals", []),
        "recommendation": data.get("recommendation", ""),
        "model": response.model,
    }


@app.task
def assess_domain(
    seed_url: str,
    feature_id: int,
    run_id: str,
    feature_name: str,
    doc_content: str,
    prompt_template: str,
) -> dict:
    """Crawl one prospect domain, then ask Claude how well it fits the feature.
    Records crawl coverage and upserts a fit_assessments row."""
    crawl = _crawl_site(seed_url, run_id, feature_id)
    domain = crawl["domain"]

    assessment = {
        "run_id": run_id,
        "feature_id": feature_id,
        "domain": domain,
        "pages_analyzed": crawl["pages_ok"],
    }
    try:
        assessment.update(
            _assess_fit(feature_name, doc_content, domain, crawl["text"], prompt_template)
        )
    except Exception as exc:
        logger.exception("Fit assessment failed for %s", domain)
        assessment["error"] = str(exc)[:500]

    save_fit_assessment(assessment)
    return {
        "domain": domain,
        "pages_crawled": crawl["pages_crawled"],
        "pages_ok": crawl["pages_ok"],
        "fit_score": assessment.get("fit_score"),
        "tier": assessment.get("tier"),
        "error": assessment.get("error"),
    }


@app.task
async def run_crawl(feature_id: int, domains: list[str]) -> dict:
    """Entry-point task. For one feature, fetch its documentation once, then fan
    out a per-domain crawl + LLM fit assessment across the provided seed
    domains."""
    run_id = str(uuid.uuid4())
    init_db()

    feature = get_feature(feature_id)
    if not feature:
        raise ValueError(f"Feature {feature_id} not found")
    if not feature.get("documentation_url"):
        raise ValueError(
            f"Feature {feature_id} has no documentation URL to assess fit against"
        )

    seeds = [d.strip() for d in domains if d and d.strip()]
    if not seeds:
        raise ValueError("No domains provided to crawl")

    # One global base prompt (editable in app_settings) owns the placeholders and
    # scoring. Inject this feature's additional guidance once per run — it's
    # constant across companies, so it stays inside the cacheable prefix.
    base_prompt = get_setting("fit_prompt") or DEFAULT_FIT_PROMPT
    prompt_template = base_prompt.replace(
        "{{FEATURE_INSTRUCTIONS}}",
        (feature.get("additional_prompt") or "").strip() or _NO_FEATURE_INSTRUCTIONS,
    )

    doc_content = _fetch_doc_content(feature["documentation_url"])
    logger.info(
        f"Starting run {run_id} for feature {feature_id} ({feature['name']}): "
        f"{len(seeds)} seed domains, {len(doc_content)} chars of documentation"
    )

    results = await asyncio.gather(
        *[
            assess_domain(
                seed, feature_id, run_id, feature["name"], doc_content, prompt_template
            )
            for seed in seeds
        ]
    )

    return {
        "run_id": run_id,
        "feature_id": feature_id,
        "feature": feature["name"],
        "seeds": len(seeds),
        "pages_crawled": sum(r["pages_crawled"] for r in results),
        "results": list(results),
    }


if __name__ == "__main__":
    app.start()  # required — registers tasks with Render and starts the task server
