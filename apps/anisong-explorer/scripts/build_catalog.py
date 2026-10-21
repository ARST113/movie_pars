#!/usr/bin/env python3
"""Build a compact, six-column AniSongDB SQLite database for GitHub Pages.

Input may be the public AniSongDB JSON catalog or the author's sample SQLite.
Requires Python 3.10+, standard library only.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import gzip
import json
import sqlite3
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import time

LIVE = 'https://anisongdb.com/api/catalog_dump'
SAMPLE = 'https://raw.githubusercontent.com/xSardine/AMQ-Artists-DB/main/backEnd/app/data/Enhanced-AMQ-Database.sample.db'
MAX_RESPONSE_BYTES = 300 * 1024 * 1024
KINDS = {1: 'Opening', 2: 'Ending', 3: 'Insert'}


def value(x):
    return '' if x is None else str(x).strip()


def kind(x):
    try:
        return KINDS.get(int(x), value(x))
    except (ValueError, TypeError):
        return value(x)


def http_get(url, max_attempts=3):
    for n in range(max_attempts):
        try:
            req = Request(url, headers={'User-Agent': 'AniSongDB-Catalog-GitHub-Pages/1.0', 'Accept': '*/*', 'Accept-Encoding': 'identity'})
            with urlopen(req, timeout=140) as resp:
                out = resp.read(MAX_RESPONSE_BYTES+1)
                if len(out) > MAX_RESPONSE_BYTES:
                    raise RuntimeError('Server sent too much data')
                return out
        except (URLError, TimeoutError, HTTPError) as exc:
            if isinstance(exc, HTTPError) and exc.code not in (429, 500, 502, 503, 504):
                raise
            if n >= max_attempts - 1:
                raise
            print(f'Retrying {url}: {exc}', flush=True)
            time.sleep(2 ** n)


def unpack_json(raw):
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    content = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(content, dict) or not isinstance(content.get('anime'), dict) or not isinstance(content.get('songs'), list):
        raise ValueError('Not an AniSongDB catalog')
    return content


def json_rows(catalog):
    anime = catalog['anime']
    for song in catalog['songs']:
        ann_id = song.get('annId')
        a = anime.get(str(ann_id), {})
        jp = value(a.get('animeJPName'))
        en = value(a.get('animeENName'))
        alts = a.get('animeAltName') or []
        if isinstance(alts, str):
            alts = alts.split('\\$')
        yield (
            value(a.get('animeVintage')), value(a.get('animeType')),
            jp or en, kind(song.get('songType')),
            value(song.get('songName')), value(song.get('songArtist')),
            en, jp, ' | '.join(map(value, alts)), value(ann_id),
        )


def sqlite_rows(db):
    tables = {a[0] for a in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    if 'songsFull' in tables:
        # Column indices are those published in backEnd/app/db_types.py.
        for s in db.execute('SELECT * FROM songsFull'):
            if len(s) < 40:
                raise ValueError('Invalid songsFull schema: expected >= 40 columns')
            jp, en = value(s[6]), value(s[7])
            yield (value(s[10]), value(s[11]), jp or en, kind(s[16]),
                   value(s[20]), value(s[22]), en, jp,
                   value(s[9]).replace('\\$', ' | '), value(s[0]))
        return
    if 'songs' in tables:
        cols = {a[1] for a in db.execute('PRAGMA table_info(songs)')}
        if {'season', 'anime_type', 'anime_name', 'song_type', 'song_name', 'artist'} <= cols:
            for s in db.execute('SELECT season,anime_type,anime_name,song_type,song_name,artist FROM songs'):
                yield (*map(value, s), '', '', '', '')
            return
        if {'season', 'anime_type', 'anime_title', 'song_type', 'song_title', 'artist'} <= cols:
            for s in db.execute('SELECT season,anime_type,anime_title,song_type,song_title,artist,anime_english,anime_japanese,anime_alt,ann_id FROM songs'):
                yield tuple(map(value,s))
            return
    raise ValueError(f'Unsupported source SQLite schema (tables: {sorted(tables)})')


def build(path, rows, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.sqlite.tmp')
    temp.unlink(missing_ok=True)
    conn = sqlite3.connect(temp)
    count = 0
    try:
        conn.executescript('''
        PRAGMA journal_mode=DELETE;
        CREATE TABLE songs (
          row_id INTEGER PRIMARY KEY,
          season TEXT NOT NULL DEFAULT '',
          anime_type TEXT NOT NULL DEFAULT '',
          anime_title TEXT NOT NULL DEFAULT '',
          song_type TEXT NOT NULL DEFAULT '',
          song_title TEXT NOT NULL DEFAULT '',
          artist TEXT NOT NULL DEFAULT '',
          anime_english TEXT NOT NULL DEFAULT '',
          anime_japanese TEXT NOT NULL DEFAULT '',
          anime_alt TEXT NOT NULL DEFAULT '',
          ann_id TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        batch=[]
        for row in rows:
            if len(row) != 10:
                raise ValueError('An input record has the wrong number of columns')
            batch.append(tuple(map(value,row)))
            if len(batch) >= 2500:
                conn.executemany('''INSERT INTO songs (
                  season,anime_type,anime_title,song_type,song_title,artist,
                  anime_english,anime_japanese,anime_alt,ann_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?)''', batch)
                count+=len(batch);batch.clear()
        if batch:
            conn.executemany('''INSERT INTO songs (
              season,anime_type,anime_title,song_type,song_title,artist,
              anime_english,anime_japanese,anime_alt,ann_id
            ) VALUES (?,?,?,?,?,?,?,?,?,?)''', batch)
            count+=len(batch)
        conn.executemany('INSERT INTO metadata VALUES (?,?)', [
            ('source', source),
            ('generated_utc', datetime.now(timezone.utc).isoformat(timespec='seconds')),
            ('song_count', str(count)),
            ('schema_version', '1'),
        ])
        conn.executescript('''
        CREATE INDEX idx_songs_season ON songs(season);
        CREATE INDEX idx_songs_type ON songs(song_type);
        CREATE INDEX idx_anime_type ON songs(anime_type);
        CREATE INDEX idx_anime_title ON songs(anime_title);
        CREATE INDEX idx_artist ON songs(artist);
        ''')
        conn.commit()
        rows_written = conn.execute('SELECT count(*) FROM songs').fetchone()[0]
        if count != rows_written:
            raise ValueError(f'Invalid row count: {count} vs {rows_written}')
        print(f'Built {path}: {count} songs / {path.stat().st_size if path.exists() else "pending"} bytes')
    except Exception:
        conn.close()
        temp.unlink(missing_ok=True)
        raise
    conn.close()
    temp.replace(path)
    print(f'File size: {path.stat().st_size / 1024**2:.2f} MiB')
    return count


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source', default='auto', help='auto, live, sample, or path to .db/.sqlite/.json/.json.gz')
    p.add_argument('--output', type=Path, default=Path('site/data/catalog.sqlite'))
    a=p.parse_args()
    if a.source == 'auto':
        try:
            payload = unpack_json(http_get(LIVE))
            count=build(a.output, json_rows(payload), LIVE)
        except Exception as exc:
            print(f'WARNING: LIVE API unavailable: {exc}. Falling back to sample SQLite.', flush=True)
        else:
            if count <= 0: raise RuntimeError('Empty live catalog')
            return
        raw=http_get(SAMPLE)
        sample=a.output.parent / '_temporary_sample.db'
        sample.write_bytes(raw)
        try:
            with sqlite3.connect(sample) as db:
                build(a.output, sqlite_rows(db), SAMPLE + ' (sample; media links stripped)')
        finally:
            sample.unlink(missing_ok=True)
        return
    if a.source in ('live','sample'):
        if a.source=='live':
            build(a.output, json_rows(unpack_json(http_get(LIVE))), LIVE)
        else:
            sample=a.output.parent / '_temporary_sample.db'
            a.output.parent.mkdir(parents=True, exist_ok=True)
            sample.write_bytes(http_get(SAMPLE))
            try:
                with sqlite3.connect(sample) as db:
                    build(a.output, sqlite_rows(db), SAMPLE + ' (sample; media links stripped)')
            finally:
                sample.unlink(missing_ok=True)
        return
    src=Path(a.source)
    if not src.is_file():
        p.error(f'Input file not found: {src}')
    if src.suffix.lower() in ('.db','.sqlite','.sqlite3'):
        with sqlite3.connect(src) as db:
            build(a.output, sqlite_rows(db), src.name)
    else:
        raw=src.read_bytes()
        build(a.output, json_rows(unpack_json(raw)), src.name)

if __name__=='__main__': main()
