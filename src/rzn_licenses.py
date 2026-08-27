"""Единый реестр лицензий Росздравнадзора — МАССОВАЯ выгрузка по ИНН
(заказчик, 2026-08-27: «основная проблема в массовой выгрузке… найди иной
подход, либо придумай, как вытащить автоматически»).

Канал пробит 2026-08-27: страница /services/licenses рисует таблицу через
DataTables, данные отдаёт JSON-эндпоинт POST /ajax/services/licenses
(нужны кука сессии с GET страницы и X-Requested-With). Ответ содержит ВСЁ,
ради чего планировался парсинг PDF-выписок:
- строка = лицензия (несколько лицензий ИНН → несколько строк);
- col1/col9 номер (Л041… — мед), col2 дата, col3 лицензиат, col4 орган,
  col6 ОГРН, col7 ИНН, col12 срок, col16 аннулирование, col17 прекращение;
- objects[] = ПРИЛОЖЕНИЯ: каждый адрес места деятельности с полем
  activity — дословный перечень работ (услуг), включая специальности
  («…в амбулаторных условиях по: дерматовенерологии, косметологии»).

Правовой режим: robots.txt = «Allow: /»; пауза ≥3 с между запросами
(≤1 запрос/3с, CLAUDE.md); CAPTCHA нет. ~1 запрос на ИНН: 2304 ИНН ≈ 2.5 ч.

Каждая запись — факт реестра с source_id (URL + дата). Отрицательный
результат («лицензий не найдено») тоже записывается.
"""

import datetime
import json
import re
import sqlite3
import sys
import time
import zlib

import httpx

RZN_URL = "https://roszdravnadzor.gov.ru/services/licenses"
RZN_AJAX = "https://roszdravnadzor.gov.ru/ajax/services/licenses"
PAUSE_SEC = 3.0

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": RZN_URL,
}

# Специальности из перечня работ приложения — дословные корни для
# извлечения; список расширяется, НЕ выдумывается (только то, что реально
# встречается в формулировках 852н/866н)
SPECIALTY_ROOTS = [
    "дерматовенерологи", "косметологи", "онкологи", "трихологи",
    "стоматологи", "гинекологи", "акушерств", "урологи", "кардиологи",
    "неврологи", "офтальмологи", "оториноларингологи", "педиатри",
    "терапи", "хирурги", "травматологии и ортопедии", "эндокринологи",
    "гастроэнтерологи", "аллергологии и иммунологии", "психиатри",
    "психотерапи", "физиотерапи", "рентгенологи", "ультразвуковой диагностике",
    "функциональной диагностике", "клинической лабораторной диагностике",
    "анестезиологии и реаниматологии", "сестринскому делу", "медицинскому массажу",
    "мануальной терапии", "рефлексотерапи", "пластической хирургии",
    "гистологи", "патологической анатомии", "флебологи", "профпатологи",
    "эпидемиологи", "дезинфектологи", "медицинским осмотрам", "экспертизе",
]

_MED_NUM_RE = re.compile(r"^Л041|^ЛО-")


def make_client() -> httpx.Client:
    c = httpx.Client(headers=_HEADERS, timeout=40, follow_redirects=True)
    c.get(RZN_URL, headers={"Accept": "text/html"})   # кука сессии
    return c


def _lbl(row: dict, col: str) -> str | None:
    v = (row.get(col) or {})
    s = (v.get("label") or "").strip()
    return s or None


def fetch_licenses(inn: str, client: httpx.Client) -> list[dict] | None:
    """Все лицензии ИНН из Единого реестра. None = запрос не удался
    (НЕ то же самое, что пустой список — «лицензий не найдено»)."""
    try:
        r = client.post(RZN_AJAX, data={"draw": "1", "start": "0",
                                        "length": "100", "q_no": inn})
        if r.status_code != 200:
            return None
        data = r.json().get("data") or []
    except Exception:  # noqa: BLE001
        return None
    out = []
    for row in data:
        # такт 3: q_no ищет и по номеру лицензии, и по названию — чужая
        # строка с совпавшими цифрами отбрасывается сверкой ИНН ответа
        row_inn = ((row.get("col7") or {}).get("label") or "").strip()
        if row_inn and row_inn != inn:
            continue
        objs = []
        for o in row.get("objects") or []:
            objs.append({
                "address": (o.get("address_fact") or "").strip(),
                "city": (o.get("city") or "").strip(),
                "region": (o.get("region") or "").strip(),
                "activity": (o.get("activity") or "").strip(),
            })
        num = _lbl(row, "col1") or _lbl(row, "col9")
        out.append({
            "number": num,
            "date": _lbl(row, "col2"),
            "licensee": _lbl(row, "col3"),
            "authority": _lbl(row, "col4"),
            "ogrn": _lbl(row, "col6"),
            "inn": _lbl(row, "col7") or inn,
            "valid_to": _lbl(row, "col12"),
            "annulled": _lbl(row, "col16"),
            "terminated": _lbl(row, "col17"),
            "is_med": bool(num and _MED_NUM_RE.match(num)),
            "objects": objs,
        })
    return out


def specialties_from_activity(activity_texts: list[str]) -> list[str]:
    """Дословно встреченные специальности из перечней работ приложений."""
    joined = " ".join(activity_texts).lower()
    return [s for s in SPECIALTY_ROOTS if s in joined]


def ensure_tables(db: sqlite3.Connection):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS rzn_licenses (
        inn TEXT, number TEXT, date TEXT, licensee TEXT, authority TEXT,
        ogrn TEXT, valid_to TEXT, annulled TEXT, terminated TEXT,
        is_med INTEGER, objects_n INTEGER, specialties TEXT,
        raw_gz BLOB, source_url TEXT, checked_at TEXT,
        PRIMARY KEY (inn, number));
    CREATE TABLE IF NOT EXISTS rzn_checked (
        inn TEXT PRIMARY KEY, status TEXT, licenses_n INTEGER,
        med_licenses_n INTEGER, checked_at TEXT);
    """)
    db.commit()


def save_licenses(db: sqlite3.Connection, inn: str,
                  lics: list[dict] | None) -> dict:
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if lics is None:
        db.execute("INSERT OR REPLACE INTO rzn_checked VALUES (?,?,?,?,?)",
                   (inn, "запрос не удался", None, None, now))
        db.commit()
        return {"status": "запрос не удался"}
    med_n = 0
    for lic in lics:
        acts = [o["activity"] for o in lic["objects"] if o["activity"]]
        specs = specialties_from_activity(acts) if lic["is_med"] else []
        if lic["is_med"]:
            med_n += 1
        db.execute(
            "INSERT OR REPLACE INTO rzn_licenses VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (inn, lic["number"], lic["date"], lic["licensee"], lic["authority"],
             lic["ogrn"], lic["valid_to"], lic["annulled"], lic["terminated"],
             int(lic["is_med"]), len(lic["objects"]), ", ".join(specs),
             zlib.compress(json.dumps(lic, ensure_ascii=False).encode("utf-8")),
             RZN_URL, now))
    db.execute("INSERT OR REPLACE INTO rzn_checked VALUES (?,?,?,?,?)",
               (inn, "проверен", len(lics), med_n, now))
    db.commit()
    return {"status": "проверен", "licenses": len(lics), "med": med_n}


def batch(db: sqlite3.Connection, table: str = "pilot_companies",
          budget_sec: float = 3600) -> dict:
    """Массовый прогон по ИНН таблицы (идемпотентно: пропускает проверенные;
    «запрос не удался» перепроверяется). Последовательно, пауза 3 с."""
    ensure_tables(db)
    inns = [r[0] for r in db.execute(
        f"SELECT c.inn FROM {table} c LEFT JOIN rzn_checked k ON k.inn=c.inn "
        f"WHERE k.inn IS NULL OR k.status='запрос не удался'")]
    client = make_client()
    t0 = time.time()
    stats = {"done": 0, "с лицензиями": 0, "с мед-лицензией": 0,
             "без лицензий": 0, "ошибок": 0}
    for i, inn in enumerate(inns):
        if time.time() - t0 > budget_sec:
            break
        lics = fetch_licenses(inn, client)
        res = save_licenses(db, inn, lics)
        stats["done"] += 1
        if lics is None:
            stats["ошибок"] += 1
            if stats["ошибок"] > 5 and stats["ошибок"] > stats["done"] * 0.2:
                stats["стоп"] = ">20% ошибок — обязательная остановка (CLAUDE.md)"
                break
        elif res.get("med"):
            stats["с мед-лицензией"] += 1
            stats["с лицензиями"] += 1
        elif res.get("licenses"):
            stats["с лицензиями"] += 1
        else:
            stats["без лицензий"] += 1
        time.sleep(PAUSE_SEC)
    stats["осталось"] = len(inns) - stats["done"]
    return stats


if __name__ == "__main__":
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "one":
        inn = sys.argv[2]
        ensure_tables(con)
        lics = fetch_licenses(inn, make_client())
        print(json.dumps(lics, ensure_ascii=False, indent=1)[:4000])
        print(save_licenses(con, inn, lics))
    elif cmd == "batch":
        table = sys.argv[2] if len(sys.argv) > 2 else "pilot_companies"
        b = float(sys.argv[3]) if len(sys.argv) > 3 else 3600
        print("реестр лицензий:", batch(con, table, b))
    else:
        print("команды: one <ИНН> | batch [таблица] [сек]")
