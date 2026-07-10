#!/usr/bin/env python3
"""
Trigger a crawl + fit assessment for a feature over a list of domains (CLI
equivalent of the dashboard's "New Crawl"). Each site is crawled and scored by
the LLM against the feature's documentation.

Usage:
    python trigger.py --feature-id 1 --domains-csv domains.csv

Required environment variables:
    RENDER_API_KEY   Your Render API key (from dashboard.render.com/u/settings)
    WORKFLOW_SLUG    Slug of your deployed Workflow service (e.g. "keyword-scraper")
"""

import argparse
import csv
import json
import os
import sys

from render_sdk import Render


def _read_domains(path: str) -> list[str]:
    """Read domains from a CSV: a url/domain column if present, else column 0."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    col = header.index("url") if "url" in header else (
        header.index("domain") if "domain" in header else None
    )
    data = rows[1:] if col is not None else rows
    idx = col if col is not None else 0
    seen, out = set(), []
    for row in data:
        if idx < len(row):
            value = row[idx].strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                out.append(value)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Kick off a crawl for a feature.")
    parser.add_argument("--feature-id", type=int, required=True)
    parser.add_argument("--domains-csv", required=True, help="CSV file of domains")
    args = parser.parse_args()

    workflow_slug = os.environ.get("WORKFLOW_SLUG")
    if not workflow_slug:
        print("Error: WORKFLOW_SLUG environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    domains = _read_domains(args.domains_csv)
    if not domains:
        print(f"Error: no domains found in {args.domains_csv}.", file=sys.stderr)
        sys.exit(1)

    task_slug = f"{workflow_slug}/run_crawl"
    print(f"Triggering {task_slug} for feature {args.feature_id} over {len(domains)} domains ...")

    render = Render()
    result = render.workflows.run_task(task_slug, [args.feature_id, domains])

    print(json.dumps(result.results, indent=2))


if __name__ == "__main__":
    main()
