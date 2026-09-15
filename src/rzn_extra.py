# -*- coding: utf-8 -*-
"""РЗН-добор по списку ИНН вне контура test-40 (заказчик, 2026-09-15:
проверка таблицы типизации — «найди лицензии по ИНН, как делали; внеси
кол-во точек из лицензий и специализации; сделай через actions»).

Список ИНН лежит в data/rzn_extra_inns.txt (по одному в строке) — все ИНН
111 строк-юрлиц таблицы типизации, включая одноимённых тёзок агрегатных
строк. Прогон переиспользует src/rzn_licenses.batch() без изменений:
та же вежливость (пауза 3 с), те же таблицы rzn_licenses / rzn_checked
в data/osint.db, тот же source_id (URL реестра + дата).

Запуск — workflow rzn-extra.yml кнопкой (заказчик, 2026-09-15: «сделать
так, чтобы запускался кнопкой в репозитарии») на обычном раннере GitHub,
как остальные экшнс репо. В августе 2026 реестр блокировал облачные IP
(ConnectError), поэтому перед прогоном ОБЯЗАТЕЛЬНА самопроверка канала:
запрос 3 контрольных ИНН с заведомо известными лицензиями. Реестр отдаёт
всем «200 OK», но с заблокированного IP — пустой data: формальный успех
без данных (suspicious_zero). Все 3 контрольных пусты → прогон падает с
явным сообщением, НЕ пишет «лицензий не найдено» по 261 ИНН.

Команды:
  python -m src.rzn_extra load       # data/rzn_extra_inns.txt → таблица extra_inns
  python -m src.rzn_extra selfcheck  # канал до прогона; код 1 = облако глушится
  python -m src.rzn_extra run [бюджет_сек=5400]
  python -m src.rzn_extra report
"""
import sqlite3
import sys

DB = "data/osint.db"
LIST = "data/rzn_extra_inns.txt"


def open_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS extra_inns (inn TEXT PRIMARY KEY)")
    return db


def cmd_load():
    db = open_db()
    inns = [ln.strip() for ln in open(LIST, encoding="utf-8")
            if ln.strip().isdigit() and len(ln.strip()) in (10, 12)]
    db.executemany("INSERT OR IGNORE INTO extra_inns VALUES (?)",
                   [(i,) for i in inns])
    db.commit()
    n = db.execute("SELECT COUNT(*) FROM extra_inns").fetchone()[0]
    print(f"в списке файла: {len(inns)} · в таблице extra_inns: {n}")


def cmd_selfcheck():
    """Канал к РЗН с текущего IP. Контрольные ИНН берутся из уже собранных
    лицензий (rzn_licenses) — по ним реестр обязан вернуть строки. Все
    контрольные пусты/сбой → выход с кодом 1 и понятным сообщением."""
    import time
    from src.rzn_licenses import make_client, fetch_licenses
    db = open_db()
    controls = [r[0] for r in db.execute(
        "SELECT DISTINCT inn FROM rzn_licenses LIMIT 3")]
    if not controls:
        print("⚠ Самопроверка пропущена: в rzn_licenses нет контрольных ИНН")
        return
    client = make_client()
    if client is None:
        print("⛔ РЗН недоступен: сессия не открылась. Прогон остановлен.")
        sys.exit(1)
    got = 0
    for inn in controls:
        rows = fetch_licenses(inn, client)
        n = len(rows) if rows else 0
        print(f"  контроль {inn}: {'строк ' + str(n) if rows is not None else 'запрос не удался'}")
        got += n
        time.sleep(3)
    if got == 0:
        print("⛔ СТОП. Реестр РЗН отвечает «200 OK», но по всем контрольным ИНН "
              "с заведомо существующими лицензиями отдаёт пустые данные — "
              "IP этого раннера глушится реестром (формальный успех без данных). "
              "Прогон остановлен, чтобы не записать ложное «лицензий не найдено». "
              "Вариант: self-hosted раннер (Settings → Actions → Runners) "
              "или локальный скрипт tools/rzn_extra_local.py.")
        sys.exit(1)
    print(f"✓ Канал к РЗН работает: контрольные ИНН вернули {got} строк(и)")


def cmd_run(budget_sec: float):
    from src.rzn_licenses import batch, ensure_tables
    db = open_db()
    ensure_tables(db)
    stats = batch(db, table="extra_inns", budget_sec=budget_sec)
    print("итог:", stats)


def cmd_report():
    db = open_db()
    rows = db.execute(
        "SELECT e.inn, k.status, k.med_licenses_n, "
        " (SELECT SUM(objects_n) FROM rzn_licenses l "
        "  WHERE l.inn=e.inn AND l.is_med=1) "
        "FROM extra_inns e LEFT JOIN rzn_checked k ON k.inn=e.inn "
        "ORDER BY e.inn").fetchall()
    done = sum(1 for r in rows if r[1] == "проверен")
    print(f"ИНН в очереди: {len(rows)} · проверено: {done} · "
          f"осталось: {len(rows)-done}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "load":
        cmd_load()
    elif cmd == "selfcheck":
        cmd_selfcheck()
    elif cmd == "run":
        cmd_run(float(sys.argv[2]) if len(sys.argv) > 2 else 5400)
    else:
        cmd_report()
