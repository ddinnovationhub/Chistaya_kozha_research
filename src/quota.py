"""Суточные счётчики запросов к внешним API (заказчик, 2026-08-28:
триал Яндекс Геопоиска — не более 1000 запросов в сутки).

Счётчик живёт в osint.db → переживает перезапуски и коммитится вместе
с базой. Расход учитывается ДО запроса (переоценка безопаснее недооценки);
достижение лимита не роняет прогон — канал честно пропускается с
сообщением, строка остаётся на следующий день.
"""

import datetime
import sqlite3

DB_PATH = "data/osint.db"
LIMITS = {"yandex_geosearch": 1000}


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA busy_timeout=15000")
    db.execute("""CREATE TABLE IF NOT EXISTS api_quota (
        service TEXT, day TEXT, used INTEGER,
        PRIMARY KEY (service, day))""")
    return db


def spend(service: str, n: int = 1) -> bool:
    """True — расход учтён, можно слать запрос; False — суточный лимит
    исчерпан, запрос НЕ отправлять."""
    limit = LIMITS.get(service)
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
        return (row[0] if row else 0), LIMITS.get(service)
    finally:
        db.close()
