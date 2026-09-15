# -*- coding: utf-8 -*-
"""РЗН-добор по списку ИНН вне контура test-40 (заказчик, 2026-09-15:
проверка таблицы типизации — «найди лицензии по ИНН, как делали; внеси
кол-во точек из лицензий и специализации; сделай через actions»).

Список ИНН лежит в data/rzn_extra_inns.txt (по одному в строке) — все ИНН
111 строк-юрлиц таблицы типизации, включая одноимённых тёзок агрегатных
строк. Прогон переиспользует src/rzn_licenses.batch() без изменений:
та же вежливость (пауза 3 с), те же таблицы rzn_licenses / rzn_checked
в data/osint.db, тот же source_id (URL реестра + дата).

Запуск — workflow rzn-extra.yml на self-hosted раннере заказчика
(реестр РЗН блокирует IP облачных датацентров — с раннеров GitHub
не работает; это не обход, а легитимный доступ обычного пользователя).

Команды:
  python -m src.rzn_extra load    # data/rzn_extra_inns.txt → таблица extra_inns
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
    elif cmd == "run":
        cmd_run(float(sys.argv[2]) if len(sys.argv) > 2 else 5400)
    else:
        cmd_report()
