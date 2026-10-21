import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_catalog', ROOT/'scripts/build_catalog.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
spec = importlib.util.spec_from_file_location('enrich_shikimori', ROOT/'scripts/enrich_shikimori.py')
shiki = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shiki)

def test_enrichment():
    with tempfile.TemporaryDirectory() as temp:
        d = Path(temp)
        catalog = {'anime': {'123': {'animeJPName':'Naruto','animeENName':'Naruto','animeVintage':'Fall 2002','animeType':'TV','linked_ids':{'myanimelist':20}}}, 'songs':[{'annId':123,'songType':1,'songName':'Haruka Kanata','songArtist':'AKFG'}]}
        db = d/'test.sqlite'; cache = d/'ru.json'
        build.build(db, build.json_rows(catalog), 'test')
        with patch.object(shiki, 'load_previous', return_value={}), patch.object(shiki, 'post_batch',return_value=[{'id':'20','malId':'20','russian':'Наруто','licenseNameRu':'Наруто: Ураганные хроники'}]):
            populated = shiki.run(db, cache, 0, 1)
        assert populated == 1
        with sqlite3.connect(db) as conn:
            assert conn.execute('select anime_russian,anime_ru_license,mal_id from songs').fetchone() == ('Наруто','Наруто: Ураганные хроники','20')
        assert json.loads(cache.read_text())['titles']['20']['russian']=='Наруто'
        with patch.object(shiki, 'load_previous', return_value={'20': {'russian':'Наруто','licenseNameRu':''}}), patch.object(shiki,'post_batch',side_effect=AssertionError('cache should prevent requests')):
            assert shiki.run(db, cache) == 1

if __name__ == '__main__':
    test_enrichment()
    print('Shikimori enrichment tested')
