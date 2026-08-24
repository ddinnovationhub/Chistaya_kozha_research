"""Точка входа боевого прогона по городу (GitHub Actions).

Текущий объём: этап 0 (проверка окружения) + живая проверка ключей одним
запросом к каждому API. Пайплайн разведки (этапы 4-8) подключается сюда же
по мере утверждения. Чекпойнт пишется в data/ при любом исходе — воркфлоу
выгружает его артефактом даже при падении шага.

Запуск: CITY='Казань' python -m src.run_city
"""

import datetime
import json
import os
import pathlib
import sys

from src.api_client import dadata_find_raw, handle_api_response, yandex_search_raw

REQUIRED = {
    "YANDEX_API_KEY":    "Яндекс Search API — основной поиск",
    "YANDEX_FOLDER_ID":  "Яндекс Search API — идентификатор каталога",
    "DADATA_API_KEY":    "DaData — проверка ИНН и реквизитов",
    "DADATA_SECRET_KEY": "DaData — секретный ключ",
}


def main() -> int:
    missing = [f"  ✗ {k} — {v}" for k, v in REQUIRED.items() if not os.environ.get(k)]
    if missing:
        print("СТОП. Отсутствуют обязательные ключи:\n" + "\n".join(missing))
        print("Добавь их в GitHub Secrets (Settings → Secrets → Actions)")
        return 1

    city = os.environ.get("CITY", "").strip()
    if not city:
        print("СТОП. Город не задан. Укажи его при запуске: CITY='Казань'")
        return 1

    limit = int(os.environ.get("QUERY_LIMIT", "0") or 0)
    today = datetime.date.today().isoformat()
    checkpoint = {
        "city": city,
        "date": today,
        "query_limit": limit,
        "stage": "smoke",
        "api_checks": {},
    }
    ckpt_path = pathlib.Path("data") / f"checkpoint_{city}_{today}.json"

    try:
        print(f"✓ Окружение проверено. Город: {city}. Лимит запросов: {limit or 'по насыщению'}")

        resp = yandex_search_raw(f"дерматолог {city}", n=1)
        handle_api_response(resp, "Яндекс Search API")
        checkpoint["api_checks"]["yandex_search_api"] = resp.status_code
        print(f"✓ Яндекс Search API: живой запрос прошёл (HTTP {resp.status_code})")

        resp = dadata_find_raw("медицинский центр", city)
        handle_api_response(resp, "DaData")
        checkpoint["api_checks"]["dadata"] = resp.status_code
        print(f"✓ DaData: живой запрос прошёл (HTTP {resp.status_code})")

        print(
            "\nПайплайн разведки (этапы 4-8) ещё не подключён — прогон штатно "
            "остановлен после проверки окружения и ключей. Это ожидаемое поведение."
        )
        checkpoint["stage"] = "smoke_ok"
        return 0
    except Exception as exc:  # noqa: BLE001 — чекпойнт пишется при любом исходе
        checkpoint["stage"] = "failed"
        checkpoint["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        ckpt_path.parent.mkdir(exist_ok=True)
        ckpt_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Чекпойнт: {ckpt_path}")


if __name__ == "__main__":
    sys.exit(main())
