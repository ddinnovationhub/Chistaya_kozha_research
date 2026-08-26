"""[ВЫВЕДЕН ИЗ ФЛОУ — promt_spark_krug, 2026-08-25] Вход теперь — выборка СПАРК (src/spark_import.py), слепой discovery отключён. Код сохранён на случай возврата подхода.

Импорт локального L1-файла в очередь кандидатов (решение 2026-08-26, п.4).

Вход — output/{город}_L1_{дата}.json от src/run_l1 (локальная машина
заказчика). Кандидаты вливаются через CandidateQueue (дедуп при записи, G0),
журнал рубрик — в таблицу queries (те же query_id, что дал бы прогон).

Запуск: python -m src.import_l1 --city "Новосибирск" --file output/..._L1_....json
"""

import argparse
import datetime
import json
import pathlib

from src.discovery import CandidateQueue, open_db


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--file", required=True)
    args = ap.parse_args()

    data = json.loads(pathlib.Path(args.file).read_text(encoding="utf-8"))
    db = open_db()
    queue = CandidateQueue(db)
    added = 0
    for c in data["candidates"]:
        added += queue.add({"title": c["title"], "url": c["url"], "domain": c["domain"]},
                           c["query_id"], "L1-local (ручной запуск заказчика)")
    for j in data.get("journal", []):
        db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (j["query_id"], 1, "rubric", args.city, j["text"],
                    "L1_RUBRICS (локальный запуск)",
                    datetime.datetime.now().isoformat(timespec="seconds"),
                    j["n_results"], j["n_new"], None, j["status"]))
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    print(f"L1 влит: новых кандидатов {added} из {len(data['candidates'])} "
          f"(дедуп при записи) · всего в очереди {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
