#!/usr/bin/env python3
"""Shikimori Russian names -> local AniSongDB SQLite / static JSON cache.

Avoids runtime requests from GitHub Pages and only joins verified MyAnimeList IDs.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GRAPHQL = 'https://shikimori.one/api/graphql'
PUBLIC_CACHE = 'https://arst113.github.io/movie_pars/data/shikimori_ru.json'
QUERY = '''query ($ids: String!) {
  animes(ids: $ids, limit: 50) { id malId russian licenseNameRu }
}'''
USER_AGENT = 'AniSongExplorer/1.1 (ARST113/movie_pars; public non-commercial metadata index)'


def load_previous(path: Path, remote: str = PUBLIC_CACHE) -> dict:
    sources = []
    if path.exists():
        sources.append(('local', path.read_bytes()))
    else:
        try:
            req = Request(remote, headers={'User-Agent': USER_AGENT})
            with urlopen(req, timeout=20) as f:
                sources.append(('published Pages', f.read(8 * 1024 * 1024)))
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            print('No previous Shikimori mapping:', exc, flush=True)
    for source, raw in sources:
        try:
            payload = json.loads(raw)
            result = payload.get('titles', {})
            if isinstance(result, dict):
                print(f'Loaded {len(result)} title mappings from {source}', flush=True)
                return {str(k): v for k, v in result.items() if str(k).isdigit() and isinstance(v, dict)}
        except (ValueError, TypeError):
            continue
    return {}


def post_batch(ids: list[str], attempts: int = 4) -> list[dict]:
    data = json.dumps({'query': QUERY, 'variables': {'ids': ','.join(ids)}}).encode('utf-8')
    for attempt in range(attempts):
        req = Request(GRAPHQL, data=data, headers={
            'User-Agent': USER_AGENT, 'Accept': 'application/json',
            'Content-Type': 'application/json',
        }, method='POST')
        try:
            with urlopen(req, timeout=35) as f:
                response = json.load(f)
            if response.get('errors'):
                raise RuntimeError('GraphQL errors: ' + json.dumps(response['errors'], ensure_ascii=False)[:400])
            results = response.get('data', {}).get('animes')
            if not isinstance(results, list):
                raise RuntimeError('GraphQL missing data.animes list')
            return results
        except HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            delay = float(exc.headers.get('Retry-After', '0') or 0)
            wait = max(delay, 2 ** attempt * 3)
        except (URLError, TimeoutError, OSError):
            wait = 2 ** attempt * 3
        if attempt == attempts - 1:
            raise RuntimeError('Shikimori API did not respond after retries')
        print(f'Shikimori temporary failure; retry in {wait:.1f} seconds', flush=True)
        time.sleep(min(wait, 60))
    raise AssertionError('unreachable')


def run(db_path: Path, cache_path: Path, delay: float = 1.1, max_requests: int = 0):
    conn = sqlite3.connect(db_path)
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(songs)')}
        if not {'mal_id', 'anime_russian', 'anime_ru_license'} <= columns:
            raise ValueError('Database schema needs mal_id, anime_russian, anime_ru_license')
        ids = sorted((str(row[0]) for row in conn.execute("SELECT DISTINCT mal_id FROM songs WHERE mal_id <> ''") if str(row[0]).isdigit()), key=int)
        cached = load_previous(cache_path)
        missing = [v for v in ids if v not in cached]
        print(f'Anime MAL IDs: {len(ids)}, cache: {len(cached)}, missing: {len(missing)}', flush=True)
        calls = 0
        total_new = 0
        for start in range(0, len(missing), 45):
            if max_requests and calls >= max_requests:
                break
            batch = missing[start:start + 45]
            try:
                animes = post_batch(batch)
            except Exception as exc:
                print(f'WARNING: Shikimori unavailable, keeping previous titles: {exc}', flush=True)
                break
            requested = set(batch)
            for anime in animes:
                try:
                    mid = str(int(anime.get('malId') or 0))
                except (ValueError, TypeError):
                    continue
                if mid not in requested:
                    continue
                ru = str(anime.get('russian') or '').strip()
                license_ru = str(anime.get('licenseNameRu') or '').strip()
                cached[mid] = {'russian': ru, 'licenseNameRu': license_ru}
                if ru or license_ru:
                    total_new += 1
            calls += 1
            if calls == 1 or calls % 10 == 0:
                print(f'Shikimori requests {calls}, mapped Russian titles +{total_new}', flush=True)
            if start + 45 < len(missing):
                time.sleep(max(delay, 0.85))
        rows = [(v.get('russian') or '', v.get('licenseNameRu') or '', k)
                for k, v in cached.items() if k in set(ids)]
        conn.executemany('UPDATE songs SET anime_russian = ?, anime_ru_license = ? WHERE mal_id = ?', rows)
        populated = conn.execute("SELECT COUNT(*) FROM songs WHERE anime_russian <> '' OR anime_ru_license <> ''").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('shikimori_source', 'https://shikimori.one/api/graphql')")
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('shikimori_matched_songs', ?)", (str(populated),))
        conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES ('shikimori_update_utc', ?)", (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
        conn.commit()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'version': 1, 'updated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'titles': cached}, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        tmp.replace(cache_path)
        print(f'Russian matching complete: {populated} song rows, {len(rows)} MAL IDs, {calls} requests, {total_new} added titles', flush=True)
        return populated
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--delay', type=float, default=1.1)
    parser.add_argument('--max-requests', type=int, default=0)
    args = parser.parse_args()
    run(args.db, args.cache, args.delay, args.max_requests)

if __name__ == '__main__':
    main()
