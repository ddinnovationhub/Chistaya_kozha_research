"""Ревизия накопленной серой зоны по новому фильтру (заказчик, 2026-08-31).

Строки с маркером «Требует ручной проверки» прогоняются через
test40.gray_zone_verdict: немедицинские кандидаты карт становятся честным
«сайт не найден», медицинские получают приоритет. Идемпотентно и
возобновляемо — уже переработанные строки (новый формат «Ручная проверка
[...]») пропускаются, поэтому скрипт можно запускать порциями.

Запуск: python -m tools.gray_revision [сколько_строк]
"""

import sqlite3
import sys
import time

from src.test40 import gray_zone_verdict


def main(limit: int = 0) -> dict:
    db = sqlite3.connect("data/osint.db")
    db.execute("PRAGMA busy_timeout=15000")
    rows = db.execute(
        "SELECT inn, name, search_status, search_candidates FROM t40_companies "
        "WHERE search_status LIKE 'Требует ручной проверки%' ORDER BY row_no"
    ).fetchall()
    if limit:
        rows = rows[:limit]
    import collections
    stats = collections.Counter({"проверено": 0, "отсеяно (немед)": 0,
                                 "ошибок": 0})
    for inn, name, status, cand_log in rows:
        try:
            dom = status.split("проверки: ", 1)[1].split(" — ", 1)[0].strip()
        except IndexError:
            stats["ошибок"] += 1
            continue
        try:
            verdict = gray_zone_verdict(dom, name)
        except Exception as e:  # noqa: BLE001 — строка не валит ревизию
            print(f"⚠ {dom}: {type(e).__name__} — оставлен как есть")
            stats["ошибок"] += 1
            continue
        if verdict is None:
            stats["отсеяно (немед)"] += 1
            db.execute(
                "UPDATE t40_companies SET search_status='сайт не найден', "
                "search_candidates=? WHERE inn=?",
                (f"{cand_log or ''} | ревизия 2026-08-31: отсеян немедицинский "
                 f"кандидат карт {dom}"[:400], inn))
        else:
            prio, ev = verdict
            stats[prio] += 1
            db.execute(
                "UPDATE t40_companies SET search_status=? WHERE inn=?",
                (f"Ручная проверка [{prio}]: {dom} — {ev}"[:250], inn))
        stats["проверено"] += 1
        db.commit()
        if stats["проверено"] % 20 == 0:
            print(f"  … {stats['проверено']}/{len(rows)}", flush=True)
        time.sleep(0.5)
    db.close()
    return stats


if __name__ == "__main__":
    print("ревизия серой зоны:",
          main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
