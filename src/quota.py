"""Суточные счётчики запросов к внешним API (заказчик, 2026-08-28:
триал Яндекс Геопоиска — не более 1000 запросов в сутки).

Счётчик живёт в osint.db → переживает перезапуски и коммитится вместе
с базой. Расход учитывается ДО запроса (переоценка безопаснее недооценки);
достижение лимита не роняет прогон — канал честно пропускается с
сообщением, строка остаётся на следующий день.
"""

import datetime
import os
import sqlite3

DB_PATH = "data/osint.db"
LIMITS = {"yandex_geosearch": 1000,
          # демо-ключ 2ГИС: 1000 запросов/день на продукт (заказчик, 2026-08-27)
          "dgis_places": 1000,
          # Keenable: 100 000/мес по ключу; суточный потолок 3000 ≈ 90 000/мес
          # с запасом (заказчик, 2026-09-02)
          "keenable": 3000}


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("""CREATE TABLE IF NOT EXISTS api_quota (
        service TEXT, day TEXT, used INTEGER,
        PRIMARY KEY (service, day))""")
    return db


def limit_for(service: str) -> int | None:
    """Суточный лимит сервиса с поправкой на число параллельных шардов.

    ПАРАЛЛЕЛЬНЫЕ ШАРДЫ (2026-09-04): квота Геопоиска и 2ГИС — суточная НА
    КЛЮЧ, а счётчик живёт в базе, и у каждого шарда база своя. Без деления
    пять шардов независимо израсходовали бы по 1000 запросов при реальном
    лимите 1000 на всех. QUOTA_SHARE (число шардов волны) выставляет
    воркфлоу; без него поведение прежнее."""
    limit = LIMITS.get(service)
    if limit is None:
        return None
    try:
        share = int(os.environ.get("QUOTA_SHARE") or 1)
    except ValueError:
        share = 1
    return max(1, limit // share) if share > 1 else limit


def spend(service: str, n: int = 1) -> bool:
    """True — расход учтён, можно слать запрос; False — суточный лимит
    исчерпан, запрос НЕ отправлять."""
    limit = limit_for(service)
    day = datetime.date.today().isoformat()
    db = _db()
    try:
        db.execute("INSERT OR IGNORE INTO api_quota VALUES (?,?,0)",
                   (service, day))
        used = db.execute("SELECT used FROM api_quota WHERE service=? AND day=?",
                          (service, day)).fetchone()[0]
        if limit is not None and used + n > limit:
            print(f"⛔ {service}: суточный лимит {limit} исчерпан "
                  f"(израсходовано {used}) — канал пропущен до завтра")
            return False
        db.execute("UPDATE api_quota SET used=used+? WHERE service=? AND day=?",
                   (n, service, day))
        db.commit()
        return True
    finally:
        db.close()


def status(service: str) -> tuple[int, int | None]:
    """(израсходовано сегодня, лимит)."""
    day = datetime.date.today().isoformat()
    db = _db()
    try:
        row = db.execute("SELECT used FROM api_quota WHERE service=? AND day=?",
                         (service, day)).fetchone()
        return (row[0] if row else 0), limit_for(service)
    finally:
        db.close()
