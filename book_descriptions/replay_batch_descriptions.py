"""Recover book descriptions from previously completed Firecrawl batches.

Only GETs saved Firecrawl results. Never launches a new scrape, or replaces
descriptions previously entered into Google Sheets.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from batch_backfill import FirecrawlBatch, canonical_url, firecrawl_description
from collector import DEFAULT_SHEET_ID, Sheets, prepare
from extraction import Extraction, normalise_text, plausible

LOG = logging.getLogger("book_descriptions.replay")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", action="append", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    creds = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not creds or not key:
        raise RuntimeError("Missing Google Sheets or Firecrawl secret")
    sheets = Sheets(os.getenv("SPREADSHEET_ID", DEFAULT_SHEET_ID), creds)
    books, _ = prepare(sheets, args.dry_run)
    pending = defaultdict(list)
    for row in books:
        if not row.description:
            pending[canonical_url(row.url)].append(row)
    updates = []
    stats = Counter()
    visited = set()

    def on_document(doc):
        stats["retrieved"] += 1
        parsed = firecrawl_description(doc)
        if not parsed:
            stats["still_rejected"] += 1
            return
        canon, description, url = parsed
        if canon in visited:
            stats["duplicates"] += 1
            return
        visited.add(canon)
        rows = pending.pop(canon, [])
        if not rows:
            stats["already_filled_or_not_in_sheet"] += 1
            return
        clean = normalise_text(description, 1000)
        if not plausible(clean):
            stats["not_plausible"] += 1
            return
        for row in rows:
            updates.append((row, Extraction(clean, url, "cached_firecrawl")))
        stats["recovered_book_urls"] += 1

    client = FirecrawlBatch(key)
    try:
        for job_id in args.job:
            state = client.poll(job_id, on_document,
                                deadline=time.monotonic() + 240, interval=2)
            LOG.info("Replay batch %s: %s", job_id, state)
            stats["jobs_replayed"] += 1
    finally:
        client.close()

    for start in range(0, len(updates), 40):
        stats["written"] += sheets.flush(updates[start:start + 40], args.dry_run)
    report = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "jobs": args.job, "stats": dict(stats), "dry_run": args.dry_run}
    Path("replay-book-annotations-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("Report: %s", json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
