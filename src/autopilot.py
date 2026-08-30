"""Автопилот конвейера test-40 (заказчик, 2026-08-30): «одно нажатие —
система сама доводит прогон до цели, не превышая ни один лимит».

Что автопилот ДЕЛАЕТ: считает остаток работы, решает «продолжать/стоп»,
хранит снапшот прогресса между прогонами; воркфлоу по решению «continue»
перезапускает сам себя (workflow_dispatch), а ежедневный крон продолжает
на следующий день, когда суточные квоты обнулились.

Что автопилот НЕ ДЕЛАЕТ: не обходит предохранители ТЗ. Любой системный
стоп (источник >20% ошибок, бюджет 5000 ₽, ошибка авторизации) валит свой
этап → воркфлоу красный → шаг «Продолжение» пропускается по условию
успеха. Автопилот продолжает только ПОЛНОСТЬЮ УСПЕШНЫЕ прогоны.

Гварды от бесконечной цепочки:
- потолок перезапусков за цепочку (CHAIN_CAP);
- «нулевой прогресс»: прогон, не сдвинувший ни один счётчик, цепочку
  не продолжает (типовая причина — суточная квота; крон дожмёт завтра);
- повторные обходы упавших строк ограничены RETRY_CAP — после него строка
  остаётся с честным статусом ошибки и уходит в ручную проверку.
"""

import datetime
import json
import sqlite3
import sys

CHAIN_CAP = 8       # перезапусков подряд за одну цепочку (день)
RETRY_CAP = 3       # повторов обхода упавшей строки
BUDGET_STOP_SHARE = 0.98   # выше — продолжение не назначается

RETRY_STATUS = "ошибка обхода — на повтор"
FINAL_RETRY_STATUS = "ошибка обхода — исчерпаны повторы, ручная проверка"


def ensure_tables(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS autopilot_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        target_rows INTEGER, chain INTEGER, enabled INTEGER,
        snapshot TEXT, updated_at TEXT)""")
    cols = {r[1] for r in db.execute("PRAGMA table_info(t40_companies)")}
    if "fetch_retries" not in cols:
        db.execute("ALTER TABLE t40_companies ADD COLUMN fetch_retries INTEGER")
    db.commit()


def snapshot(db: sqlite3.Connection) -> dict:
    """Счётчики терминальных состояний — метрика прогресса между прогонами."""
    def q(sql):
        try:
            return db.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            return 0
    return {
        "imported": q("SELECT COUNT(*) FROM t40_companies"),
        "sites": q("SELECT COUNT(*) FROM t40_companies WHERE found_site IS NOT NULL"),
        "searched": q("SELECT COUNT(*) FROM t40_companies WHERE search_status IS NOT NULL"),
        "spark_checked": q("SELECT COUNT(*) FROM t40_companies WHERE site_source IS NOT NULL"),
        "fetched": q("SELECT COUNT(*) FROM t40_companies WHERE fetch_status IS NOT NULL"),
        "mapchecked": q("SELECT COUNT(*) FROM t40_companies WHERE map_check IS NOT NULL"),
        "judgments": q("SELECT COUNT(*) FROM llm_judgments"),
    }


def remaining(db: sqlite3.Connection, target_rows: int) -> dict:
    def q(sql, *args):
        return db.execute(sql, args).fetchone()[0]
    max_row = q("SELECT COALESCE(MAX(row_no), 0) FROM t40_companies")
    return {
        "импорт": max(0, target_rows - max_row),
        "очередь поиска": q(
            "SELECT COUNT(*) FROM t40_companies WHERE row_no<=? "
            "AND found_site IS NULL AND search_status IS NULL", target_rows),
        "очередь обхода": q(
            "SELECT COUNT(*) FROM t40_companies WHERE row_no<=? "
            "AND found_site IS NOT NULL AND fetch_status IS NULL", target_rows),
        "на повтор обхода": q(
            "SELECT COUNT(*) FROM t40_companies WHERE row_no<=? "
            "AND fetch_status=?", target_rows, RETRY_STATUS),
        "очередь даблчека": q(
            "SELECT COUNT(*) FROM t40_companies WHERE row_no<=? "
            "AND found_site IS NOT NULL AND map_check IS NULL", target_rows),
        "без единого судьи": q(
            "SELECT COUNT(*) FROM t40_companies c WHERE row_no<=? "
            "AND passport IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM llm_judgments j WHERE j.inn=c.inn)", target_rows),
    }


def _budget_ok() -> tuple[bool, str]:
    from src.budget import BudgetTracker
    b = BudgetTracker()
    if b.spent >= b.ceiling_rub * BUDGET_STOP_SHARE:
        return False, f"бюджет {b.spent:.0f} ₽ из {b.ceiling_rub:.0f} ₽"
    return True, f"бюджет {b.spent:.0f} ₽ из {b.ceiling_rub:.0f} ₽"


def prepare(db: sqlite3.Connection) -> dict:
    """Начало прогона: строки «ошибка обхода — на повтор» возвращаются
    в очередь (не более RETRY_CAP раз); исчерпавшие повторы получают
    честный финальный статус — в ручную проверку, не в вечный цикл."""
    ensure_tables(db)
    exhausted = db.execute(
        "UPDATE t40_companies SET fetch_status=? "
        "WHERE fetch_status=? AND COALESCE(fetch_retries,0) >= ?",
        (FINAL_RETRY_STATUS, RETRY_STATUS, RETRY_CAP)).rowcount
    reset = db.execute(
        "UPDATE t40_companies SET fetch_status=NULL, "
        "fetch_retries=COALESCE(fetch_retries,0)+1 WHERE fetch_status=?",
        (RETRY_STATUS,)).rowcount
    db.commit()
    return {"возвращено в очередь": reset, "исчерпали повторы": exhausted}


def decide(db: sqlite3.Connection, target_rows: int, chain: int) -> str:
    """Конец прогона: continue | stop (последняя строка stdout — решение)."""
    ensure_tables(db)
    prev_row = db.execute("SELECT snapshot FROM autopilot_state WHERE id=1").fetchone()
    prev = json.loads(prev_row[0]) if prev_row and prev_row[0] else None
    cur = snapshot(db)
    rem = remaining(db, target_rows)
    total_rem = sum(rem.values())
    budget_fine, budget_line = _budget_ok()

    verdict, reason = "continue", ""
    if total_rem == 0:
        verdict, reason = "stop", "цель достигнута — вся работа завершена"
    elif not budget_fine:
        verdict, reason = "stop", f"потолок бюджета: {budget_line} — доклад заказчику"
    elif chain >= CHAIN_CAP:
        verdict, reason = ("stop", f"потолок цепочки {CHAIN_CAP} перезапусков — "
                                   "крон продолжит завтра")
    elif chain > 0 and prev is not None and all(
            cur[k] <= prev.get(k, 0) for k in cur):
        verdict, reason = ("stop", "прогон не сдвинул ни один счётчик "
                                   "(вероятно, суточные квоты) — крон продолжит завтра")

    enabled = 1 if total_rem > 0 else 0
    db.execute("INSERT OR REPLACE INTO autopilot_state VALUES (1,?,?,?,?,?)",
               (target_rows, chain, enabled, json.dumps(cur, ensure_ascii=False),
                datetime.datetime.now().isoformat(timespec="seconds")))
    db.commit()

    print(f"остаток работы: {rem} · {budget_line} · цепочка {chain}/{CHAIN_CAP}",
          file=sys.stderr)
    print(f"решение: {verdict} — {reason or 'работа осталась, лимиты позволяют'}",
          file=sys.stderr)
    print(verdict)
    return verdict


def cron(db: sqlite3.Connection) -> str:
    """Утренняя проверка: печатает target_rows для перезапуска или stop."""
    ensure_tables(db)
    row = db.execute("SELECT target_rows, enabled FROM autopilot_state "
                     "WHERE id=1").fetchone()
    if not row or not row[1]:
        print("автопилот выключен или не настраивался", file=sys.stderr)
        print("stop")
        return "stop"
    target = int(row[0])
    total_rem = sum(remaining(db, target).values())
    budget_fine, budget_line = _budget_ok()
    if total_rem == 0:
        db.execute("UPDATE autopilot_state SET enabled=0 WHERE id=1")
        db.commit()
        print("работа завершена — автопилот выключен", file=sys.stderr)
        print("stop")
        return "stop"
    if not budget_fine:
        print(f"потолок бюджета: {budget_line}", file=sys.stderr)
        print("stop")
        return "stop"
    print(f"остаток {total_rem} · {budget_line} → перезапуск rows={target}",
          file=sys.stderr)
    print(str(target))
    return str(target)


if __name__ == "__main__":
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "prepare":
        print("подготовка:", prepare(con), file=sys.stderr)
    elif cmd == "decide":
        decide(con, int(sys.argv[2]), int(sys.argv[3]))
    elif cmd == "cron":
        cron(con)
    elif cmd == "off":
        ensure_tables(con)
        con.execute("UPDATE autopilot_state SET enabled=0 WHERE id=1")
        con.commit()
        print("автопилот выключен")
    else:
        print("команды: prepare | decide <target> <chain> | cron | off")
