"""ТЕСТ 20-40 СТРОК обновлённой базы (заказчик, 2026-08-27: «Берём первые
20-40 строк и тестим на них подход. Я вручную смотрю что получается и
решаем запускать ли подход в пром на все строки»).

Склеенный конвейер — все решения разборов 2026-08-26/27 в одном заходе:
1. РЕЕСТР ЛИЦЕНЗИЙ РЗН по ИНН (src/rzn_licenses) — ДО сайтов: наличие
   действующей мед-лицензии и перечень работ приложений — факт реестра;
2. проверка ВСЕХ кандидатов сайтов из ячейки СПАРК гибкой навигацией
   (по тексту ссылок, не жёсткие пути) + лестница ИНН → адрес+название;
3. поисковая достройка ненайденных — только из Actions (ключи в Secrets);
4. обход подтверждённых полным каскадом, «паспорт сайта» (src/passport),
   суждения Б → А (4 исхода) + специальности с сайта;
5. выгрузка Excel на ручную проверку заказчика: лист ИТОГ, паспорта,
   лицензии, полные колонки.

Файл базы задаётся параметром (обновлённую версию заказчик пришлёт);
формат — те же 9 колонок, что и пилот-108.
"""

import datetime
import sqlite3
import sys
import time

import openpyxl

from src.pilot108 import judge_pilot_a, split_site_cell
from src.spark_import import REGION_CITY
from src.validators import validate_inn

DEFAULT_ROWS = 40


def ensure_t40_tables(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS t40_companies (
        row_no INTEGER, inn TEXT PRIMARY KEY, ogrn TEXT, name TEXT,
        sites_raw TEXT, region TEXT, city TEXT, industry TEXT,
        okved_marker TEXT, revenue_2025 TEXT,
        found_site TEXT, site_source TEXT, grade TEXT, grade_evidence TEXT,
        search_attempts INTEGER, search_status TEXT,
        fetch_status TEXT, fetch_level INTEGER, pages_seen INTEGER,
        med_judgment TEXT, med_basis TEXT, mgmt_network TEXT,
        profile_judgment TEXT, profile_matches_n INTEGER,
        profile_matches TEXT, positions_seen INTEGER,
        site_specialties TEXT, passport TEXT, checked_at TEXT);
    CREATE TABLE IF NOT EXISTS t40_positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inn TEXT, domain TEXT, name_raw TEXT, price TEXT, page_url TEXT);
    CREATE TABLE IF NOT EXISTS t40_page_texts (
        inn TEXT, url TEXT, text_gz BLOB, PRIMARY KEY (inn, url));
    """)
    db.commit()


def import_t40(path: str, db: sqlite3.Connection,
               first_n: int = DEFAULT_ROWS) -> dict:
    """Первые N строк файла с 9 колонками пилота. Повторный импорт того же
    файла — идемпотентен (INSERT OR REPLACE по ИНН)."""
    ensure_t40_tables(db)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    stats = {"total": 0, "with_sites": 0, "bad_inn": 0}
    for i, r in enumerate(ws.iter_rows(min_row=2, values_only=True), 1):
        if stats["total"] >= first_n:
            break
        if not r[1]:
            continue
        num, name, ogrn, sites, inn, region, industry, marker, revenue = r[:9]
        inn = str(inn).strip()
        stats["total"] += 1
        if not validate_inn(inn):
            stats["bad_inn"] += 1
            continue
        if split_site_cell(sites):
            stats["with_sites"] += 1
        db.execute(
            "INSERT OR REPLACE INTO t40_companies (row_no, inn, ogrn, name, "
            "sites_raw, region, city, industry, okved_marker, revenue_2025) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (int(num) if num else i, inn, str(ogrn) if ogrn else None,
             str(name).strip(), str(sites) if sites else None, region,
             REGION_CITY.get(region, region), industry,
             str(marker) if marker is not None else None,
             str(revenue) if revenue is not None else None))
    db.commit()
    return stats


def _check_candidates_flex(inn: str, name: str, city: str,
                           candidates: list[str]) -> tuple[str, str, str] | None:
    """Лестница пилота (ИНН → адрес в городе + соответствие названию),
    но страницы собираются ГИБКОЙ навигацией по тексту ссылок —
    эмпирика 2026-08-27: +8 подтверждений из 27 отказов жёстких путей."""
    from src.site_finder import (content_matches_name, flexible_contact_texts,
                                 triple_check)
    for dom in candidates:
        texts = flexible_contact_texts(dom)
        if not texts:
            continue
        chk = triple_check(dom, inn, city, pages_hint=texts)
        if chk["verdict"] == "ИНН":
            return dom, "подтверждён ИНН", chk["evidence"]
        if chk["verdict"] == "адрес" and content_matches_name(
                " ".join(texts), name):
            return dom, "подтверждён адресом", (
                chk["evidence"] + "; содержание соответствует названию")
    return None


def check_sites(db: sqlite3.Connection, budget_sec: float = 1800,
                workers: int = 8) -> dict:
    """Кандидаты из ячейки СПАРК — гибкая проверка. Бесплатно."""
    import concurrent.futures as cf
    rows = [r for r in db.execute(
        "SELECT inn, name, city, sites_raw FROM t40_companies "
        "WHERE sites_raw IS NOT NULL AND found_site IS NULL "
        "AND site_source IS NULL")]
    t0 = time.time()
    stats = {"confirmed_inn": 0, "confirmed_addr": 0, "no": 0}

    def work(item):
        inn, name, city, sites = item
        return inn, _check_candidates_flex(inn, name, city,
                                           split_site_cell(sites)[:3])

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        chunk = workers * 2
        for i in range(0, len(rows), chunk):
            if time.time() - t0 > budget_sec:
                break
            for fut in cf.as_completed(
                    [ex.submit(work, it) for it in rows[i:i + chunk]]):
                inn, res = fut.result()
                if res:
                    dom, grade, ev = res
                    key = "confirmed_inn" if "ИНН" in grade else "confirmed_addr"
                    stats[key] += 1
                    db.execute("UPDATE t40_companies SET found_site=?, grade=?, "
                               "grade_evidence=?, site_source='кандидат из "
                               "выгрузки СПАРК (гибкая навигация)' WHERE inn=?",
                               (dom, grade, ev[:300], inn))
                else:
                    stats["no"] += 1
                    db.execute("UPDATE t40_companies SET site_source="
                               "'кандидаты выгрузки не подтвердились (гибко)' "
                               "WHERE inn=?", (inn,))
                db.commit()
    return stats


def run_search(db: sqlite3.Connection, budget_sec: float = 3600) -> dict:
    """Поисковая достройка ненайденных — ТОЛЬКО из Actions (ключи в Secrets).
    Кандидаты поиска тоже проверяются гибкой навигацией."""
    import base64

    from src.api_client import handle_api_response, yandex_search_raw
    from src.dedup import normalize_domain
    from src.discovery import is_aggregator_domain, parse_yandex_xml
    from src.pilot108 import SEARCH_COST_RUB
    rows = list(db.execute(
        "SELECT inn, name, city FROM t40_companies WHERE found_site IS NULL "
        "AND search_status IS NULL"))
    t0 = time.time()
    stats = {"found_inn": 0, "found_addr": 0, "not_found": 0,
             "spent_rub": 0.0, "done": 0}
    for inn, name, city in rows:
        if time.time() - t0 > budget_sec:
            break
        resp = yandex_search_raw(f"{name} {city}", n=10)
        if handle_api_response(resp, "Яндекс Search API") is None:
            continue
        stats["spent_rub"] += SEARCH_COST_RUB
        results = parse_yandex_xml(
            base64.b64decode(resp.json()["rawData"]).decode("utf-8"))
        cands, seen = [], set()
        for r in results:
            dom = normalize_domain(r.get("url") or "")
            if dom and dom not in seen and not is_aggregator_domain(dom):
                seen.add(dom)
                cands.append(dom)
            if len(cands) >= 5:
                break
        res = _check_candidates_flex(inn, name, city, cands)
        if res:
            dom, grade, ev = res
            key = "found_inn" if "ИНН" in grade else "found_addr"
            stats[key] += 1
            db.execute("UPDATE t40_companies SET found_site=?, grade=?, "
                       "grade_evidence=?, site_source='поиск (название+город, "
                       "гибкая навигация)', search_attempts=?, "
                       "search_status='найден' WHERE inn=?",
                       (dom, grade, ev[:300], len(cands), inn))
        else:
            stats["not_found"] += 1
            db.execute("UPDATE t40_companies SET search_status='сайт не найден', "
                       "search_attempts=? WHERE inn=?", (len(cands), inn))
        stats["done"] += 1
        db.commit()
        time.sleep(1)
    return stats


def crawl_judge(db: sqlite3.Connection, budget_sec: float = 2400,
                workers: int = 4) -> dict:
    """Обход подтверждённых (полный каскад) + паспорт + суждения Б → А
    + специальности с сайта."""
    import concurrent.futures as cf
    import zlib

    from src.classify import load_contours
    from src.extract_site import extract_pages
    from src.html_text import html_to_text
    from src.mapper import build_formulation_index
    from src.passport import build_passport
    from src.phase1 import crawl_light, judge_profile, load_ck_price_index
    form_index = build_formulation_index()
    contours = load_contours()
    ck = load_ck_price_index()
    rows = list(db.execute(
        "SELECT inn, name, city, found_site FROM t40_companies "
        "WHERE found_site IS NOT NULL AND fetch_status IS NULL"))
    t0 = time.time()
    stats = {"ok": 0, "unreachable": 0}

    def work(item):
        inn, name, city, dom = item
        pages, info = crawl_light(dom, form_index, max_level=4)
        if not pages:
            return item, None, None, info
        data = extract_pages(pages, form_index)
        prof = judge_profile(data["services"], form_index, contours, ck)
        a, basis, mgmt = judge_pilot_a(pages, data)
        passport = build_passport(dom, pages, data)
        return item, (a, basis, mgmt, prof, data, passport), pages, info

    now = datetime.datetime.now().isoformat(timespec="seconds")
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        chunk = workers * 2
        for i in range(0, len(rows), chunk):
            if time.time() - t0 > budget_sec:
                break
            for fut in cf.as_completed(
                    [ex.submit(work, it) for it in rows[i:i + chunk]]):
                item, judged, pages, info = fut.result()
                inn, name, city, dom = item
                if judged is None:
                    stats["unreachable"] += 1
                    db.execute("UPDATE t40_companies SET "
                               "fetch_status='Сайт недоступен (уровни 1-4)', "
                               "checked_at=? WHERE inn=?", (now, inn))
                    db.commit()
                    continue
                a, basis, mgmt, prof, data, passport = judged
                db.execute("DELETE FROM t40_positions WHERE inn=?", (inn,))
                for s in data["services"]:
                    db.execute("INSERT INTO t40_positions (inn, domain, "
                               "name_raw, price, page_url) VALUES (?,?,?,?,?)",
                               (inn, dom, s["name"], s.get("price"),
                                s["page_url"]))
                db.execute("DELETE FROM t40_page_texts WHERE inn=?", (inn,))
                for u, p in pages.items():
                    db.execute("INSERT OR REPLACE INTO t40_page_texts "
                               "(inn, url, text_gz) VALUES (?,?,?)",
                               (inn, u, zlib.compress(
                                   html_to_text(p)[:120000].encode("utf-8"))))
                db.execute(
                    "UPDATE t40_companies SET fetch_status='ok', fetch_level=?, "
                    "pages_seen=?, med_judgment=?, med_basis=?, mgmt_network=?, "
                    "profile_judgment=?, profile_matches_n=?, profile_matches=?, "
                    "positions_seen=?, site_specialties=?, passport=?, "
                    "checked_at=? WHERE inn=?",
                    (info["level"], info["pages"], a, basis[:500], mgmt,
                     prof["profile"], prof["matches_n"], prof["matches"],
                     prof["positions_seen"],
                     ", ".join(data["doctor_specialties"]), passport, now, inn))
                stats["ok"] += 1
                db.commit()
    return stats


def export_t40(db: sqlite3.Connection, src_path: str) -> str:
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = openpyxl.Workbook()
    day = datetime.date.today().isoformat()
    BOLD = Font(name="Arial", size=10, bold=True)
    ARIAL = Font(name="Arial", size=10)
    HDR = PatternFill("solid", fgColor="DDE7F3")

    def _sheet(ws, headers, widths):
        for c, h in enumerate(headers, 1):
            cell = ws.cell(1, c, h); cell.font = BOLD; cell.fill = HDR
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(c)].width = w

    # ── ИТОГ: одна строка на компанию, всё существенное рядом ──
    ws = wb.active
    ws.title = "ИТОГ"
    _sheet(ws, ["№", "Название", "ИНН", "Город",
                "РЗН: мед-лицензия", "РЗН: специальности из приложений",
                "РЗН: адресов", "Найденный сайт", "Чем подтверждён",
                "Суждение А (по сайту)", "Основание А",
                "Суждение Б", "Специальности на сайте", "Статус"],
           (6, 30, 13, 12, 16, 45, 9, 22, 20, 22, 50, 16, 30, 24))
    r = 2
    for row in db.execute(
            "SELECT c.row_no, c.name, c.inn, c.city, "
            "k.med_licenses_n, "
            "(SELECT GROUP_CONCAT(specialties, '; ') FROM rzn_licenses l "
            " WHERE l.inn=c.inn AND l.is_med=1), "
            "(SELECT SUM(objects_n) FROM rzn_licenses l "
            " WHERE l.inn=c.inn AND l.is_med=1), "
            "c.found_site, c.grade, c.med_judgment, c.med_basis, "
            "c.profile_judgment, c.site_specialties, "
            "COALESCE(c.fetch_status, c.search_status, c.site_source, '—') "
            "FROM t40_companies c LEFT JOIN rzn_checked k ON k.inn=c.inn "
            "ORDER BY c.row_no"):
        vals = list(row)
        n_med = vals[4]
        vals[4] = ("нет данных" if n_med is None
                   else f"есть ({n_med})" if n_med else "нет")
        for c, v in enumerate(vals, 1):
            cell = ws.cell(r, c, v if v is not None else "")
            cell.font = ARIAL
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        r += 1

    # ── Лицензии РЗН: по одной строке на лицензию ──
    ws = wb.create_sheet("Лицензии_РЗН")
    _sheet(ws, ["ИНН", "Компания (лицензиат)", "Номер", "Дата", "Мед",
                "Действие", "Аннулирована", "Прекращена", "Адресов",
                "Специальности из приложений", "Орган"],
           (13, 32, 24, 11, 6, 12, 14, 14, 9, 60, 30))
    r = 2
    for row in db.execute(
            "SELECT inn, licensee, number, date, "
            "CASE is_med WHEN 1 THEN 'да' ELSE '' END, valid_to, annulled, "
            "terminated, objects_n, specialties, authority FROM rzn_licenses "
            "WHERE inn IN (SELECT inn FROM t40_companies) ORDER BY inn"):
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v if v is not None else "")
            cell.font = ARIAL
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        r += 1

    # ── Паспорта сайтов: сырьё для ручной проверки суждений ──
    ws = wb.create_sheet("Паспорта")
    _sheet(ws, ["№", "Название", "ИНН", "Сайт", "Паспорт сайта"],
           (6, 28, 13, 20, 150))
    r = 2
    for row in db.execute(
            "SELECT row_no, name, inn, found_site, passport FROM t40_companies "
            "WHERE passport IS NOT NULL ORDER BY row_no"):
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v if v is not None else "")
            cell.font = ARIAL
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        r += 1

    # ── Полные колонки: 9 исходных + все рабочие ──
    src_wb = openpyxl.load_workbook(src_path, read_only=True, data_only=True)
    src_rows = list(src_wb[src_wb.sheetnames[0]].iter_rows(values_only=True))
    ws = wb.create_sheet("Полные_колонки")
    headers = list(src_rows[0]) + [
        "Город поиска", "Найденный сайт", "Источник сайта", "Грейд",
        "Чем подтверждён", "Статус обхода", "Уровень каскада", "Суждение А",
        "Основание А", "Сеть УК", "Суждение Б", "Совпадений",
        "Совпавшие позиции", "Специальности на сайте", "Попыток поиска"]
    _sheet(ws, headers, [12] * len(headers))
    r = 2
    imported = {row[0] for row in db.execute("SELECT inn FROM t40_companies")}
    for src in src_rows[1:]:
        if not src[1]:
            continue
        inn = str(src[4]).strip()
        if inn not in imported:
            continue
        row = db.execute(
            "SELECT city, found_site, site_source, grade, grade_evidence, "
            "COALESCE(fetch_status, search_status, 'сайт не найден'), "
            "fetch_level, med_judgment, med_basis, mgmt_network, "
            "profile_judgment, profile_matches_n, profile_matches, "
            "site_specialties, search_attempts FROM t40_companies WHERE inn=?",
            (inn,)).fetchone() or [None] * 15
        for c, v in enumerate(list(src) + list(row), 1):
            cell = ws.cell(r, c, v if v is not None else "")
            cell.font = ARIAL
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        r += 1

    out = f"output/Тест40_{day}.xlsx"
    wb.save(out)
    return out


if __name__ == "__main__":
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import":
        path = sys.argv[2]
        n = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_ROWS
        print("импорт:", import_t40(path, con, n))
    elif cmd == "rzn":
        from src.rzn_licenses import batch
        b = float(sys.argv[2]) if len(sys.argv) > 2 else 3600
        print("реестр лицензий:", batch(con, "t40_companies", b))
    elif cmd == "sites":
        b = float(sys.argv[2]) if len(sys.argv) > 2 else 1800
        print("сайты (гибко):", check_sites(con, b))
    elif cmd == "search":
        print("поиск:", run_search(con))
    elif cmd == "judge":
        b = float(sys.argv[2]) if len(sys.argv) > 2 else 2400
        print("обход и суждения:", crawl_judge(con, b))
    elif cmd == "export":
        print("выгрузка:", export_t40(con, sys.argv[2]))
    else:
        print("команды: import <файл> [N] | rzn [сек] | sites [сек] | "
              "search | judge [сек] | export <файл>")
