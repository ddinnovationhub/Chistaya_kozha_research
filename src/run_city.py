"""Точка входа боевого прогона по городу (GitHub Actions).

Объём: этап 0 (смоук всех проверок со сводной таблицей) → этап 5 (discovery
по утверждённому промпту prompts/05_discovery_executor.md). Проверка сайтов
(этап 6) подключается после утверждения её промпта. Бюджет: каждый платный
запрос списывается через BudgetTracker ДО отправки (data/budget.json,
накопительно по проекту). Чекпойнт пишется при любом исходе.

Запуск: CITY='Казань' python -m src.run_city
"""

import datetime
import json
import os
import pathlib
import sys

import yaml

from src.api_client import dadata_find_raw, handle_api_response, yandex_search_raw
from src.budget import BudgetTracker
from src.discovery import open_db, run_discovery
from src.query_gen import city_code, generate_all
from src.recall import compute_recall

REQUIRED = {
    "YANDEX_API_KEY":    "Яндекс Search API — основной поиск",
    "YANDEX_FOLDER_ID":  "Яндекс Search API — идентификатор каталога",
    "DADATA_API_KEY":    "DaData — проверка ИНН и реквизитов",
    "DADATA_SECRET_KEY": "DaData — секретный ключ",
}


def main() -> int:
    city = os.environ.get("CITY", "").strip()
    limit = int(os.environ.get("QUERY_LIMIT", "0") or 0)
    today = datetime.date.today().isoformat()

    results = []  # (проверка, статус, детали)

    for key, purpose in REQUIRED.items():
        ok = bool(os.environ.get(key))
        results.append((f"ключ {key}", "OK" if ok else "ОШИБКА",
                        purpose if ok else "отсутствует в окружении"))
    results.append(("параметр CITY", "OK" if city else "ОШИБКА",
                    city or "не задан (CITY='Казань')"))

    keys_ok = all(s == "OK" for _, s, _ in results)
    checkpoint = {"city": city or None, "date": today, "query_limit": limit,
                  "stage": "smoke", "api_checks": {}}

    budget = None
    try:
        budget = BudgetTracker()
        results.append(("бюджетный счётчик", "OK", budget.report()))
    except Exception as exc:  # noqa: BLE001
        results.append(("бюджетный счётчик", "ОШИБКА", f"{type(exc).__name__}: {exc}"))

    if keys_ok and budget is not None:
        for name, service_key, call in [
            ("Яндекс Search API", "yandex_search_api",
             lambda: yandex_search_raw(f"дерматолог {city}", n=1)),
            ("DaData", "dadata",
             lambda: dadata_find_raw("медицинский центр", city)),
        ]:
            try:
                budget.charge(service_key, 1)
                resp = call()
                handle_api_response(resp, name)
                checkpoint["api_checks"][service_key] = resp.status_code
                results.append((f"живой запрос {name}", "OK", f"HTTP {resp.status_code}"))
            except Exception as exc:  # noqa: BLE001 — собираем ВСЕ результаты
                checkpoint["api_checks"][service_key] = f"{type(exc).__name__}"
                results.append((f"живой запрос {name}", "ОШИБКА",
                                f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"))
    else:
        results.append(("живые запросы API", "ПРОПУЩЕНО",
                        "нет ключей или счётчика — вызовы не выполнялись"))

    # ── Сводная таблица ──────────────────────────────────────────────────
    failed = [r for r in results if r[1] == "ОШИБКА"]
    width = max(len(r[0]) for r in results)
    print("\n" + "═" * 72)
    print(f"СМОУК-ПРОВЕРКА · город: {city or '—'} · {today}")
    print("─" * 72)
    for name, status, detail in results:
        mark = {"OK": "✓", "ОШИБКА": "✗", "ПРОПУЩЕНО": "·"}[status]
        print(f" {mark} {name.ljust(width)}  {status.ljust(9)}  {detail}")
    print("─" * 72)
    if budget is not None:
        print(f" {budget.report()}")
    print(f" Итог: {len(results) - len(failed)}/{len(results)} проверок пройдено")
    print("═" * 72)

    if not failed:
        checkpoint["stage"] = "smoke_ok"
        # ── Этап 5: discovery по утверждённому промпту ────────────────────
        try:
            services = yaml.safe_load(pathlib.Path("dictionaries/services.yaml").read_text(encoding="utf-8"))
            nosology = yaml.safe_load(pathlib.Path("dictionaries/nosology.yaml").read_text(encoding="utf-8"))
            districts = None
            dfile = pathlib.Path("data/city_districts.json")
            if dfile.exists():
                districts = json.loads(dfile.read_text(encoding="utf-8")).get(city)
            queries = generate_all(city, services, nosology, districts)
            if districts:
                print(f"L6 включён: {len(districts)} районов ({dfile})")
            else:
                print("L6 выключен: районов для города нет в data/city_districts.json — полнота по окраинам ниже")
            qfile = pathlib.Path("data") / f"queries_{city_code(city)}.jsonl"
            with qfile.open("w", encoding="utf-8") as f:
                for q in queries:
                    f.write(json.dumps(q, ensure_ascii=False) + "\n")
            print(f"\nDISCOVERY · {city}: запросов в списке {len(queries)} "
                  f"(лимит: {limit or 'по насыщению'})")
            db = open_db()
            # L1 (каталоги) — решение заказчика 2026-08-25, вариант (а):
            # Playwright-контур в бою; блок каталога → quality_note, не молчание
            from src.catalogs import run_l1
            l1_queries = [q for q in queries if q["layer"] == 1]
            l1 = run_l1(city, l1_queries, db)
            checkpoint["l1"] = l1
            summary = run_discovery(city, queries, limit=limit, budget=budget, db=db)
            checkpoint["discovery"] = summary
            checkpoint["stage"] = "discovery_done"
            print("─" * 72)
            print(f" L1 (каталоги): рубрик исполнено {l1['l1_executed']}/{len(l1_queries)}"
                  f" · новых кандидатов: {l1['l1_new_candidates']}"
                  f" · ошибок: {l1['l1_errors']}"
                  f" · подозрительных нулей: {l1.get('l1_suspicious_zero', 0)}")
            if l1.get("quality_note"):
                print(f" ⚠ КАЧЕСТВО: {l1['quality_note']}")
            print(f" Запросов исполнено (API): {summary['queries_executed']}"
                  f"/{summary['queries_total_api']} · ошибок: {summary['queries_errors']}")
            for lname, st in sorted(summary.get("layer_stats", {}).items()):
                print(f"   {lname}: исполнено {st['executed']} · новых кандидатов {st['new_candidates']} · ошибок {st['errors']}")
            if summary.get("suspicious_zero_layers"):
                print(f" ⚠ ПОДОЗРИТЕЛЬНЫЙ НОЛЬ по слоям: {', '.join(summary['suspicious_zero_layers'])} — "
                      f"исполнены без ошибок, но 0 новых кандидатов; разобрать до доверия слою")
            print(f" Уникальных кандидатов в очереди: {summary['candidates_unique']}")
            print(f" Насыщение: {summary['saturation']['reason']}")
            recall = compute_recall(city, db)
            checkpoint["recall"] = recall
            if recall is None:
                print(f" Recall-тест: файл data/recall_test_{city}.yaml НЕ НАЙДЕН — "
                      f"полнота не проверена (запросить список известных клиник у заказчика)")
            else:
                print(f" Recall-тест (известные клиники): {recall['found']}/{recall['total']}")
                if recall["missed"]:
                    # правило заказчика 2026-08-25: выводить всех, обрезка запрещена без пометки
                    print(f"   не найдены (все {len(recall['missed'])}): "
                          + "; ".join(recall["missed"]))
            print(f" {summary['budget']}")
            print("─" * 72)
            print("Проверка сайтов (этап 6): промпт утверждён 2026-08-25, код в разработке — не запускается.")
        except Exception as exc:  # noqa: BLE001 — чекпойнт при любом исходе
            checkpoint["stage"] = "discovery_failed"
            checkpoint["discovery_error"] = f"{type(exc).__name__}: {exc}"
            print(f"⛔ discovery остановлен: {type(exc).__name__}: {exc}")
            failed.append(("discovery", "ОШИБКА", str(exc)[:160]))
    else:
        checkpoint["stage"] = "smoke_failed"
        checkpoint["failed"] = [r[0] for r in failed]

    ckpt_path = pathlib.Path("data") / f"checkpoint_{city or 'nocity'}_{today}.json"
    ckpt_path.parent.mkdir(exist_ok=True)
    ckpt_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Чекпойнт: {ckpt_path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
