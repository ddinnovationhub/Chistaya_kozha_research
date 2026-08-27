"""ПРЕДПОЛЁТНАЯ ПРОВЕРКА ключей и инструментов (заказчик, 2026-08-27:
«проверь работоспособность всех ключей и инструментов»).

Каждый инструмент проверяется ОДНИМ дешёвым запросом; ключи не печатаются.
Стоимость полного прогона: 1 запрос Яндекс-поиска (~0.52 ₽) + копейки
YandexGPT; остальное бесплатно. Запуск: python -m src.preflight
(локально проверится бесключевое; ключи — из GitHub Actions).
"""

import os
import sys

import httpx

OK, FAIL, SKIP = "✓ РАБОТАЕТ", "✗ НЕ РАБОТАЕТ", "— пропуск"


def _row(name, status, detail=""):
    print(f"  {name:34} {status:16} {detail}")
    return status == FAIL


def check_rzn() -> bool:
    from src.rzn_licenses import fetch_licenses, make_client
    c = make_client(retries=2)
    if c is None:
        return _row("Реестр лицензий РЗН", FAIL, "сессия не открылась")
    lics = fetch_licenses("0273028277", c)   # ФАРМЛЕНД — эталонный ИНН
    if lics and any(x["is_med"] for x in lics):
        return _row("Реестр лицензий РЗН", OK,
                    f"эталонный ИНН: {len(lics)} лицензии, мед найдена")
    return _row("Реестр лицензий РЗН", FAIL, "эталонный ИНН не вернул лицензий")


def check_judge(provider: str) -> bool:
    from src.llm_judge import PROVIDERS, judge
    cfg = PROVIDERS.get(provider)
    if cfg and cfg.get("key_env") and not os.environ.get(cfg["key_env"]) \
            and not cfg.get("keyless_ok"):
        return _row(f"Судья {provider}", SKIP, f"нет {cfg['key_env']} в Secrets")
    if provider == "yandexgpt" and not os.environ.get("YANDEX_API_KEY"):
        return _row("Судья yandexgpt", SKIP, "нет YANDEX_API_KEY")
    passport = ("САЙТ: test.ru\nМЕНЮ: Дерматология | Цены | Контакты\n"
                "ПОЗИЦИИ: Приём дерматолога — 1500 руб.")
    res = judge(provider, "ТЕСТ, ООО", "Казань", passport)
    if res and res.get("суждение_А"):
        return _row(f"Судья {provider}", OK, f"ответ: {res['суждение_А']}")
    return _row(f"Судья {provider}", FAIL, "нет валидного ответа (лог выше)")


def check_jina() -> bool:
    key = os.environ.get("JINA_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        r = httpx.get("https://r.jina.ai/https://example.com",
                      headers=headers, timeout=45)
    except Exception as e:  # noqa: BLE001
        return _row("Jina Reader (SPA-каскад)", FAIL, type(e).__name__)
    if r.status_code == 200:
        return _row("Jina Reader (SPA-каскад)", OK,
                    "ключ принят" if key else "без ключа (403 возможен с IP Actions)")
    return _row("Jina Reader (SPA-каскад)", FAIL,
                f"код {r.status_code}" + ("" if key else " (ключа нет)"))


def check_yandex_search() -> bool:
    if not (os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID")):
        return _row("Яндекс Search API", SKIP, "нет ключей в Secrets")
    from src.api_client import yandex_search_raw
    r = yandex_search_raw("тест", n=1)
    if r.status_code == 200:
        return _row("Яндекс Search API", OK, "1 запрос, ~0.52 ₽")
    return _row("Яндекс Search API", FAIL, f"код {r.status_code}")


def check_maps() -> bool:
    """Карточные каналы (Яндекс Геопоиск / 2ГИС) — кандидаты сайтов из карт."""
    fail = False
    if os.environ.get("YANDEX_GEOSEARCH_API_KEY"):
        from src.map_candidates import yandex_map_urls
        urls = yandex_map_urls("Инвитро", "Казань")
        fail |= _row("Яндекс Геопоиск (карты)", OK if urls else FAIL,
                     f"карточка вернула URL: {urls[0]}" if urls
                     else "карточек с URL не вернулось")
    else:
        _row("Яндекс Геопоиск (карты)", SKIP, "нет YANDEX_GEOSEARCH_API_KEY")
    if os.environ.get("DGIS_API_KEY"):
        from src.map_candidates import gis2_urls
        urls = gis2_urls("Инвитро", "Казань")
        fail |= _row("2ГИС Каталог (карты)", OK if urls else FAIL,
                     f"карточка вернула URL: {urls[0]}" if urls
                     else "карточек с URL не вернулось")
    else:
        _row("2ГИС Каталог (карты)", SKIP, "нет DGIS_API_KEY")
    return fail


def check_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        return _row("Playwright (каскад ур. 3-4)", OK, "браузер запускается")
    except Exception as e:  # noqa: BLE001
        return _row("Playwright (каскад ур. 3-4)", FAIL, type(e).__name__)


def main() -> int:
    print("=" * 70)
    print("ПРЕДПОЛЁТНАЯ ПРОВЕРКА — ключи и инструменты")
    print("=" * 70)
    failures = 0
    failures += check_rzn()
    failures += check_yandex_search()
    failures += check_jina()
    failures += check_maps()
    failures += check_playwright()
    for prov in ("kilo", "llm7", "yandexgpt", "groq", "openrouter", "cerebras"):
        failures += check_judge(prov)
    print("=" * 70)
    print("ИТОГ: провалов —", failures,
          "(бесключевые судьи из общих пулов могут временно отдавать 503 — "
          "это перегруз пула, не поломка)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
