"""Вливка локально собранных ответов РЗН (2026-08-29).

Реестр заблокировал облачные датацентры (ConnectError из Actions три
запуска подряд); заказчик собирает сырые ответы со своего IP скриптом
tools/rzn_local.py → rzn_dump.jsonl. Здесь файл проходит ТОТ ЖЕ парсер
и ту же запись, что онлайн-путь (parse_rows + save_licenses): сверка ИНН
ответа, is_med по номеру, приложения с адресами, отрицательный результат —
тоже запись. Источник помечается: «реестр РЗН, локальный сбор заказчика».

Запуск: python -m src.rzn_import <файл.jsonl>
"""

import json
import sqlite3
import sys

from src.rzn_licenses import ensure_tables, parse_rows, save_licenses


def import_dump(db: sqlite3.Connection, path: str) -> dict:
    ensure_tables(db)
    stats = {"строк файла": 0, "влито ИНН": 0, "лицензий": 0, "мед": 0,
             "битых строк": 0, "уже были": 0}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        stats["строк файла"] += 1
        try:
            rec = json.loads(line)
            inn = str(rec["inn"]).strip()
            rows = (rec["data"] or {}).get("data") or []
        except Exception:  # noqa: BLE001 — битая строка не валит вливку
            stats["битых строк"] += 1
            continue
        already = db.execute("SELECT 1 FROM rzn_checked WHERE inn=? AND "
                             "status='проверен'", (inn,)).fetchone()
        if already:
            stats["уже были"] += 1
            continue
        lic = parse_rows(inn, rows)
        res = save_licenses(db, inn, lic)
        stats["влито ИНН"] += 1
        stats["лицензий"] += res.get("licenses", 0)
        stats["мед"] += res.get("med", 0)
    db.commit()
    return stats


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python -m src.rzn_import <rzn_dump.jsonl>")
        sys.exit(1)
    db = sqlite3.connect("data/osint.db")
    print(import_dump(db, sys.argv[1]))
