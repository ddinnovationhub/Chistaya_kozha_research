"""Кандидаты сайтов из КАРТ (заказчик, 2026-08-27: «в яндекс картах клиника
числится и в карточке есть сайт — почему это не сработало?»).

Каналы: Яндекс Геопоиск (API поиска по организациям) и 2ГИС Каталог.
Оба требуют бесплатных ключей (developer.tech.yandex.ru / dev.2gis.com);
без ключа канал честно пропускается с сообщением.

ПРАВОВОЙ РЕЖИМ (CLAUDE.md, зафиксирован): данные карточек организаций
НЕ сохраняются — из ответа берётся ТОЛЬКО URL сайта как кандидат;
подтверждение принадлежности — наша лестница (ИНН / адрес лицензии /
адрес+название) по содержимому самого сайта.

Кейс, ради которого канал нужен: франчайзи федеральной сети (ИНВИТРО и
т.п.) — собственного сайта нет, веб-поиск по названию юрлица пуст, а в
карточке на картах указан сайт БРЕНДА; принадлежность юрлица к нему
подтверждает адрес точки из лицензии РЗН (сайты сетей публикуют адреса
всех офисов).
"""

import os

import httpx


def yandex_map_urls(name: str, city: str, n: int = 3) -> list[str]:
    """URL сайтов из карточек Яндекс-Геопоиска (только URL, ничего более)."""
    key = os.environ.get("YANDEX_GEOSEARCH_API_KEY")
    if not key:
        return []
    try:
        r = httpx.get("https://search-maps.yandex.ru/v1/",
                      params={"text": f"{name} {city}", "type": "biz",
                              "lang": "ru_RU", "results": 5, "apikey": key},
                      timeout=20)
        if r.status_code != 200:
            print(f"⚠ Яндекс Геопоиск: код {r.status_code}")
            return []
        out = []
        for f in r.json().get("features", []):
            meta = (f.get("properties") or {}).get("CompanyMetaData") or {}
            url = (meta.get("url") or "").strip()
            if url and url not in out:
                out.append(url)
            if len(out) >= n:
                break
        return out
    except Exception as e:  # noqa: BLE001
        print(f"⚠ Яндекс Геопоиск: {type(e).__name__}")
        return []


def gis2_urls(name: str, city: str, n: int = 3) -> list[str]:
    """URL сайтов из карточек 2ГИС Каталога (только URL, ничего более)."""
    key = os.environ.get("DGIS_API_KEY")
    if not key:
        return []
    try:
        r = httpx.get("https://catalog.api.2gis.com/3.0/items",
                      params={"q": f"{name} {city}", "page_size": 5,
                              "fields": "items.contact_groups", "key": key},
                      timeout=20)
        if r.status_code != 200:
            print(f"⚠ 2ГИС: код {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        # диагностика (preflight 2026-08-27: «карточек не вернулось» без
        # причины): 2ГИС кладёт ошибку в meta при HTTP 200
        meta = data.get("meta") or {}
        if meta.get("error"):
            print(f"⚠ 2ГИС meta.error: "
                  f"{str(meta['error'])[:250]}")
            return []
        items = (data.get("result") or {}).get("items", [])
        if not items:
            print(f"⚠ 2ГИС: items пуст; meta={str(meta)[:200]}")
        out = []
        for item in items:
            for grp in item.get("contact_groups") or []:
                for c in grp.get("contacts") or []:
                    if c.get("type") == "website":
                        url = (c.get("url") or c.get("value") or "").strip()
                        if url and url not in out:
                            out.append(url)
            if len(out) >= n:
                break
        if items and not out:
            # карточки есть, а сайтов нет — показать, что реально пришло
            print(f"⚠ 2ГИС: {len(items)} карточек без website; ключи первой: "
                  f"{sorted(items[0].keys())[:12]}")
        return out[:n]
    except Exception as e:  # noqa: BLE001
        print(f"⚠ 2ГИС: {type(e).__name__}")
        return []


def map_candidates(name: str, city: str) -> list[str]:
    """Кандидаты из обоих карточных каналов, без дублей."""
    out = []
    for u in yandex_map_urls(name, city) + gis2_urls(name, city):
        if u not in out:
            out.append(u)
    return out
