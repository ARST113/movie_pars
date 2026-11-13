"""Run a bounded, resumable full-scale book-description backfill via Firecrawl batch API.

Reads current Google Sheets state on each invocation; only empty description cells are
written. Book URLs are exact-match verified, and existing human edits are preserved.
"""
from __future__ import annotations

import argparse
import json
import logging
import hashlib
import re
import os
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import local
from urllib.parse import urlsplit, urlunsplit

import requests

from collector import (DEFAULT_SHEET_ID, Sheets, describe_with_llm, get,
                       group_shard, norm, prepare, reuse)
from extraction import Extraction, Retriever, from_html, from_markdown, normalise_text, plausible, trusted_url

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


def chitai_alternative(book) -> str | None:
    """Book24 product ID column P is often the original Chitai-gorod SKU.

    Use the verified retail SKU together with the Book24 product slug to open
    a distinct Chitai-gorod product card, without a paid search request.
    """
    if book.sheet_title != "Book24":
        return None
    product_id = get(book.data, 15).strip()
    if not re.fullmatch(r"[0-9]{6,9}", product_id):
        return None
    if (urlsplit(book.url).hostname or "").removeprefix("www.") != "book24.ru":
        return None
    slug_match = re.fullmatch(r"/product/([a-z0-9-]+)-[0-9]+/?", urlsplit(book.url).path, re.I)
    if not slug_match:
        return None
    slug = slug_match.group(1).strip("-")
    if not slug:
        return None
    return f"https://www.chitai-gorod.ru/product/{slug}-{product_id}"


def rotated_candidates(urls: list[str], shard: int, rotation: int) -> list[str]:
    """Change candidate ordering every four-hour cycle, avoiding repeated first-page failures."""
    if not urls:
        return []
    # Hash-based ordering is stable within a cycle and changes across cycles.
    return sorted(urls, key=lambda u: hashlib.sha256(
        f"{rotation}|{shard}|{u}".encode("utf-8")).digest())


def batch_url_keys(urls: list[str]) -> set[str]:
    """Canonical URL identities returned by a particular batch submission."""
    return {canonical_url(url) for url in urls}


def per_shard_credit_budget(remaining: int, shards: int, reserve: int = 25,
                            max_per_shard: int = 250) -> int:
    """Keep a shared reserve and divide the remaining credit allowance safely."""
    if shards < 1 or remaining <= reserve:
        return 0
    return min(max_per_shard, max(0, (remaining - reserve) // shards))


_DIRECT_LOCAL = local()


class _DirectWorker:
    """One reusable Requests/Chromium client per worker thread."""
    def __init__(self):
        self.retriever = Retriever(
            "", use_browser=os.getenv("USE_BROWSER", "true").lower() in ("1", "true", "yes")
        )

    def __del__(self):
        # threading.local releases this object when its owning worker exits,
        # so Playwright is closed on the same thread that created it.
        try:
            self.retriever.close()
        except Exception:
            pass


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

    def remaining_credits(self) -> int:
        root = self.base.split("/batch/scrape", 1)[0]
        body = self.request("GET", root + "/team/credit-usage")
        data = body.get("data") or {}
        if not body.get("success") or data.get("remainingCredits") is None:
            raise RuntimeError("Firecrawl credit endpoint returned no remainingCredits")
        return int(data["remainingCredits"])

    def start(self, urls: list[str]) -> str:
        data = self.request("POST", self.base, {"urls": urls,
                           "formats": ["html", "markdown"],
                           "onlyMainContent": True})
        job_id = data.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("No batch scrape job ID returned")
        LOG.info("Firecrawl batch started id=%s URLs=%d", job_id, len(urls))
        return job_id

    def _deliver_pages(self, first_page: dict, on_page) -> None:
        seen_pages = set()
        page = first_page
        for _ in range(200):
            for doc in page.get("data") or []:
                on_page(doc)
            next_url = page.get("next")
            if not next_url or next_url in seen_pages:
                break
            parsed = urlsplit(next_url)
            # Never forward the API bearer token to a foreign pagination host.
            if (parsed.scheme != "https" or parsed.hostname != "api.firecrawl.dev"
                    or not parsed.path.startswith("/v2/batch/scrape/")):
                raise RuntimeError("Unexpected Firecrawl pagination URL")
            seen_pages.add(next_url)
            page = self.request("GET", next_url)

    def poll(self, job_id: str, on_page, deadline: float, interval: int = 35) -> str:
        """Poll at a low frequency and collect every available result page."""
        url = self.base + "/" + job_id
        state = "scraping"
        while time.monotonic() < deadline:
            status = self.request("GET", url)
            state = str(status.get("status", "unknown"))
            LOG.info("Batch %s status=%s completed=%s/%s",
                     job_id, state, status.get("completed"), status.get("total"))
            if state in ("completed", "failed", "cancelled"):
                self._deliver_pages(status, on_page)
                return state
            self.sleep(interval)
        # Retrieve partial results too; pagination can be present before completion.
        status = self.request("GET", url)
        self._deliver_pages(status, on_page)
        return "partial"

    def close(self):
        self.session.close()


def firecrawl_description(doc):
    md = doc.get("metadata") or {}
    url = md.get("sourceURL") or md.get("url") or ""
    try:
        status_code = int(md.get("statusCode") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if not trusted_url(url) or status_code >= 400:
        return None
    description = from_html(doc.get("html") or "") or from_markdown(doc.get("markdown") or "")
    if not description or not plausible(description):
        return None
    return canonical_url(url), description, url


def direct_fetch(group):
    """Attempt publisher/store URLs with one reusable client per worker thread."""
    worker = getattr(_DIRECT_LOCAL, "worker", None)
    if worker is None:
        worker = _DirectWorker()
        _DIRECT_LOCAL.worker = worker
    retriever = worker.retriever
    candidates = []
    for row in sorted(group, key=preferred_order)[:2]:
        # Most remaining Book24 cards are blocked on GitHub-hosted runners.
        # Their Book24 ID column maps to a corresponding Chitai-gorod card.
        alternate = chitai_alternative(row)
        if alternate:
            candidates.append(alternate)
        candidates.append(row.url)
    for url in dict.fromkeys(candidates):
        got = retriever.get(url)
        if got and plausible(got.description):
            return group, got
    return group, None


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
    if not service_key:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is required")
    if not cloud_key:
        LOG.warning("FIRECRAWL_API_KEY is missing; continuing with direct HTTP/Chromium only")
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
        raw_description = normalise_text(got.description, 1000)
        desc = describe_with_llm(got.description, row.title, row.author,
                                 os.getenv("OPENROUTER_API_KEY", ""),
                                 os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash-lite"))
        # An LLM outage must not discard a verified publisher synopsis.
        if not desc or not plausible(desc):
            desc = raw_description
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

    # Firecrawl batch API is the paid fallback. Read the live balance and
    # divide an explicit credit budget across shards, preserving a shared reserve.
    mapping = {}
    for group in unresolved:
        selected_row = sorted(group, key=preferred_order)[0]
        # Firecrawl should attempt the alternative bookstore too: GitHub HTTP
        # often cannot reach the Book24 product page at all.
        candidate_url = chitai_alternative(selected_row) or selected_row.url
        mapping[canonical_url(candidate_url)] = (group, candidate_url)
    current_rotation = int(time.time() // (4 * 3600))
    todo = rotated_candidates(list(mapping), args.shard, current_rotation)
    count["batch_candidates"] = len(todo)
    done_urls = set()
    chunk_size = max(1, min(500, int(os.getenv("BATCH_SIZE", "150"))))
    overall_deadline = time.monotonic() + max(300, int(os.getenv("CLOUD_BUDGET_SECONDS", "14000")))
    if cloud_key and todo:
        batch = FirecrawlBatch(cloud_key)
        try:
            try:
                remaining_credits = batch.remaining_credits()
                reserve = max(0, int(os.getenv("FIRECRAWL_RESERVE_CREDITS", "25")))
                max_per_shard = max(0, int(os.getenv("FIRECRAWL_MAX_URLS_PER_SHARD", "250")))
                credit_budget = per_shard_credit_budget(
                    remaining_credits, args.shards, reserve, max_per_shard
                )
                count["firecrawl_credits_at_start"] = remaining_credits
                count["firecrawl_budget_urls"] = credit_budget
                LOG.info("Firecrawl credits=%d reserve=%d shard_budget=%d/%d",
                         remaining_credits, reserve, credit_budget, args.shards)
            except Exception as exc:
                credit_budget = 0
                count["firecrawl_credit_check_failed"] += 1
                LOG.warning("Could not verify Firecrawl balance; paid fallback disabled safely: %s",
                            str(exc)[:180])

            batch_todo = todo[:credit_budget]
            count["deferred_by_credit_budget"] = len(todo) - len(batch_todo)
            if count["deferred_by_credit_budget"]:
                LOG.info("Deferring %d Firecrawl candidates to later runs to protect credits",
                         count["deferred_by_credit_budget"])

            for start in range(0, len(batch_todo), chunk_size):
                if time.monotonic() > overall_deadline:
                    LOG.warning("Time budget reached; remaining pages will be retried next run")
                    break
                keys = batch_todo[start:start+chunk_size]
                urls = [mapping[key][1] for key in keys]
                try:
                    job_id = batch.start(urls)
                    count["firecrawl_batch_jobs"] += 1
                    count["firecrawl_urls_submitted"] += len(urls)
                except requests.HTTPError as exc:
                    status_code = getattr(getattr(exc, "response", None), "status_code", None)
                    if status_code is None:
                        status_code = getattr(getattr(exc, "response", None), "status", None)
                    if status_code == 402:
                        count["firecrawl_payment_required"] += 1
                        LOG.warning("Firecrawl returned HTTP 402; stopping paid fallback, direct results remain saved")
                        break
                    count["firecrawl_batch_start_errors"] += 1
                    LOG.warning("Firecrawl batch start failed: %s", str(exc)[:180])
                    continue
                except Exception as exc:
                    count["firecrawl_batch_start_errors"] += 1
                    LOG.warning("Firecrawl batch start failed: %s", str(exc)[:180])
                    continue

                chunk_keys = set(keys)
                def on_document(doc):
                    parsed = firecrawl_description(doc)
                    if not parsed:
                        count["firecrawl_documents_rejected"] += 1
                        return
                    normalized, description, url = parsed
                    # Compare canonical URL identities on both sides. Raw URLs
                    # may differ by www, query parameters, or a trailing slash.
                    if normalized not in mapping or normalized in done_urls or normalized not in chunk_keys:
                        return
                    if accept(mapping[normalized][0], Extraction(description, url, "firecrawl_batch")):
                        done_urls.add(normalized)

                try:
                    state = batch.poll(job_id, on_document, overall_deadline)
                except Exception as exc:
                    count["firecrawl_batch_poll_errors"] += 1
                    LOG.warning("Firecrawl batch polling failed for %s: %s", job_id, str(exc)[:180])
                    break
                commit(True)
                LOG.info("Batch %d/%d status=%s accepted=%d total_written=%d",
                         start//chunk_size+1, (len(batch_todo)+chunk_size-1)//chunk_size,
                         state, len(done_urls), count["written"])
                if state == "partial":
                    break
        finally:
            batch.close()
            commit(True)
    else:
        count["deferred_by_credit_budget"] = len(todo)
        LOG.info("Skipping Firecrawl batch fallback; direct HTTP/Chromium results are retained")
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
