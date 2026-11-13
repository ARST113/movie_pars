"""Diagnose Firecrawl batch failures against direct HTTP and Chromium access."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

BATCH_IDS = [
    "01a1d3c9-d042-7131-8d49-37c79e045bc5",
    "01a1d3ca-bd91-709b-bb53-b5385a3a9a73",
    "01a1d3ca-bd8f-735a-8c41-c95c00af0cfb",
    "01a1d3c9-dc7f-71be-b988-28344e2beef6",
    "01a1d3c9-dc7f-72ff-a7a9-78faf566cab3",
    "01a1d3cc-6906-76c2-a5f5-adc351ad2654",
    "01a1d3cc-6907-767b-8e8b-474717243ddf",
    "01a1d3ca-1413-709c-bdca-408595dee987",
    "01a1d3ca-bc51-73d3-adc9-7b922d4b0a47",
    "01a1d3ca-bea0-7237-a4fa-e2b5a24a7b7a",
    "01a1d3ca-be9d-71bb-82bd-37e97ac3fe7e",
    "01a1d3c9-f173-73c7-bada-c90ba518f507",
    "01a1d3c9-f171-7593-ac0c-c87cdb7d79a2",
    "01a1d3ca-bd51-702b-a837-1a36c5fb0500",
    "01a1d3ca-bd57-77aa-a1fd-170c2cae5dee",
    "01a1d3c9-e0af-77c7-bbae-332a6500c392",
    "01a1d3c9-e0b4-7492-bfa1-5e5bfb1a351c",
    "01a1d3c9-effb-72c6-9ebd-ff9897d52917",
    "01a1d3c9-efdf-77ae-9e13-def3d1a61a17",
]
TRUSTED = (
    "book24.ru", "labirint.ru", "ast.ru", "piter.com", "bookvoed.ru",
    "chitai-gorod.ru", "eksmo.ru", "mann-ivanov-ferber.ru", "alpinabook.ru",
    "rosman.ru", "respublica.ru", "litres.ru", "ozon.ru", "wildberries.ru",
)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BLOCK_MARKERS = (
    "cloudflare", "ddos-guard", "access denied", "just a moment",
    "проверка браузера", "доступ запрещен", "обнаружена подозрительная",
    "verify you are human", "captcha", "robot check", "подтвердите, что вы не робот",
)


def domain_for(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for domain in TRUSTED:
        if host == domain or host.endswith("." + domain):
            return domain
    return host or "unknown"


def get_errors(batch_id: str, session: requests.Session) -> dict:
    url = f"https://api.firecrawl.dev/v2/batch/scrape/{batch_id}/errors"
    for attempt in range(6):
        response = session.get(url, timeout=35)
        if response.status_code == 429 or response.status_code >= 500:
            retry = response.headers.get("Retry-After", "")
            try:
                delay = min(30, max(1, float(retry)))
            except ValueError:
                delay = min(30, 2 ** attempt)
            print(f"RETRY batch={batch_id} status={response.status_code} wait={delay}s", flush=True)
            time.sleep(delay)
            continue
        response.raise_for_status()
        return response.json()
    raise RuntimeError(f"Could not read Firecrawl errors for batch {batch_id}")


def classify(status: int | None, content_type: str, text: str, final_url: str, original_url: str) -> str:
    low = text[:12000].lower()
    if any(marker in low for marker in BLOCK_MARKERS):
        return "challenge_or_antibot"
    if status is None:
        return "connection_error"
    if status in (401, 403, 407, 429):
        return f"http_{status}_blocked_or_limited"
    if status >= 500:
        return f"http_{status}_server_error"
    if status >= 400:
        return f"http_{status}_client_error"
    if "html" not in (content_type or "").lower():
        return "non_html_response"
    if len(text.strip()) < 500:
        return "short_or_empty_html"
    original_host = (urlsplit(original_url).hostname or "").lower()
    final_host = (urlsplit(final_url).hostname or "").lower()
    if final_host and original_host and final_host != original_host:
        return "redirected_to_other_host"
    return "http_200_html"


def http_probe(url: str) -> dict:
    try:
        response = requests.get(
            url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"},
            timeout=(8, 18), allow_redirects=True,
        )
        text = response.text[:1_500_000] if response.text else ""
        soup = BeautifulSoup(text[:100_000], "html.parser")
        title = soup.title.get_text(" ", strip=True)[:180] if soup.title else ""
        result = {
            "status": response.status_code,
            "final_url": response.url,
            "content_type": response.headers.get("content-type", ""),
            "bytes": len(response.content),
            "title": title,
            "class": classify(response.status_code, response.headers.get("content-type", ""), text,
                              response.url, url),
        }
        return result
    except Exception as exc:
        return {"status": None, "class": "connection_error",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


async def browser_probes(samples: list[dict]) -> list[dict]:
    from playwright.async_api import async_playwright

    semaphore = asyncio.Semaphore(4)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def one(sample: dict) -> dict:
            async with semaphore:
                page = await browser.new_page(user_agent=UA, locale="ru-RU")
                try:
                    response = await page.goto(sample["url"], wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(500)
                    title = (await page.title())[:180]
                    body = await page.locator("body").inner_text(timeout=3000)
                    status = response.status if response else None
                    content_type = response.headers.get("content-type", "") if response else ""
                    final_url = page.url
                    return {
                        "status": status, "final_url": final_url, "title": title,
                        "body_chars": len(body),
                        "class": classify(status, content_type, body, final_url, sample["url"]),
                    }
                except Exception as exc:
                    return {"status": None, "class": "browser_navigation_error",
                            "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
                finally:
                    await page.close()

        results = await asyncio.gather(*(one(sample) for sample in samples))
        await browser.close()
        return results


def choose_samples(errors: list[dict], limit_per_domain: int = 3) -> list[dict]:
    grouped = defaultdict(list)
    seen = set()
    for item in errors:
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        grouped[domain_for(url)].append(item)

    samples = []
    for domain in sorted(grouped):
        candidates = grouped[domain]
        chosen = []
        used_errors = set()
        # Prefer one representative of each distinct Firecrawl error before filling the quota.
        for item in candidates:
            error = str(item.get("error") or "unknown")
            if error not in used_errors:
                chosen.append(item)
                used_errors.add(error)
            if len(chosen) >= limit_per_domain:
                break
        if len(chosen) < limit_per_domain:
            chosen_urls = {x.get("url") for x in chosen}
            for item in candidates:
                if item.get("url") not in chosen_urls:
                    chosen.append(item)
                    chosen_urls.add(item.get("url"))
                if len(chosen) >= limit_per_domain:
                    break
        samples.extend(chosen)
    return samples


def main() -> None:
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        raise SystemExit("FIRECRAWL_API_KEY secret is missing")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {key}"})

    all_errors = []
    robots = []
    batch_stats = []
    for batch_id in BATCH_IDS:
        payload = get_errors(batch_id, session)
        errors = payload.get("errors") or []
        blocked = payload.get("robotsBlocked") or []
        batch_stats.append({"batch_id": batch_id, "failed": len(errors), "robots_blocked": len(blocked)})
        for item in errors:
            if isinstance(item, dict):
                item = dict(item)
                item["batch_id"] = batch_id
                all_errors.append(item)
        robots.extend({"batch_id": batch_id, "url": url} for url in blocked)
        print(f"BATCH {batch_id} failed={len(errors)} robots_blocked={len(blocked)}", flush=True)
        time.sleep(0.15)
    session.close()

    by_domain = defaultdict(list)
    for item in all_errors:
        by_domain[domain_for(str(item.get("url") or ""))].append(item)

    domain_summary = []
    for domain, items in sorted(by_domain.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        top_errors = Counter(str(x.get("error") or "unknown")[:180] for x in items)
        domain_summary.append({
            "domain": domain, "failed": len(items),
            "unique_urls": len({x.get("url") for x in items}),
            "top_errors": [{"error": error, "count": count} for error, count in top_errors.most_common(5)],
        })
        print(f"DOMAIN {domain} failed={len(items)} unique_urls={len({x.get('url') for x in items})}", flush=True)
        for error, count in top_errors.most_common(3):
            print(f"  FIRECRAWL_ERROR count={count} message={error}", flush=True)

    samples = choose_samples(all_errors, limit_per_domain=3)
    print(f"SAMPLES selected={len(samples)} domains={len(by_domain)}; probing direct HTTP...", flush=True)
    with ThreadPoolExecutor(max_workers=10) as pool:
        http_results = list(pool.map(lambda item: http_probe(item["url"]), samples))

    for sample, result in zip(samples, http_results):
        sample["http"] = result
    print("Direct HTTP probes complete; probing the same URLs with Chromium...", flush=True)
    browser_results = asyncio.run(browser_probes(samples))
    for sample, result in zip(samples, browser_results):
        sample["chromium"] = result

    for sample in samples:
        print("SAMPLE " + json.dumps({
            "domain": domain_for(sample["url"]), "url": sample["url"],
            "firecrawl_error": sample.get("error"),
            "http": sample.get("http"), "chromium": sample.get("chromium"),
        }, ensure_ascii=False), flush=True)

    report = {
        "run": 17,
        "batches": batch_stats,
        "failed_urls_total": len(all_errors),
        "unique_failed_urls": len({x.get("url") for x in all_errors}),
        "robots_blocked_total": len(robots),
        "domains": domain_summary,
        "samples": samples,
        "all_errors": all_errors,
        "robots_blocked": robots,
    }
    Path("firecrawl-diagnostic-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("TOTAL " + json.dumps({
        "batches": len(batch_stats), "failed": len(all_errors),
        "unique_failed_urls": report["unique_failed_urls"],
        "robots_blocked": len(robots), "domains": len(domain_summary),
        "sampled": len(samples),
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
