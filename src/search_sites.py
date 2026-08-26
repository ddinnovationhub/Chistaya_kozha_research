"""Поисковая достройка сайтов (решение заказчика, 2026-08-26, взамен
транслитерации, выведенной из флоу).

Логика:
1. Запрос «{название} {город}» — город ОБЯЗАТЕЛЕН в запросе.
2. Топ-10 выдачи; агрегаторы/справочники пропускаются без траты попытки;
   максимум 5 неагрегаторных результатов на компанию.
   Обоснование лимита: официальный сайт по брендовому запросу с городом
   стоит в топ-3 почти всегда; глубже пятого — справочники и соцсети,
   перебор их плодит ложные привязки (дефект 1).
3. Каждый кандидат — через ТРОЙНУЮ ПРОВЕРКУ (site_finder.triple_check):
   ИНН на сайте (сильнейший) → адрес организации в городе (контакт-блок/
   подвал; >3 городов = федеральная сеть → только ИНН). Название — не признак.
4. Лимит исчерпан → статус «сайт не найден». Пустая ячейка честнее неверной.

Запускается из GitHub Actions (ключи в Secrets):
    python -m src.search_sites [бюджет_сек]
Правовой режим: данные выдачи не сохраняются (лицензия Яндекса) — только
обнаружение URL; подтверждение берётся с самого сайта.
"""

import sqlite3
import sys
import time

from src.api_client import handle_api_response, yandex_search_raw
from src.discovery import parse_yandex_xml
from src.dedup import normalize_domain
from src.discovery import is_aggregator_domain
from src.site_finder import triple_check

SEARCH_COST_RUB = 0.52
MAX_CANDIDATES = 5


def find_site_for_company(inn: str, name: str, city: str) -> tuple[str | None, str, int]:
    """(домен|None, грейд/причина, потрачено запросов)."""
    import base64
    resp = yandex_search_raw(f"{name} {city}", n=10)
    if handle_api_response(resp, "Яндекс Search API") is None:
        return None, "поиск недоступен (временная ошибка)", 1
    results = parse_yandex_xml(
        base64.b64decode(resp.json()["rawData"]).decode("utf-8"))
    tried = 0
    for r in results:
        dom = normalize_domain(r.get("url") or "")
        if not dom or is_aggregator_domain(dom):
            continue   # агрегатор не тратит попытку
        tried += 1
        if tried > MAX_CANDIDATES:
            break
        chk = triple_check(dom, inn, city)
        if chk["verdict"] == "ИНН":
            return dom, "подтверждён ИНН", 1
        if chk["verdict"] == "адрес":
            return dom, "подтверждён адресом в городе", 1
    return None, f"сайт не найден (проверено {min(tried, MAX_CANDIDATES)} кандидатов)", 1


def run_search(db: sqlite3.Connection, budget_sec: float = 18000,
               budget_rub: float = 2000) -> dict:
    """Достройка всем без сайта. Идемпотентно: site_source
    'поиск: не найден' помечает исчерпанных."""
    rows = list(db.execute(
        "SELECT inn, name, city FROM companies WHERE site IS NULL "
        "AND (site_source IS NULL OR site_source NOT LIKE 'поиск:%')"))
    t0 = time.time()
    stats = {"companies": len(rows), "done": 0, "found_inn": 0,
             "found_addr": 0, "not_found": 0, "spent_rub": 0.0}
    for inn, name, city in rows:
        if time.time() - t0 > budget_sec:
            break
        if stats["spent_rub"] >= budget_rub:
            print(f"⛔ бюджет поиска {budget_rub} ₽ исчерпан — остановка")
            break
        try:
            dom, grade, spent = find_site_for_company(inn, name, city)
        except Exception as exc:  # noqa: BLE001 — компания не роняет прогон
            print(f"  {inn}: ошибка {type(exc).__name__}")
            continue
        stats["spent_rub"] += spent * SEARCH_COST_RUB
        if dom:
            key = "found_inn" if "ИНН" in grade else "found_addr"
            stats[key] += 1
            db.execute("UPDATE companies SET site=?, site_status=?, "
                       "site_source='поиск: найден', fetch_status=NULL "
                       "WHERE inn=?", (dom, grade, inn))
        else:
            stats["not_found"] += 1
            db.execute("UPDATE companies SET site_source='поиск: не найден', "
                       "fetch_status='сайт не найден' WHERE inn=?", (inn,))
        stats["done"] += 1
        db.commit()
        time.sleep(1)
    stats["left"] = len(rows) - stats["done"]
    return stats


if __name__ == "__main__":
    import os
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 18000
    rub = float(os.environ.get("SEARCH_BUDGET_RUB") or 2000)
    st = run_search(con, budget_sec=budget, budget_rub=rub)
    print(f"поисковая достройка: обработано {st['done']}/{st['companies']}, "
          f"найдено по ИНН {st['found_inn']}, по адресу {st['found_addr']}, "
          f"не найдено {st['not_found']}, потрачено {st['spent_rub']:.0f} ₽, "
          f"осталось {st['left']}")
