#!/usr/bin/env python3
"""
Trigger the keyword scraper workflow.

Usage:
    python trigger.py

Required environment variables:
    RENDER_API_KEY   Your Render API key (from dashboard.render.com/u/settings)
    WORKFLOW_SLUG    Slug of your deployed Workflow service (e.g. "keyword-scraper")

The CSV filenames are configured on the workflow service itself via:
    KEYWORDS_CSV     e.g. "keywords.csv"
    URLS_CSV         e.g. "urls.csv"
"""

import json
import os
import sys

from render_sdk import Render


def main() -> None:
    workflow_slug = os.environ.get("WORKFLOW_SLUG")
    if not workflow_slug:
        print("Error: WORKFLOW_SLUG environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    task_slug = f"{workflow_slug}/process_csvs"
    print(f"Triggering {task_slug} ...")

    render = Render()
    result = render.workflows.run_task(task_slug, [])

    print(json.dumps(result.results, indent=2))


if __name__ == "__main__":
    main()
