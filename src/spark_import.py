"""Импорт выборки СПАРК — новый входной слой (заказчик, 2026-08-25,
promt_spark_krug: от слепого поиска к очерченному кругу).

Discovery слоями L1-L7 ВЫВЕДЕН ИЗ ФЛОУ (не удалён): полноту теперь даёт
сплошной перебор юрлиц с медицинской лицензией из СПАРК. Осознанные риски
заказчика (не обсуждаются): ИП в выборку не попадают; лицензия привязана
к юрлицу, возможны расхождения с местом фактического оказания услуг.

Формат файла: лист report, шапка на строке 4, данные с 5-й. «Регион
регистрации» содержит ЦЕНТРАЛЬНЫЙ ГОРОД выборки (фильтры выгрузки были
городскими; пометка «область» в значении поля вводит в заблуждение) —
поэтому регион маппится на город справочником ниже, дополнительная
фильтрация по адресу не требуется.

Запуск: python -m src.spark_import --file <xlsx>
"""

import argparse
import re
import sqlite3

import openpyxl

from src.validators import validate_inn, validate_ogrn

# «Регион регистрации» → центральный город (единица анализа проекта)
REGION_CITY = {
    "Башкортостан (Республика)": "Уфа",
    "Пермский край": "Пермь",
    "Самарская область": "Самара",
    "Республика Татарстан": "Казань",
    "Красноярский край": "Красноярск",
    "Нижегородская область": "Нижний Новгород",
    "Тюменская область": "Тюмень",
    "Челябинская область": "Челябинск",
    "Ростовская область": "Ростов-на-Дону",
    "Новосибирская область": "Новосибирск",
    "Свердловская область": "Екатеринбург",
    "Краснодарский край": "Краснодар",
    "Воронежская область": "Воронеж",
    "Саратовская область": "Саратов",
    "Дагестан (Республика)": "Махачкала",
    "Омская область": "Омск",
    "Алтайский край": "Барнаул",
    "Волгоградская область": "Волгоград",
}


def normalize_site_domain(url: str | None) -> str | None:
    if not url:
        return None
    u = str(url).strip().lower()
    # СПАРК может отдать несколько сайтов через запятую/точку с запятой —
    # берётся первый (такт 3: «helix72.ru, azbykamed.ru» давал домен с запятой)
    u = re.split(r"[,;\s]+", u)[0]
    u = re.sub(r"^https?://", "", u).split("/")[0].split("?")[0]
    if u.startswith("www."):
        u = u[4:]
    return u or None


def ensure_companies_table(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        inn TEXT PRIMARY KEY,          -- ключ дедупликации нового флоу
        ogrn TEXT, name TEXT, region TEXT, city TEXT,
        industry TEXT, revenue_2025 INTEGER,
        site_spark TEXT,               -- как пришло из СПАРК (факт выгрузки)
        site TEXT,                     -- рабочий домен после проверки/достройки
        site_source TEXT,              -- СПАРК подтверждён / транслитерация /
                                       -- прежняя база / платный поиск / не найден
        site_status TEXT,              -- ok / нерабочий / чужой / заглушка / не проверен
        shared_domain_with TEXT,       -- ИНН других юрлиц этого же домена
        -- Фаза 1 (один заход, оба суждения; только по сайту)
        med_judgment TEXT,             -- медорганизация / не медорганизация / не определено
        med_basis TEXT,                -- цитата и URL — обязательны
        profile_judgment TEXT,         -- похож / не похож / не определено
        profile_matches_n INTEGER,
        profile_matches TEXT,          -- перечень совпавших позиций
        positions_seen INTEGER,        -- позиций услуг увидено в фазе 1
        price_file_url TEXT,           -- найденная ссылка на прайс-файл (PDF/XLS/DOC)
        fetch_status TEXT,             -- ok / требует проверки / сайт не найден
        fetch_level INTEGER,
        pages_seen INTEGER,
        checked_at TEXT);
    """)
    db.commit()


def import_spark(path: str, db: sqlite3.Connection) -> dict:
    ensure_companies_table(db)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["report"]
    stats = {"total": 0, "inserted": 0, "bad_inn": 0, "bad_ogrn": 0,
             "with_site": 0, "shared_domains": 0}
    rows = []
    for r in ws.iter_rows(min_row=5, values_only=True):
        if not r[1]:
            continue
        stats["total"] += 1
        num, name, ogrn, site, inn, region, industry, revenue = r[:8]
        inn = str(inn).strip()
        ogrn = str(ogrn).strip() if ogrn else None
        if not validate_inn(inn):
            stats["bad_inn"] += 1   # запись не создаётся — ключ невалиден
            continue
        if ogrn and not validate_ogrn(ogrn):
            stats["bad_ogrn"] += 1
            ogrn = None             # G5: невалидный формат не записывается
        dom = normalize_site_domain(site)
        if dom:
            stats["with_site"] += 1
        rows.append((inn, ogrn, str(name).strip(), region,
                     REGION_CITY.get(region, region),
                     industry, int(revenue) if revenue else None, dom))
    # один домен на несколько ИНН = сеть под несколькими юрлицами:
    # обходить ОДИН РАЗ, связывать со всеми ИНН (задвоение недопустимо)
    by_dom: dict[str, list[str]] = {}
    for row in rows:
        if row[7]:
            by_dom.setdefault(row[7], []).append(row[0])
    for row in rows:
        inn, dom = row[0], row[7]
        shared = ([i for i in by_dom.get(dom, []) if i != inn]
                  if dom else [])
        db.execute(
            "INSERT OR REPLACE INTO companies (inn, ogrn, name, region, city, "
            "industry, revenue_2025, site_spark, shared_domain_with, "
            "site_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*row, "; ".join(shared) or None, "не проверен"))
        db.execute("UPDATE companies SET site=site_spark, "
                   "site_source='СПАРК (не проверен)' WHERE inn=? "
                   "AND site_spark IS NOT NULL", (inn,))
    stats["inserted"] = len(rows)
    stats["shared_domains"] = sum(1 for d, inns in by_dom.items() if len(inns) > 1)
    db.commit()
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--db", default="data/osint.db")
    args = ap.parse_args()
    con = sqlite3.connect(args.db)
    con.execute("PRAGMA busy_timeout=15000")
    st = import_spark(args.file, con)
    print(f"импорт СПАРК: всего {st['total']}, записано {st['inserted']}, "
          f"с сайтом {st['with_site']}, невалидных ИНН {st['bad_inn']}, "
          f"ОГРН отбито {st['bad_ogrn']}, сетевых доменов {st['shared_domains']}")
