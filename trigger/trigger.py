#!/usr/bin/env python3
"""
Trigger the keyword scraper workflow.

Usage:
    python trigger.py <keywords.csv> <urls.csv>

Required environment variables:
    RENDER_API_KEY   Your Render API key (from dashboard.render.com/u/settings)
    WORKFLOW_SLUG    Slug of your deployed Workflow service (e.g. "keyword-scraper")
"""

import argparse
import json
import os
import sys
from pathlib import Path

from render_sdk import Render


def main() -> None:
    parser = argparse.ArgumentParser(description="Trigger the keyword scraper workflow")
    parser.add_argument("keywords_csv", help="Path to keywords CSV (one keyword per row, column header: 'keyword')")
    parser.add_argument("urls_csv", help="Path to URLs CSV (one URL per row, column header: 'url')")
    args = parser.parse_args()

    keywords_csv = Path(args.keywords_csv).read_text()
    urls_csv = Path(args.urls_csv).read_text()

    workflow_slug = os.environ.get("WORKFLOW_SLUG")
    if not workflow_slug:
        print("Error: WORKFLOW_SLUG environment variable is not set.", file=sys.stderr)
        print("Set it to the slug of your Render Workflow service, e.g. 'keyword-scraper'.", file=sys.stderr)
        sys.exit(1)

    task_slug = f"{workflow_slug}/process_csvs"
    print(f"Triggering {task_slug} ...")

    render = Render()
    result = render.workflows.run_task(task_slug, [keywords_csv, urls_csv])

    print(json.dumps(result.results, indent=2))


if __name__ == "__main__":
    main()
