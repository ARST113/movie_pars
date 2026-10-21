import importlib.util
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('builder',ROOT/'scripts/build_catalog.py')
builder=importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class BuildTests(unittest.TestCase):
    def test_json(self):
        catalog=json.loads((ROOT/'tests/demo_catalog.json').read_text())
        rows=list(builder.json_rows(catalog))
        self.assertEqual(len(rows),4)
        self.assertEqual(rows[3][2],'')
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'test.sqlite'
            builder.build(p,rows,'test')
            db=sqlite3.connect(p)
            self.assertEqual(db.execute('select count(*) from songs').fetchone()[0],4)
            self.assertEqual(db.execute('select count(*) from songs where anime_title=""').fetchone()[0],1)
            self.assertEqual(db.execute('select count(*) from songs where song_type="Opening"').fetchone()[0],2)
            db.close()
    def test_original_sqlite_layout(self):
        src=Path('/mnt/data/anisongdb_export_test/raw_40.db')
        if not src.exists():self.skipTest('Previous fixture not available')
        with sqlite3.connect(src) as db: r=list(builder.sqlite_rows(db))
        self.assertEqual(len(r),1)
        self.assertEqual(len(r[0]),11)

if __name__=='__main__':unittest.main()
