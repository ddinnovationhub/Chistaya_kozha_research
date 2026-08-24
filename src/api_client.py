"""Клиенты внешних API с обязательной индикацией ошибок.
Контракт: 402/429 — стоп с чекпойнтом, 401/403 — стоп сразу,
503/504 — пауза 30 с и повтор, прочее — пропуск запроса."""

import base64
import os

import httpx

from .errors import AuthError, QuotaExhaustedError


def save_checkpoint():
    """Заглушка до этапа 5: чекпойнт пишет модуль графа."""


# Причины 403 по сервисам — конкретика вместо «проверь Secrets»
_FORBIDDEN_HINTS = {
    "Яндекс Search API": (
        "403 у Яндекса — это ДОСТУП ЗАПРЕЩЁН при валидном ключе. Четыре причины по частоте:\n"
        "  1. Платёжный аккаунт не привязан к облаку или неактивен (биллинг)\n"
        "  2. У сервисного аккаунта нет роли search-api.webSearch.user на каталоге\n"
        "  3. Область действия API-ключа не включает yc.search-api.execute\n"
        "  4. В folderId передан ID облака (cloud-id) вместо ID каталога (folder-id)"
    ),
    "DaData": (
        "403 у DaData: ключ неактивен, тариф исчерпан или запрос без заголовка "
        "Authorization: Token. Проверь dadata.ru/profile/"
    ),
}


def _body_excerpt(response) -> str:
    """Тело ответа при любом коде кроме 200, обрезка до 2000 символов."""
    try:
        text = response.text or ""
    except Exception:  # noqa: BLE001 — диагностика не должна ронять обработчик
        return "<тело ответа недоступно>"
    return text[:2000] + ("… [обрезано]" if len(text) > 2000 else "")


def handle_api_response(response, service_name: str):
    code = response.status_code
    if code == 200:
        return response

    print(f"— тело ответа {service_name} (HTTP {code}): {_body_excerpt(response)}")

    if code in (402, 429):
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

    elif code == 401:
        msg = (f"⛔ 401 АУТЕНТИФИКАЦИЯ — {service_name}: ключ отсутствует, опечатан "
               f"или отозван. Сервис не узнал ключ. Проверь значение в GitHub Secrets.")
        print(msg)
        raise AuthError(msg)

    elif code == 403:
        hint = _FORBIDDEN_HINTS.get(
            service_name, "доступ запрещён при валидном ключе — проверь права/тариф")
        msg = f"⛔ 403 ДОСТУП ЗАПРЕЩЁН — {service_name}.\n{hint}"
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
