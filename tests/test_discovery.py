"""Тесты дедупликации G0 и разбора выдачи discovery (без сети)."""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.dedup import normalize_domain, normalize_name
from src.discovery import CandidateQueue, parse_yandex_xml

XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response>
<results><grouping>
<group><doc><url>https://www.clinic-a.ru/derm</url><domain>www.clinic-a.ru</domain>
<title>Клиника <hlword>А</hlword> — дерматология</title></doc></group>
<group><doc><url>https://prodoctorov.ru/kazan/lpu/123-clinic-a/</url><domain>prodoctorov.ru</domain>
<title>Клиника А на ПроДокторов</title></doc></group>
<group><doc><url>https://prodoctorov.ru/kazan/lpu/456-clinic-b/</url><domain>prodoctorov.ru</domain>
<title>Клиника Б</title></doc></group>
</grouping></results></response></yandexsearch>"""


class TestNormalization(unittest.TestCase):
    def test_opf_and_stopwords_removed(self):
        self.assertEqual(normalize_name('ООО «Клиника Чистая Кожа»'), "чистая кожа")
        self.assertEqual(normalize_name("Медицинский центр Элика"), "элика")

    def test_homoglyphs_latin_to_cyrillic(self):
        # 'Eлика' с латинской E == 'Елика' с кириллической после нормализации
        self.assertEqual(normalize_name("Eлика"), normalize_name("Елика"))

    def test_domain_normalization(self):
        self.assertEqual(normalize_domain("https://WWW.Clinic-A.ru/page?x=1"), "clinic-a.ru")
        self.assertIsNone(normalize_domain(""))


class TestParseAndQueue(unittest.TestCase):
    def _db(self):
        db = sqlite3.connect(":memory:")
        db.executescript("""
        CREATE TABLE candidates (dedup_key TEXT PRIMARY KEY, title TEXT, url TEXT,
          domain TEXT, kind TEXT, discovered_by_query TEXT, source_id TEXT,
          discovered_at TEXT);""")
        return db

    def test_parse_extracts_docs_and_strips_hlword(self):
        docs = parse_yandex_xml(XML)
        self.assertEqual(len(docs), 3)
        self.assertEqual(docs[0]["domain"], "clinic-a.ru")
        self.assertEqual(docs[0]["title"], "Клиника А — дерматология")

    def test_site_dedup_by_domain(self):
        q = CandidateQueue(self._db())
        docs = parse_yandex_xml(XML)
        self.assertTrue(q.add(docs[0], "L2-x", "yandex_search_api"))
        # та же клиника, другая страница того же домена → дубль
        again = dict(docs[0], url="https://clinic-a.ru/price")
        self.assertFalse(q.add(again, "L2-y", "yandex_search_api"))

    def test_aggregator_cards_not_collapsed(self):
        q = CandidateQueue(self._db())
        docs = parse_yandex_xml(XML)
        self.assertTrue(q.add(docs[1], "L2-x", "yandex_search_api"))
        self.assertTrue(q.add(docs[2], "L2-x", "yandex_search_api"))  # другая карточка — не дубль
        self.assertFalse(q.add(docs[1], "L2-y", "yandex_search_api"))  # та же карточка — дубль

    def test_queue_survives_reopen(self):
        db = self._db()
        q = CandidateQueue(db)
        q.add(parse_yandex_xml(XML)[0], "L2-x", "yandex_search_api")
        q2 = CandidateQueue(db)  # перезапуск: ключи читаются из БД
        self.assertFalse(q2.add(parse_yandex_xml(XML)[0], "L2-y", "yandex_search_api"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
