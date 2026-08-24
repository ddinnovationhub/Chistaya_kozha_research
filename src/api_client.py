"""Клиенты внешних API с обязательной индикацией ошибок.
Контракт: 402/429 — стоп с чекпойнтом, 401/403 — стоп сразу,
503/504 — пауза 30 с и повтор, прочее — пропуск запроса."""

import base64
import os

import httpx

from .errors import AuthError, QuotaExhaustedError


def save_checkpoint():
    """Заглушка до этапа 5: чекпойнт пишет модуль графа."""


def handle_api_response(response, service_name: str):
    code = response.status_code
    if code == 200:
        return response

    elif code in (402, 429):
        msg = (
            f"\n{'='*60}\n"
            f"⛔ ЛИМИТ ИСЧЕРПАН — {service_name} (код {code})\n"
            f"Что делать:\n"
            f"  • Яндекс Search API → пополни баланс на aistudio.yandex.ru\n"
            f"  • DaData → проверь лимит на dadata.ru/profile/\n"
            f"  • Perplexity → проверь баланс на perplexity.ai/settings\n"
            f"Уже собранные данные сохранены. Прогон приостановлен.\n"
            f"{'='*60}"
        )
        print(msg)
        save_checkpoint()
        raise QuotaExhaustedError(msg)

    elif code in (401, 403):
        msg = f"⛔ ОШИБКА АВТОРИЗАЦИИ — {service_name} (код {code}). Проверь GitHub Secrets."
        print(msg)
        raise AuthError(msg)

    elif code in (503, 504):
        print(f"⚠ {service_name} временно недоступен (код {code}). Жду 30 сек...")
        import time
        time.sleep(30)
        return None  # сигнал повторить запрос

    else:
        print(f"⚠ {service_name} вернул код {code}. Пропускаю запрос.")
        return None


def yandex_search_raw(query: str, n: int = 10) -> httpx.Response:
    return httpx.post(
        "https://searchapi.api.cloud.yandex.net/v2/web/search",
        headers={"Authorization": f"Api-Key {os.environ['YANDEX_API_KEY']}"},
        json={
            "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
            "folderId": os.environ["YANDEX_FOLDER_ID"],
            "groupings": [{"groupMode": "FLAT", "groupsOnPage": n}],
            "responseFormat": "FORMAT_XML",
        },
        timeout=30,
    )


def dadata_find_raw(name: str, city: str) -> httpx.Response:
    return httpx.post(
        "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party",
        headers={
            "Authorization": f"Token {os.environ['DADATA_API_KEY']}",
            "Content-Type": "application/json",
        },
        json={"query": f"{name} {city}", "count": 3},
        timeout=15,
    )


def dadata_find(name: str, city: str) -> dict | None:
    resp = dadata_find_raw(name, city)
    handle_api_response(resp, "DaData")
    suggestions = resp.json().get("suggestions", [])
    return suggestions[0]["data"] if suggestions else None
