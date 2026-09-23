"""ГИР БО (bo.nalog.gov.ru) — официальная бухгалтерская отчётность по ИНН.

Канал пробит 2026-09-23: открытый JSON-API без ключа.
  1) GET /advanced-search/organizations/search?query={ИНН}&page=0
     → карточка: id, ОГРН, адрес, ОКВЭД, statusCode, statusDate.
  2) GET /nbo/organizations/{id}/bfo/
     → список отчётных периодов; в typeCorrections[].correction лежат
       balance (форма 0710001) и financialResult (0710002).

Из отчётности берутся ТОЛЬКО фактические строки форм, без достраивания:
  1600 активы всего (current/previous) · 1300 капитал · 2110 выручка ·
  2200 прибыль от продаж · 2300 прибыль до налогообложения ·
  2330 проценты к уплате · 2400 чистая прибыль.
Суммы форм — в тысячах рублей; в БД пишутся КАК ЕСТЬ (тыс. ₽).

У части организаций (МСП) сдана упрощённая форма: строк 2200/2300/2330
в ней нет. Тогда EBIT не вычисляется — статус «Не найдено», а не оценка.

Правовой режим: robots.txt источника не запрещает; пауза PAUSE_SEC между
запросами; данные официальные, публикуются по ст. 18 402-ФЗ.
Каждая запись хранит source_url и checked_at (контракт записи, CLAUDE.md).
"""

import datetime
import json
import sqlite3
import sys
import time

import httpx

BASE = "https://bo.nalog.gov.ru"
SEARCH = BASE + "/advanced-search/organizations/search"
BFO = BASE + "/nbo/organizations/{oid}/bfo/"
PAUSE_SEC = 1.0

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

LINES = ("1300", "1600", "2110", "2120", "2200", "2300", "2330", "2400",
         "2210", "2220", "2340", "2350", "2410")


def ensure_tables(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS girbo_org (
        inn TEXT PRIMARY KEY, org_id INTEGER, short_name TEXT, ogrn TEXT,
        region TEXT, city TEXT, okved2 TEXT, status_code TEXT, status_date TEXT,
        source_url TEXT, checked_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS girbo_fin (
        inn TEXT, period TEXT, simplified INTEGER,
        line1300 REAL, line1600 REAL, line1600_prev REAL,
        line2110 REAL, line2120 REAL, line2200 REAL, line2300 REAL,
        line2330 REAL, line2400 REAL, line2210 REAL, line2220 REAL,
        line2340 REAL, line2350 REAL, line2410 REAL,
        source_url TEXT, checked_at TEXT,
        PRIMARY KEY (inn, period))""")
    db.execute("""CREATE TABLE IF NOT EXISTS girbo_checked (
        inn TEXT PRIMARY KEY, status TEXT, periods_n INTEGER, checked_at TEXT)""")
    db.commit()


def find_org(inn: str, client: httpx.Client) -> dict | None:
    r = client.get(SEARCH, params={"query": inn, "page": 0}, headers=_HEADERS, timeout=40)
    if r.status_code != 200:
        return None
    content = (r.json() or {}).get("content") or []
    for row in content:
        if "".join(ch for ch in str(row.get("inn", "")) if ch.isdigit()) == inn:
            return row
    return None


def fetch_bfo(oid: int, client: httpx.Client) -> list | None:
    r = client.get(BFO.format(oid=oid), headers=_HEADERS, timeout=60)
    if r.status_code != 200:
        return None
    return r.json()


def _corr(period_row: dict) -> dict | None:
    """Актуальная корректировка периода (тип 12 — годовая БО)."""
    best = None
    for tc in period_row.get("typeCorrections") or []:
        c = tc.get("correction") or {}
        if c.get("balance") or c.get("financialResult"):
            if tc.get("type") == 12:
                return c
            best = best or c
    return best


def save(db: sqlite3.Connection, inn: str, org: dict, periods: list):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    db.execute("INSERT OR REPLACE INTO girbo_org VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (inn, org.get("id"),
                (org.get("shortName") or "").strip(),
                org.get("ogrn"), org.get("region"), org.get("city"),
                org.get("okved2"), org.get("statusCode"), org.get("statusDate"),
                f"{SEARCH}?query={inn}", now))
    for p in periods or []:
        c = _corr(p)
        if not c:
            continue
        b = c.get("balance") or {}
        f = c.get("financialResult") or {}
        vals = {k: (b.get("current" + k) if k.startswith("1") else f.get("current" + k))
                for k in LINES}
        db.execute("INSERT OR REPLACE INTO girbo_fin VALUES "
                   "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (inn, str(p.get("period")),
                    1 if len(f) <= 20 else 0,
                    vals["1300"], vals["1600"], b.get("previous1600"),
                    vals["2110"], vals["2120"], vals["2200"], vals["2300"],
                    vals["2330"], vals["2400"], vals["2210"], vals["2220"],
                    vals["2340"], vals["2350"], vals["2410"],
                    BFO.format(oid=org.get("id")), now))
    db.commit()


def run(inns: list[str], db_path: str = "data/osint.db", force: bool = False):
    db = sqlite3.connect(db_path)
    ensure_tables(db)
    done = {r[0] for r in db.execute("SELECT inn FROM girbo_checked WHERE status='ok'")}
    todo = [i for i in inns if force or i not in done]
    print(f"ГИР БО: к сбору {len(todo)} ИНН (уже собрано {len(inns) - len(todo)})", flush=True)
    now = lambda: datetime.datetime.now().isoformat(timespec="seconds")
    with httpx.Client(follow_redirects=True) as client:
        for n, inn in enumerate(todo, 1):
            try:
                org = find_org(inn, client)
                if not org:
                    db.execute("INSERT OR REPLACE INTO girbo_checked VALUES (?,?,?,?)",
                               (inn, "не найден в ГИР БО", 0, now()))
                    db.commit()
                else:
                    time.sleep(PAUSE_SEC)
                    periods = fetch_bfo(org["id"], client) or []
                    save(db, inn, org, periods)
                    db.execute("INSERT OR REPLACE INTO girbo_checked VALUES (?,?,?,?)",
                               (inn, "ok", len(periods), now()))
                    db.commit()
            except Exception as e:                       # noqa: BLE001
                db.execute("INSERT OR REPLACE INTO girbo_checked VALUES (?,?,?,?)",
                           (inn, f"ошибка: {type(e).__name__}", 0, now()))
                db.commit()
            if n % 20 == 0:
                print(f"  {n}/{len(todo)}", flush=True)
            time.sleep(PAUSE_SEC)
    ok = db.execute("SELECT count(*) FROM girbo_checked WHERE status='ok'").fetchone()[0]
    print(f"ГИР БО: готово, ok={ok}", flush=True)


if __name__ == "__main__":
    src = sys.argv[1]
    run([l.strip() for l in open(src, encoding="utf-8") if l.strip()])
