"""Точка входа боевого прогона по городу (GitHub Actions).

Текущий объём: этап 0 + смоук ключей. Смоук прогоняет ВСЕ проверки и выводит
сводную таблицу, а не падает на первой ошибке (решение заказчика 2026-08-24).
Бюджет: каждый платный запрос списывается через BudgetTracker (data/budget.json,
накопительно по проекту). Чекпойнт пишется при любом исходе.

Запуск: CITY='Казань' python -m src.run_city
"""

import datetime
import json
import os
import pathlib
import sys

from src.api_client import dadata_find_raw, handle_api_response, yandex_search_raw
from src.budget import BudgetTracker

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
        print("\nПайплайн разведки (этапы 5-8) ещё не подключён — прогон штатно "
              "остановлен после смоука. Это ожидаемое поведение.")
        checkpoint["stage"] = "smoke_ok"
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
