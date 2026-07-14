import asyncio
import hashlib
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

from db import (
    get_feature,
    get_setting,
    init_db,
    record_page,
    save_doc_brief,
    save_fit_assessment,
    save_keyword_hits,
    save_keywords,
)

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

# Fallback for the editable global distillation prompt (app_settings key
# 'distill_prompt'). Same for every feature — only {{DOCUMENTATION}} differs.
DISTILL_PROMPT = """\
The text below is documentation for a product feature called "{{FEATURE_NAME}}". \
Distill it into a concise, self-contained brief that another analyst will use to \
judge whether a company is a good fit for this feature — without ever seeing the \
original documentation.

Cover, in this order:
- What the feature does, in 2-3 sentences.
- The kinds of companies, teams, technical stacks, or use cases it is built for.
- Concrete signals on a company's website that would indicate a STRONG fit.
- Signals that would indicate a WEAK or poor fit.

Be specific and compact — aim for under ~400 words. Output the brief as plain text \
with no preamble.

<documentation>
{{DOCUMENTATION}}
</documentation>"""

# Fallback for the editable global keyword-generation prompt (app_settings key
# 'keyword_prompt'). Same for every feature — only {{DOCUMENTATION}} differs.
KEYWORD_PROMPT = """\
Generate a CSV with a single column "keyword" of terms I should scrape a company's \
website for to see whether they would be a good fit for the "{{FEATURE_NAME}}" \
feature, based on the documentation below.

Output only the CSV: a "keyword" header on the first line, then one keyword per \
line — no explanation and no code fences.

<documentation>
{{DOCUMENTATION}}
</documentation>"""

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


def _distill_docs(feature_name: str, doc_content: str, template: str) -> str:
    """One LLM call: compress the raw documentation into a compact fit brief,
    using the editable global distillation prompt."""
    prompt = template.replace("{{FEATURE_NAME}}", feature_name or "").replace(
        "{{DOCUMENTATION}}", doc_content
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=FIT_MODEL,
        max_tokens=2000,
        output_config={"effort": "low"},  # summarization — cheap/fast
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(f"distillation returned no text (stop_reason={response.stop_reason})")
    return text.strip()


# doc_brief_hash sentinel meaning "a human edited this brief — use it verbatim".
_MANUAL_BRIEF = "manual"


def _resolve_doc_brief(feature: dict, doc_content: str) -> str:
    """Return the feature's documentation brief, distilling it only when needed.
    The result is what each per-company call sees as the documentation — a small
    brief instead of the full page.

    A manually-edited brief (hash sentinel 'manual') is authoritative and used
    verbatim; otherwise the auto brief is reused while the docs are unchanged and
    re-distilled when they change.
    """
    if feature.get("doc_brief") and feature.get("doc_brief_hash") == _MANUAL_BRIEF:
        logger.info("Using manually-edited documentation brief for feature %s", feature["id"])
        return feature["doc_brief"]
    if not doc_content.strip():
        return ""
    # Fold the distillation prompt into the cache key so editing that prompt
    # re-distills on the next crawl (not only when the docs themselves change).
    distill_template = get_setting("distill_prompt") or DISTILL_PROMPT
    content_hash = hashlib.sha256(
        (distill_template + "\x00" + doc_content).encode("utf-8")
    ).hexdigest()
    if feature.get("doc_brief") and feature.get("doc_brief_hash") == content_hash:
        logger.info("Reusing cached documentation brief for feature %s", feature["id"])
        return feature["doc_brief"]
    try:
        brief = _distill_docs(feature["name"], doc_content, distill_template)
    except Exception:
        # Fall back to the raw docs so assessments still run.
        logger.exception("Documentation distillation failed; falling back to raw docs")
        return doc_content
    save_doc_brief(feature["id"], brief, content_hash)
    logger.info("Distilled documentation brief for feature %s (%d chars)", feature["id"], len(brief))
    return brief


def _parse_keyword_csv(text: str) -> list[str]:
    """Parse the model's CSV-ish output into a de-duplicated keyword list."""
    seen, out = set(), []
    for line in (text or "").splitlines():
        v = line.strip()
        if not v or v.startswith("```"):  # skip blanks and code fences
            continue
        if "," in v:  # single-column CSV, but be tolerant of extra columns
            v = v.split(",", 1)[0]
        v = v.strip().strip('"').lstrip("-*•").strip()
        if not v or v.lower() in ("keyword", "keywords"):  # skip header
            continue
        key = v.lower()
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out[:200]


def _generate_keywords(feature_name: str, doc_content: str, template: str) -> list[str]:
    """One LLM call: derive scrape keywords from the documentation."""
    prompt = template.replace("{{FEATURE_NAME}}", feature_name or "").replace(
        "{{DOCUMENTATION}}", doc_content
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model=FIT_MODEL,
        max_tokens=2000,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse_keyword_csv(text)


def _resolve_keywords(feature: dict, doc_content: str) -> list[str]:
    """Return the feature's scrape keywords. Keywords are sticky: once a feature
    has any — auto-generated or hand-edited in the dashboard — they're reused
    verbatim. They're only (re)generated when the field is empty."""
    existing = feature.get("keywords")
    if existing:
        logger.info("Using existing keywords for feature %s", feature["id"])
        return existing
    if not doc_content.strip():
        return []
    kw_template = get_setting("keyword_prompt") or KEYWORD_PROMPT
    try:
        keywords = _generate_keywords(feature["name"], doc_content, kw_template)
    except Exception:
        logger.exception("Keyword generation failed")
        return []
    save_keywords(feature["id"], keywords)
    logger.info("Generated %d keywords for feature %s", len(keywords), feature["id"])
    return keywords


def _allocate_char_budget(lengths: list[int], budget: int) -> list[int]:
    """Split a total character budget across pages so every page gets a fair
    share and none is dropped. Water-filling: each round gives every still-hungry
    page an equal slice of the remaining budget, capped at its actual length, so
    short pages take only what they need and the slack flows to longer pages.
    """
    n = len(lengths)
    allocs = [0] * n
    if n == 0 or budget <= 0:
        return allocs
    hungry = [i for i in range(n) if lengths[i] > 0]
    remaining = budget
    while remaining > 0 and hungry:
        share = max(1, remaining // len(hungry))
        still = []
        for i in hungry:
            give = min(share, lengths[i] - allocs[i], remaining)
            allocs[i] += give
            remaining -= give
            if allocs[i] < lengths[i] and remaining > 0:
                still.append(i)
            if remaining <= 0:
                break
        if len(still) == len(hungry) and share == 0:
            break  # budget < number of hungry pages; can't split further
        hungry = still
    return allocs


def _crawl_site(seed_url: str, run_id: str, feature_id: int, keywords: list[str]) -> dict:
    """Crawl a seed URL and same-domain links (BFS), recording coverage in
    crawled_pages and gathering visible page text. The CRAWL_CONTENT_CHARS budget
    is divided fairly across all crawled pages (see _allocate_char_budget) so no
    page is dropped just because earlier pages were long.

    Returns {domain, pages_crawled, pages_ok, pages_skipped_robots, text,
    keyword_counts}.
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
    # Case-insensitive substring counts across the whole site, per keyword.
    lowered_keywords = [(kw, kw.lower()) for kw in keywords if kw.strip()]
    keyword_counts: dict[str, int] = {}
    # (url, visible text, status) for each HTML page — trimmed to the LLM budget
    # once all pages are known, so the budget is shared fairly across them.
    pages: list[tuple[str, str, int]] = []

    while queue and pages_crawled < CRAWL_MAX_PAGES:
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
        pages_ok += 1

        # Count keywords on the full page text (independent of the LLM budget).
        if text and lowered_keywords:
            lowered = text.lower()
            for kw, kwl in lowered_keywords:
                c = lowered.count(kwl)
                if c:
                    keyword_counts[kw] = keyword_counts.get(kw, 0) + c

        # Defer recording coverage (and the per-page char count) until the budget
        # is split below. Store at most a whole budget's worth to bound memory.
        pages.append((url, (text or "")[:CRAWL_CONTENT_CHARS], status))

        for link in _extract_links(soup, url):
            if (
                link not in enqueued
                and _same_domain(link, base)
                and _is_crawlable(link)
            ):
                enqueued.add(link)
                queue.append(link)

    # Share the budget across pages, then build the bundle and record per-page
    # coverage with how many characters each page contributed.
    allocs = _allocate_char_budget([len(t) for _, t, _ in pages], CRAWL_CONTENT_CHARS)
    chunks: list[str] = []
    total_chars = 0
    for (url, text, status), n_chars in zip(pages, allocs):
        snippet = text[:n_chars]
        chunks.append(f"\n\n## {url}\n{snippet}")
        total_chars += len(snippet)
        record_page(
            run_id, feature_id, base, url, ok=True,
            status_code=status, content_chars=len(snippet),
        )

    logger.info(
        f"Crawled {pages_crawled} pages for {base} "
        f"({pages_ok} ok, {pages_skipped_robots} skipped by robots.txt, "
        f"{total_chars} chars gathered across {len(pages)} pages)"
    )
    return {
        "domain": base,
        "pages_crawled": pages_crawled,
        "pages_ok": pages_ok,
        "pages_skipped_robots": pages_skipped_robots,
        "text": "".join(chunks).strip(),
        "keyword_counts": keyword_counts,
    }


# Placeholders whose value changes per company (everything before the first one
# is identical across a run, so it can be prompt-cached).
_CUSTOMER_TOKENS = ("{{CUSTOMER_DOMAIN}}", "{{CRAWLED_CONTENT}}")


def _build_user_content(
    template: str, feature_name: str, doc_brief: str, domain: str, content: str
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
        .replace("{{FEATURE_DOCUMENTATION}}", doc_brief or "(no documentation could be fetched)")
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
    feature_name: str, doc_brief: str, domain: str, content: str, prompt_template: str
) -> dict:
    """Call Claude to score how well the crawled company fits the feature."""
    content_blocks = _build_user_content(
        prompt_template, feature_name, doc_brief, domain, content
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
    doc_brief: str,
    prompt_template: str,
    keywords: list[str],
) -> dict:
    """Crawl one prospect domain (counting keywords), then ask Claude how well it
    fits the feature. Records crawl coverage, keyword hits, and a fit_assessments
    row."""
    crawl = _crawl_site(seed_url, run_id, feature_id, keywords)
    domain = crawl["domain"]

    # Replace this company's keyword tallies with the current run's counts.
    save_keyword_hits(feature_id, run_id, domain, crawl["keyword_counts"])

    assessment = {
        "run_id": run_id,
        "feature_id": feature_id,
        "domain": domain,
        "pages_analyzed": crawl["pages_ok"],
        "content_chars": len(crawl["text"]),
    }
    try:
        assessment.update(
            _assess_fit(feature_name, doc_brief, domain, crawl["text"], prompt_template)
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

    # Fetch the docs once (HTTP), then — in the same setup step — distill them
    # into a brief and generate scrape keywords. Both are single LLM calls cached
    # across runs, so unchanged docs/prompts aren't reprocessed.
    doc_content = _fetch_doc_content(feature["documentation_url"])
    doc_brief = _resolve_doc_brief(feature, doc_content)
    keywords = _resolve_keywords(feature, doc_content)
    logger.info(
        f"Starting run {run_id} for feature {feature_id} ({feature['name']}): "
        f"{len(seeds)} seed domains, {len(doc_content)} chars of docs → "
        f"{len(doc_brief)} char brief, {len(keywords)} keywords"
    )

    results = await asyncio.gather(
        *[
            assess_domain(
                seed, feature_id, run_id, feature["name"], doc_brief,
                prompt_template, keywords,
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
