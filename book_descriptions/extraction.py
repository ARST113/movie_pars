"""Conservative book-description extraction from publisher and store product pages."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

LOG = logging.getLogger(__name__)
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TRUSTED_DOMAINS = (
    "book24.ru", "labirint.ru", "ast.ru", "piter.com", "bookvoed.ru",
    "chitai-gorod.ru", "eksmo.ru", "mann-ivanov-ferber.ru", "alpinabook.ru",
    "rosman.ru", "respublica.ru", "litres.ru", "ozon.ru", "wildberries.ru",
)
BAD = (
    "доставка по всей россии", "купить книгу в интернет-магазине", "купить книгу по выгодной цене",
    "интернет-магазин книг", "вы можете купить", "ваш браузер", "доступ запрещен",
    "access denied", "just a moment", "ddos-guard", "проверка браузера", "обнаружена подозрительная",
    "включите javascript", "отзывы покупателей", "вход в личный кабинет",
    "недостаточно информации", "не могу составить", "не удалось найти описание", "я не могу",
)
SELECTORS = (
    '[itemprop="description"]', '[data-testid="book-description"]',
    '[data-testid="product-description"]', '.book-description',
    '.product-description', '.product__description', '.product-detail__description',
    '.product-info__description', '.detail-product__description',
    '.b-about__text', '.annotation', '.book-annotation',
    '.book-about', '#book-description', '#description',
)


def normalise_text(value: str, max_chars: int = 2400) -> str:
    if not isinstance(value, str):
        return ""
    value = BeautifulSoup(value, "html.parser").get_text(" ", strip=True) if "<" in value and ">" in value else value
    value = unescape(value).replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip(' \"\u00ab\u00bb')
    return value[:max_chars].rsplit(" ", 1)[0].strip() if len(value) > max_chars else value


def plausible(value: str) -> bool:
    value = normalise_text(value)
    low = value.lower()
    if len(value) < 85 or len(value.split()) < 12:
        return False
    if any(b in low for b in BAD):
        return False
    if sum(ch.isalpha() for ch in value) < len(value) * .55:
        return False
    if value.count("http") > 1:
        return False
    return True


def trusted_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and any(domain == d or domain.endswith("." + d) for d in TRUSTED_DOMAINS)
    except Exception:
        return False


def jsonld_descriptions(blob: object) -> list[str]:
    found: list[str] = []
    if isinstance(blob, list):
        for item in blob:
            found.extend(jsonld_descriptions(item))
    elif isinstance(blob, dict):
        types = blob.get("@type", [])
        types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
        if any(str(t).lower() in ("book", "product", "audiobook", "creativework") for t in types):
            val = blob.get("description")
            if isinstance(val, str):
                found.append(val)
        for child in ("@graph", "mainEntity", "itemOffered", "itemListElement"):
            if child in blob:
                found.extend(jsonld_descriptions(blob[child]))
    return found


def from_html(html: str) -> str | None:
    if not html:
        return None
    soup = BeautifulSoup(html, "lxml")
    candidates: list[tuple[int, str]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            for content in jsonld_descriptions(json.loads(script.string or script.get_text())):
                candidates.append((110, content))
        except (TypeError, ValueError):
            continue
    for selector in SELECTORS:
        for element in soup.select(selector)[:4]:
            text = element.get_text(" ", strip=True)
            candidates.append((100, text))
    for attr, key in (("property", "og:description"), ("name", "description"), ("name", "twitter:description")):
        for node in soup.find_all("meta", attrs={attr: key}):
            candidates.append((45, node.get("content", "")))
    candidates = [(priority, normalise_text(value)) for priority, value in candidates if plausible(value)]
    if not candidates:
        return None
    # A long, site-wide page dump is less trustworthy than a named description field.
    candidates.sort(key=lambda pair: (pair[0], min(len(pair[1]), 1600)), reverse=True)
    return candidates[0][1]


def from_markdown(md: str) -> str | None:
    if not md:
        return None
    # Avoid accidentally treating the full product page, comments, or recommendations as synopsis.
    pattern = re.compile(r"(?:^|\n)\s{0,3}#{1,4}\s*(?:О книге|Описание книги|Аннотация|Описание|Сюжет)\s*\n", re.I)
    for match in pattern.finditer(md):
        remainder = md[match.end():]
        section = re.split(r"\n\s*(?:#{1,4}\s|\*\*[^*]+\*\*|Отзывы|Похожие книги)", remainder, maxsplit=1)[0]
        section = re.sub(r"!\[[^]]*\]\([^)]*\)", "", section)
        section = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", section)
        content = normalise_text(section)
        if plausible(content):
            return content
    # Some publisher pages render an underlined heading, e.g. "О книге Читать фрагмент".
    section_pattern = re.compile(r"(?:^|\n)(?:О книге(?: Читать фрагмент)?|Описание книги|Аннотация)\s*\n[-=]{3,}\s*\n", re.I)
    for match in section_pattern.finditer(md):
        segment = md[match.end():]
        segment = re.split(r"(?:\n\s*\n(?:Читать полностью|Свернуть описание|Слушать аннотацию)|\n[-=]{3,})", segment, maxsplit=1)[0]
        segment = re.sub(r"!\[[^]]*\]\([^)]*\)", "", segment)
        segment = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", segment)
        content = normalise_text(segment)
        if plausible(content):
            return content
    return None


@dataclass
class Extraction:
    description: str
    source_url: str
    method: str


class Browser:
    """One Chromium process per Action run, started only when actually needed."""
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._playwright = None
        self._browser = None
        self._page = None

    def read(self, url: str) -> str | None:
        if not self.enabled:
            return None
        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.chromium.launch(headless=True)
                context = self._browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
                self._page = context.new_page()
            response = self._page.goto(url, wait_until="domcontentloaded", timeout=22000)
            if response is None or response.status >= 400:
                return None
            self._page.wait_for_timeout(700)
            return self._page.content()
        except Exception as exc:
            LOG.warning("Browser failed for %s: %s", url, str(exc)[:120])
            return None

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()


class Retriever:
    def __init__(self, firecrawl_key: str = "", firecrawl_base: str = "https://api.firecrawl.dev/v2", use_browser: bool = True):
        self.firecrawl_key = firecrawl_key
        self.firecrawl_base = firecrawl_base.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5"})
        self.browser = Browser(use_browser)
        self.cache: dict[str, Extraction | None] = {}
        self.firecrawl_calls = 0
        self.firecrawl_limit = max(0, int(os.getenv("FIRECRAWL_MAX_REQUESTS", "220")))

    def http_get(self, url: str) -> str | None:
        for attempt in range(2):
            try:
                resp = self.session.get(url, timeout=17, allow_redirects=True)
                if resp.status_code in (429, 500, 502, 503):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if resp.ok and "html" in resp.headers.get("content-type", "").lower():
                    return resp.text[:1_500_000]
            except requests.RequestException:
                pass
        return None

    def firecrawl_scrape(self, url: str) -> tuple[str | None, str | None]:
        if not self.firecrawl_key or self.firecrawl_calls >= self.firecrawl_limit:
            return None, None
        self.firecrawl_calls += 1
        try:
            resp = self.session.post(
                self.firecrawl_base + "/scrape",
                json={"url": url, "formats": ["html", "markdown"], "onlyMainContent": True, "maxAge": 0},
                headers={"Authorization": "Bearer " + self.firecrawl_key}, timeout=75,
            )
            if not resp.ok:
                LOG.warning("Firecrawl scrape HTTP %s on %s", resp.status_code, url)
                return None, None
            data = resp.json().get("data") or {}
            return data.get("html"), data.get("markdown")
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Firecrawl scrape failed %s: %s", url, str(exc)[:120])
            return None, None

    def find_other_url(self, title: str, author: str, current_url: str) -> list[str]:
        if not self.firecrawl_key or self.firecrawl_calls >= self.firecrawl_limit:
            return []
        self.firecrawl_calls += 1
        # Query is restricted to known book websites, and every returned URL is validated.
        query = '"' + title[:95].replace('"', "") + '" ' + author[:65].replace('"', "") + ' книга описание'
        try:
            resp = self.session.post(
                self.firecrawl_base + "/search",
                json={"query": query, "limit": 5},
                headers={"Authorization": "Bearer " + self.firecrawl_key}, timeout=35,
            )
            if not resp.ok:
                return []
            hits = (resp.json().get("data") or {}).get("web", [])
            result = []
            for item in hits:
                url = str(item.get("url", ""))
                txt = (str(item.get("title", "")) + ' ' + str(item.get("description", ""))).lower().replace("ё", "е")
                parts = [x for x in re.findall(r"[a-zа-я0-9]{3,}", title.lower().replace("ё", "е")) if len(x) >= 4]
                author_words = [x for x in re.findall(r"[a-zа-я]{4,}", author.lower().replace("ё", "е"))]
                author_ok = not author_words or any(part in txt for part in author_words)
                if (trusted_url(url) and url != current_url and author_ok and
                    sum(p in txt for p in parts) >= min(2, len(parts)) and parts):
                    result.append(url)
            return result[:3]
        except (requests.RequestException, ValueError):
            return []

    def get(self, url: str) -> Extraction | None:
        if not trusted_url(url):
            return None
        if url in self.cache:
            return self.cache[url]
        desc = from_html(self.http_get(url) or "")
        if desc:
            result = Extraction(desc, url, "direct")
            self.cache[url] = result
            return result
        html, markdown = self.firecrawl_scrape(url)
        desc = from_html(html or "") or from_markdown(markdown or "")
        if desc:
            result = Extraction(desc, url, "firecrawl")
            self.cache[url] = result
            return result
        desc = from_html(self.browser.read(url) or "")
        result = Extraction(desc, url, "browser") if desc else None
        self.cache[url] = result
        return result

    def lookup(self, url: str, title: str, author: str) -> Extraction | None:
        result = self.get(url)
        if result:
            return result
        for alternative in self.find_other_url(title, author, url):
            result = self.get(alternative)
            if result:
                return result
        return None

    def close(self) -> None:
        self.browser.close()
        self.session.close()
