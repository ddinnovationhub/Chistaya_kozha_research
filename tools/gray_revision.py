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
    import concurrent.futures as cf
    stats = collections.Counter({"проверено": 0, "отсеяно (немед)": 0,
                                 "ошибок": 0})

    def work(item):
        """Вердикт по одной строке. Домены разные, поэтому параллельность
        не нарушает лимит «≤1 запрос/3 с на домен» (sources.yaml)."""
        inn, name, status, cand_log = item
        try:
            dom = status.split("проверки: ", 1)[1].split(" — ", 1)[0].strip()
        except IndexError:
            return inn, cand_log, None, "битый маркер"
        try:
            # без Playwright-эскалации: локально нет JINA_API_KEY и
            # headless-браузер даёт 3 часа на 170 строк. Нечитаемые
            # строки возвращаются в очередь поиска — их добьёт прогон
            # в Actions, где ключ Jina есть
            return inn, cand_log, gray_zone_verdict(dom, name,
                                                    escalate=False), dom
        except Exception as e:  # noqa: BLE001 — строка не валит ревизию
            return inn, cand_log, None, f"ошибка {type(e).__name__} на {dom}"

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = ex.map(work, rows)

    for inn, cand_log, verdict, dom in results:
        if dom.startswith(("ошибка", "битый")):
            print(f"⚠ {inn}: {dom} — оставлен как есть")
            stats["ошибок"] += 1
            continue
        if verdict is None:
            stats["отсеяно (немед)"] += 1
            db.execute(
                "UPDATE t40_companies SET search_status='сайт не найден', "
                "search_candidates=? WHERE inn=?",
                (f"{cand_log or ''} | ревизия 2026-08-31: отсеян немедицинский "
                 f"кандидат карт {dom}"[:400], inn))
        elif verdict[0] == "сайт не прочитан":
            # не человеку, а обратно в конвейер: следующий прогон Actions
            # прочитает сайт рендером и вынесет вердикт сам
            stats["в очередь поиска (нужен рендер)"] += 1
            db.execute(
                "UPDATE t40_companies SET search_status=NULL, "
                "search_candidates=? WHERE inn=?",
                (f"{cand_log or ''} | ревизия 2026-08-31: {dom} не прочитан "
                 f"без рендера — на переобход"[:400], inn))
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
    db.close()
    return stats


if __name__ == "__main__":
    print("ревизия серой зоны:",
          main(int(sys.argv[1]) if len(sys.argv) > 1 else 0))
