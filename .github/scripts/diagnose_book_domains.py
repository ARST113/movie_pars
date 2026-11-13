"""Read-only comparison of live book product pages from GitHub Actions."""
from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

SAMPLES = [
    ("Book24", "https://book24.ru/product/plamya-oderzhimosti-6769433/"),
    ("Book24", "https://book24.ru/product/posledniy-gambit-6729343/"),
    ("Лабиринт", "https://www.labirint.ru/books/986862/"),
    ("Лабиринт", "https://www.labirint.ru/books/786953/"),
    ("АСТ", "https://ast.ru/book/nepervaya-lyubov-ispoved-gorya-i-radosti-899252/"),
    ("АСТ", "https://ast.ru/book/na-ostrie-klinka-887387/"),
    ("Питер", "https://www.piter.com/collection/top-2/product/vysokonagruzhennye-prilozheniya-programmirovanie-masshtabirovanie-podderzhka-2-e-izd-2"),
    ("Питер", "https://www.piter.com/collection/top-2/product/kompyuternye-seti-printsipy-tehnologii-protokoly-7-e-izdanie"),
    ("Буквоед", "https://www.bookvoed.ru/product/tajnyj-dnevnik-veriti-8809672"),
    ("Буквоед", "https://www.bookvoed.ru/product/rebekka-arros-cetvertoe-krylo-obrez-s-uzorom-8806189"),
    ("Читай-город", "https://www.chitai-gorod.ru/product/esli-vse-koshki-v-mire-ischeznut-poket-2968841"),
    ("Читай-город", "https://www.chitai-gorod.ru/product/nepervaa-lubov-ispoved-gora-i-radosti-3173686"),
    ("Эксмо", "https://eksmo.ru/book/novaya-kniga-ITD1443817/"),
    ("Эксмо", "https://eksmo.ru/book/lzhets-u-n-3-ITD1506475/"),
    ("МИФ", "https://www.mann-ivanov-ferber.ru/catalog/product/mir-za-velikim-tumanom-cudovishhe-karnoxelma/"),
    ("МИФ", "https://www.mann-ivanov-ferber.ru/catalog/product/mir-za-velikim-tumanom-drakone-serebro/"),
    ("Альпина", "https://alpinabook.ru/catalog/book-kak-ne-umeret-molodym/"),
    ("Альпина", "https://alpinabook.ru/catalog/book-vopros-o-vinovnosti/"),
    ("Республика", "https://www.respublica.ru/knigi/bestsellery/572693-von-phyon-s-mindal-t-o"),
    ("Республика", "https://www.respublica.ru/knigi/bestsellery/528452-klir-d-atomnye-privychki-sam-sebe-psiholog-t-o"),
    ("Литрес", "https://www.litres.ru/book/aleks-mihaelides/bezmolvnyy-pacient-40502015/"),
    ("Литрес", "https://www.litres.ru/audiobook/daniel-kaneman/dumay-medlenno-reshay-bystro-21636519/"),
]
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BLOCK_MARKERS = (
    "cloudflare", "ddos-guard", "access denied", "just a moment",
    "проверка браузера", "доступ запрещен", "обнаружена подозрительная",
    "verify you are human", "captcha", "robot check", "подтвердите, что вы не робот",
)


def classify(status, content_type, text):
    low = (text or "")[:12000].lower()
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
    if len((text or "").strip()) < 500:
        return "short_or_empty_html"
    return "http_200_html"


def http_probe(item):
    name, url = item
    try:
        response = requests.get(
            url, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"},
            timeout=(8, 18), allow_redirects=True,
        )
        text = response.text[:1_500_000]
        soup = BeautifulSoup(text[:100_000], "html.parser")
        title = soup.title.get_text(" ", strip=True)[:160] if soup.title else ""
        return {"source": name, "url": url, "status": response.status_code,
                "final_url": response.url, "content_type": response.headers.get("content-type", ""),
                "bytes": len(response.content), "title": title,
                "class": classify(response.status_code, response.headers.get("content-type", ""), text)}
    except Exception as exc:
        return {"source": name, "url": url, "status": None, "class": "connection_error",
                "error": f"{type(exc).__name__}: {str(exc)[:220]}"}


async def browser_probe(items):
    from playwright.async_api import async_playwright
    semaphore = asyncio.Semaphore(4)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def one(item):
            name, url = item
            async with semaphore:
                page = await browser.new_page(user_agent=UA, locale="ru-RU")
                try:
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(500)
                    title = (await page.title())[:160]
                    body = await page.locator("body").inner_text(timeout=3000)
                    status = response.status if response else None
                    content_type = response.headers.get("content-type", "") if response else ""
                    return {"source": name, "url": url, "status": status, "final_url": page.url,
                            "title": title, "body_chars": len(body),
                            "class": classify(status, content_type, body)}
                except Exception as exc:
                    return {"source": name, "url": url, "status": None,
                            "class": "browser_navigation_error",
                            "error": f"{type(exc).__name__}: {str(exc)[:220]}"}
                finally:
                    await page.close()

        results = await asyncio.gather(*(one(item) for item in items))
        await browser.close()
        return results


def firecrawl_account_check():
    key = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if not key:
        return {"credit_endpoint": {"status": None, "error": "FIRECRAWL_API_KEY missing"}}
    session = requests.Session()
    session.headers["Authorization"] = "Bearer " + key
    result = {}
    for label, url in (
        ("credit_endpoint", "https://api.firecrawl.dev/v2/team/credit-usage"),
        ("old_batch_errors", "https://api.firecrawl.dev/v2/batch/scrape/01a1d3c9-d042-7131-8d49-37c79e045bc5/errors"),
    ):
        try:
            response = session.get(url, timeout=25)
            try:
                body = response.json()
            except ValueError:
                body = response.text[:500]
            if label == "credit_endpoint" and response.ok:
                data = body.get("data") or {}
                body = {"remainingCredits": data.get("remainingCredits"),
                        "planCredits": data.get("planCredits"),
                        "status": body.get("success")}
            elif isinstance(body, dict):
                body = {k: v for k, v in body.items() if k in ("success", "error", "code", "message")}
            result[label] = {"status": response.status_code, "body": body}
        except Exception as exc:
            result[label] = {"status": None, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    session.close()
    return result


def main():
    print("Checking current Firecrawl key and historical batch access (GET only)...", flush=True)
    firecrawl = firecrawl_account_check()
    print("FIRECRAWL_ACCOUNT " + json.dumps(firecrawl, ensure_ascii=False), flush=True)

    print(f"Testing {len(SAMPLES)} representative current product URLs via direct HTTP...", flush=True)
    with ThreadPoolExecutor(max_workers=10) as pool:
        http_results = list(pool.map(http_probe, SAMPLES))
    print("Testing the same URLs via Chromium...", flush=True)
    browser_results = asyncio.run(browser_probe(SAMPLES))

    for http, browser in zip(http_results, browser_results):
        print("PAGE " + json.dumps({
            "source": http["source"], "url": http["url"],
            "http": http, "chromium": browser,
        }, ensure_ascii=False), flush=True)

    report = {"firecrawl_account": firecrawl, "http": http_results, "chromium": browser_results}
    Path("book-domain-diagnostic.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("CLASS_COUNTS " + json.dumps({
        "http": {k: sum(1 for x in http_results if x["class"] == k)
                 for k in sorted({x["class"] for x in http_results})},
        "chromium": {k: sum(1 for x in browser_results if x["class"] == k)
                     for k in sorted({x["class"] for x in browser_results})},
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
