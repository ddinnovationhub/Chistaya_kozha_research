"""L1 (каталоги) с ЛОКАЛЬНОЙ машины заказчика (решение 2026-08-26, п.4).

2ГИС отдаёт CAPTCHA, Яндекс.Карты — заглушку «limited» датацентровым IP
GitHub Actions; CAPTCHA не обходим. Рубричный слой находит клиники со
слабыми сайтами — терять нельзя, поэтому каталоги гоняются отдельно,
с обычного IP. Если и здесь придёт капча — честный статус blocked.

Запуск (нужны Python 3.11+, playwright + chromium):
    pip install -r requirements.txt && python -m playwright install chromium
    python -m src.run_l1 --city "Новосибирск"

Результат: output/{город}_L1_{дата}.json — кандидаты + журнал по рубрикам.
Вливается в очередь: python -m src.import_l1 --city "Новосибирск" --file <json>
"""

import argparse
import datetime
import json
import pathlib
import tempfile

from src.catalogs import run_l1
from src.discovery import open_db
from src.query_gen import generate_l1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    l1_queries = generate_l1(args.city)
    # временная база: кандидаты и журнал собираются в неё, наружу идёт JSON
    with tempfile.TemporaryDirectory() as tmp:
        db = open_db(pathlib.Path(tmp) / "l1_tmp.db")
        summary = run_l1(args.city, l1_queries, db)
        cands = [dict(zip(("title", "url", "domain", "query_id", "source_id"), row))
                 for row in db.execute(
                     "SELECT title, url, domain, discovered_by_query, source_id "
                     "FROM candidates")]
        journal = [dict(zip(("query_id", "text", "n_results", "n_new", "status"), row))
                   for row in db.execute(
                       "SELECT query_id, text, n_results, n_new_candidates, status "
                       "FROM queries")]

    day = datetime.date.today().isoformat()
    out = pathlib.Path(args.out or f"output/{args.city}_L1_{day}.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(
        {"city": args.city, "date": day, "summary": {k: v for k, v in summary.items()
                                                     if k != "diag"},
         "journal": journal, "candidates": cands},
        ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"L1 локально: рубрик {summary['l1_executed']} · "
          f"кандидатов {len(cands)} · ошибок/блокировок {summary['l1_errors']} · "
          f"подозрительных нулей {summary['l1_suspicious_zero']}")
    if summary.get("quality_note"):
        print(f"⚠ {summary['quality_note']}")
    print(f"Файл: {out}")
    print(f"Влить в очередь: python -m src.import_l1 --city '{args.city}' --file '{out}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
