"""Run a bounded, resumable full-scale book-description backfill via Firecrawl batch API.

Reads current Google Sheets state on each invocation; only empty description cells are
written. Book URLs are exact-match verified, and existing human edits are preserved.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from collector import (DEFAULT_SHEET_ID, Sheets, describe_with_llm, get,
                       group_shard, norm, prepare, reuse)
from extraction import Extraction, Retriever, from_html, from_markdown, plausible, trusted_url

LOG = logging.getLogger("book_descriptions.batch")
BATCH_ROOT = "https://api.firecrawl.dev/v2/batch/scrape"


def canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return urlunsplit(("https", host, parsed.path.rstrip("/") or "/", "", ""))


def preferred_order(book):
    host = (urlsplit(book.url).hostname or "").removeprefix("www.")
    domains = ("eksmo.ru", "ast.ru", "mann-ivanov-ferber.ru", "alpinabook.ru",
               "piter.com", "litres.ru", "chitai-gorod.ru", "labirint.ru", "book24.ru")
    return (domains.index(host) if host in domains else len(domains), book.sheet_title)


def groups_for(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row.key or (norm(row.title), norm(row.url))].append(row)
    return list(groups.values())


class FirecrawlBatch:
    def __init__(self, key: str, base: str = BATCH_ROOT, sleep_fn=time.sleep):
        self.base = base
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"})
        self.sleep = sleep_fn

    def request(self, method: str, url: str, payload=None) -> dict:
        # Treat 429 as a provider throttling signal, not a missing book.
        for attempt in range(8):
            try:
                resp = self.session.request(method, url, json=payload, timeout=40)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    retry = resp.headers.get("Retry-After", "")
                    try:
                        seconds = min(120, max(1, float(retry)))
                    except ValueError:
                        seconds = min(120, 5 * (2 ** attempt))
                    LOG.warning("Firecrawl HTTP %d (%s), retrying after %.0fs",
                                resp.status_code, method, seconds)
                    self.sleep(seconds)
                    continue
                resp.raise_for_status()
                return resp.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 7:
                    raise RuntimeError("Network attempts exhausted") from exc
                self.sleep(min(60, 4 * (attempt + 1)))
        raise RuntimeError(f"Firecrawl retry attempts exhausted: {method} {url.split('?')[0]}")

    def start(self, urls: list[str]) -> str:
        data = self.request("POST", self.base, {"urls": urls,
                           "formats": ["html", "markdown"],
                           "onlyMainContent": True, "maxAge": 0})
        job_id = data.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("No batch scrape job ID returned")
        LOG.info("Firecrawl batch started id=%s URLs=%d", job_id, len(urls))
        return job_id

    def poll(self, job_id: str, on_page, deadline: float, interval: int = 35) -> str:
        """Poll at a low frequency; paginate data only after job finishes."""
        url = self.base + "/" + job_id
        state = "scraping"
        while time.monotonic() < deadline:
            status = self.request("GET", url)
            state = str(status.get("status", "unknown"))
            LOG.info("Batch %s status=%s completed=%s/%s",
                     job_id, state, status.get("completed"), status.get("total"))
            if state in ("completed", "failed", "cancelled"):
                seen_pages = set()
                page = status
                for _ in range(200):
                    for doc in page.get("data") or []:
                        on_page(doc)
                    next_url = page.get("next")
                    if not next_url or next_url in seen_pages:
                        break
                    parsed = urlsplit(next_url)
                    # No externally supplied pagination URLs may exfiltrate credentials.
                    if parsed.scheme != "https" or parsed.hostname != "api.firecrawl.dev" or not parsed.path.startswith("/v2/batch/scrape/"):
                        raise RuntimeError("Unexpected Firecrawl pagination URL")
                    seen_pages.add(next_url)
                    page = self.request("GET", next_url)
                return state
            self.sleep(interval)
        # Even an unfinished job may contain partial results; the next scheduled
        # GitHub invocation will retry missing rows and recheck current state.
        status = self.request("GET", url)
        for doc in status.get("data") or []:
            on_page(doc)
        return "partial"

    def close(self):
        self.session.close()


def firecrawl_description(doc):
    md = doc.get("metadata") or {}
    url = md.get("sourceURL") or md.get("url") or ""
    if not trusted_url(url) or (md.get("statusCode") and int(md.get("statusCode")) >= 400):
        return None
    description = from_html(doc.get("html") or "") or from_markdown(doc.get("markdown") or "")
    if not description or not plausible(description):
        return None
    return canonical_url(url), description, url


def direct_fetch(group):
    """Attempt known publisher/store URLs before cloud scraping, in worker-owned Chromium."""
    retriever = Retriever("", use_browser=os.getenv("USE_BROWSER", "true").lower() == "true")
    try:
        for row in sorted(group, key=preferred_order)[:2]:
            got = retriever.get(row.url)
            if got and plausible(got.description):
                return group, got
        return group, None
    finally:
        retriever.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--shards", type=int, default=4)
    ap.add_argument("--limit", type=int, default=1500)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (0 <= args.shard < args.shards):
        ap.error("Invalid shard")
    service_key = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    cloud_key = os.environ.get("FIRECRAWL_API_KEY", "")
    if not service_key or not cloud_key:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON and FIRECRAWL_API_KEY are required")
    sheets = Sheets(os.environ.get("SPREADSHEET_ID", DEFAULT_SHEET_ID), service_key)
    books, _ = prepare(sheets, args.dry_run)
    pending = [r for r in books if not r.description]
    existing = reuse(books)
    copied = 0
    if args.shard == 0:
        matches = [(r, existing[r.key]) for r in pending if r.key in existing]
        for start in range(0, len(matches), 40):
            copied += sheets.flush(matches[start:start+40], args.dry_run)
    left = [r for r in pending if not r.key or r.key not in existing]
    selected = group_shard(left, args.shard, args.shards, 0, args.limit)
    grouped = groups_for(selected)
    LOG.info("Backfill initial books=%d missing=%d shard=%d/%d rows=%d unique=%d reused=%d",
             len(books), len(pending), args.shard, args.shards, len(selected), len(grouped), copied)
    pending_updates = []
    count = Counter()

    def commit(force=False):
        if pending_updates and (force or len(pending_updates) >= 40):
            count["written"] += sheets.flush(pending_updates[:], args.dry_run)
            pending_updates.clear()

    def accept(group, got):
        if not got or not plausible(got.description):
            return False
        row = sorted(group, key=preferred_order)[0]
        desc = describe_with_llm(got.description, row.title, row.author,
                                 os.getenv("OPENROUTER_API_KEY", ""),
                                 os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite"))
        if not desc or not plausible(desc):
            return False
        pending_updates.extend((r, Extraction(desc, got.source_url, got.method)) for r in group)
        count["scraped_" + got.method] += 1
        commit()
        return True

    # Direct site retrieval and browser fallback: no Firecrawl 429s here.
    unresolved = []
    workers = max(1, min(12, int(os.getenv("WORKERS", "6"))))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(direct_fetch, group): group for group in grouped}
        for n, future in enumerate(as_completed(future_map), 1):
            group = future_map[future]
            try:
                _, got = future.result()
            except Exception as exc:
                LOG.warning("Direct retrieval issue: %s", str(exc)[:150])
                got = None
            if not accept(group, got):
                unresolved.append(group)
            if n % 50 == 0:
                LOG.info("Direct phase %d/%d, unresolved=%d, written=%d", n, len(grouped),len(unresolved),count["written"])
    commit(True)

    # Firecrawl batch API: one request covers many books, with provider-managed
    # concurrency. Mapping must match the exact original URL, never mere title.
    mapping = {}
    for group in unresolved:
        selected_row = sorted(group, key=preferred_order)[0]
        mapping[canonical_url(selected_row.url)] = group
    todo = list(mapping)
    count["batch_candidates"] = len(todo)
    done_urls = set()
    batch = FirecrawlBatch(cloud_key)
    chunk_size = max(1, min(500, int(os.getenv("BATCH_SIZE", "150"))))
    overall_deadline = time.monotonic() + max(300, int(os.getenv("CLOUD_BUDGET_SECONDS", "14000")))
    try:
        for start in range(0, len(todo), chunk_size):
            if time.monotonic() > overall_deadline:
                LOG.warning("Time budget reached; remaining pages will be retried next run")
                break
            urls = todo[start:start+chunk_size]
            job_id = batch.start(urls)
            def on_document(doc):
                parsed = firecrawl_description(doc)
                if not parsed:
                    return
                normalized, description, url = parsed
                if normalized not in mapping or normalized in done_urls or normalized not in urls:
                    return
                if accept(mapping[normalized], Extraction(description, url, "firecrawl_batch")):
                    done_urls.add(normalized)
            state = batch.poll(job_id, on_document, overall_deadline)
            commit(True)
            LOG.info("Batch %d/%d status=%s accepted=%d total_written=%d",
                     start//chunk_size+1, (len(todo)+chunk_size-1)//chunk_size,
                     state,len(done_urls),count["written"])
            if state == "partial":
                break
    finally:
        batch.close()
        commit(True)
    count["unresolved"] = len(todo) - len(done_urls)
    report = {"at":datetime.now(timezone.utc).isoformat(),"shard":args.shard,
              "shards":args.shards,"books":len(books),"missing_before":len(pending),
              "reused":copied,"selected_rows":len(selected),"unique":len(grouped),
              "stats":dict(count),"dry_run":args.dry_run}
    Path(f"batch-description-report-{args.shard}.json").write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    LOG.info("Report: %s",json.dumps(report,ensure_ascii=False))


if __name__ == "__main__":
    main()
