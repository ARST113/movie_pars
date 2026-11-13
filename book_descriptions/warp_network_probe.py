"""Compare publisher-page extraction from GitHub Actions with and without Cloudflare WARP.

This probe deliberately uses no Firecrawl API, no LLM API, and performs no Sheets writes.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

from extraction import Retriever

URLS = [
    # Book24: dominant source among the remaining rows.
    "https://book24.ru/product/malchiki-iz-follz-6743763/",
    "https://book24.ru/product/zhizn-vzaymy-ili-u-neba-lyubimchikov-net-3369644/",
    "https://book24.ru/product/dnevnik-pamyati-186595/",
    # Other stores and publishers with currently empty description cells.
    "https://www.labirint.ru/books/1030549/",
    "https://www.labirint.ru/books/1031492/",
    "https://www.piter.com/collection/top-2/product/rag-i-generativnyy-ii-sozdaem-sobstvennye-rag-payplayny-s-pomoschyu-llamaindex-deep-lake-i-pinecon",
    "https://www.chitai-gorod.ru/product/chasodei-chasovoy-klyuch-kniga-1-2271897",
    "https://www.chitai-gorod.ru/product/kniga-smesariki-3158849",
    "https://eksmo.ru/book/pomogi-mne-umeret-ITD1501784/",
    "https://eksmo.ru/book/poslednee-chto-nuzhno-szhech-ITD1381596/",
    "https://eksmo.ru/book/topor-bogomola-ITD1352922/",
    "https://www.mann-ivanov-ferber.ru/catalog/product/ne-meshaj-sebe-zhit/",
    "https://www.respublica.ru/knigi/bestsellery/472001-ni-sy-bud-uveren-v-svoih-silah-i-ne-pozvolyay-somneniyam-meshat-tebe-dvigatsya-vpered",
]


def main() -> None:
    mode = os.environ.get("PROBE_MODE", "baseline")
    path = Path("warp-probe-results.json")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    retriever = Retriever("", use_browser=True)
    try:
        for url in URLS:
            started = time.monotonic()
            item = {
                "mode": mode,
                "domain": urlparse(url).hostname,
                "url": url,
                "ok": False,
                "method": None,
                "chars": 0,
                "seconds": 0,
                "error": None,
            }
            try:
                result = retriever.get(url)
                if result:
                    item["ok"] = True
                    item["method"] = result.method
                    item["chars"] = len(result.description)
            except Exception as exc:  # keep the remaining probes running
                item["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
            item["seconds"] = round(time.monotonic() - started, 2)
            previous.append(item)
            print(
                f"{mode:8} {item['domain']:28} "
                f"ok={str(item['ok']):5} method={str(item['method']):8} "
                f"chars={item['chars']:4} seconds={item['seconds']:6}"
            )
    finally:
        retriever.close()
    path.write_text(json.dumps(previous, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
