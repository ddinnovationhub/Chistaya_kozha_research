"""Сохранённые тексты страниц — ОТДЕЛЬНЫЙ файл базы data/page_texts.db.

ЗАЧЕМ ВЫНЕСЕНЫ (2026-09-04). Тексты скачанных страниц весили 42 из 102 МБ
общей базы, и data/osint.db дважды за день упиралась в жёсткий лимит
GitHub — 100 МБ на файл: сначала прогон 39 не смог закоммитить результат,
потом слияние второй волны шардов (двухчасовая работа пяти прогонов
спаслась только из артефактов). После выноса osint.db весит 54 МБ и имеет
запас роста, тексты — свои 48 МБ, оба файла коммитятся отдельно.

ПОЧЕМУ ИМЕННО ТЕКСТЫ. Они сохраняются «на будущее» — для повторной
классификации без нового обхода и для маппинга услуг этапа 6, — но ни
один шаг конвейера их сейчас не читает. То есть это самый тяжёлый и
одновременно самый холодный кусок базы: его отсутствие в рабочем файле не
замедляет ни один этап.

Тот же приём уже применён к прайсам (data/prices.db, заказчик 2026-09-02).
"""

import os
import sqlite3
import zlib

DB_PATH = "data/page_texts.db"


def open_db(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("""CREATE TABLE IF NOT EXISTS t40_page_texts (
        inn TEXT, url TEXT, text_gz BLOB, PRIMARY KEY (inn, url))""")
    return db


def save_pages(inn: str, pages: dict[str, str], path: str = DB_PATH) -> int:
    """{url: видимый текст} → база. Заменяет прежние страницы этого ИНН."""
    db = open_db(path)
    try:
        db.execute("DELETE FROM t40_page_texts WHERE inn=?", (inn,))
        db.executemany(
            "INSERT OR REPLACE INTO t40_page_texts (inn, url, text_gz) "
            "VALUES (?,?,?)",
            [(inn, u, zlib.compress(t[:120000].encode("utf-8")))
             for u, t in pages.items()])
        db.commit()
        return len(pages)
    finally:
        db.close()


def delete_pages(inns: list[str], path: str = DB_PATH) -> int:
    """Страницы ИНН, у которых сброшен сайт (инвариант чистоты автопилота)."""
    if not inns:
        return 0
    db = open_db(path)
    try:
        n = db.executemany("DELETE FROM t40_page_texts WHERE inn=?",
                           [(i,) for i in inns]).rowcount
        db.commit()
        return max(0, n)
    finally:
        db.close()


def merge_from(src_path: str, inns: list[str], path: str = DB_PATH) -> int:
    """Тексты шарда → общая база (только по ИНН его диапазона)."""
    if not os.path.exists(src_path) or not inns:
        return 0
    db = open_db(path)
    try:
        db.execute("ATTACH DATABASE ? AS s", (f"file:{src_path}?mode=ro",))
        try:
            marks = ",".join("?" * len(inns))
            n = db.execute(
                f"INSERT OR IGNORE INTO t40_page_texts "
                f"SELECT * FROM s.t40_page_texts WHERE inn IN ({marks})",
                inns).rowcount
            db.commit()
            return n
        except sqlite3.OperationalError:
            return 0
        finally:
            db.execute("DETACH DATABASE s")
    finally:
        db.close()
