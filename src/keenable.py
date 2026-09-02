"""Keenable — ВТОРОЙ источник кандидатов сайтов (заказчик, 2026-09-02).

Независимый поисковый индекс (api.keenable.ai): 100 000 запросов в месяц
бесплатно, ≤10 запросов/с; ключ KEENABLE_API_KEY из Secrets, без ключа —
публичный эндпоинт (≤1000/час на IP, для отладки).

Замер 2026-09-02 на эталоне (сайты, найденные Яндексом и подтверждённые
ИНН): «название + город» — 23/30 в топ-10, слой по ИНН — 10/25. Вывод:
НЕ замена Яндекса (минус четверть находок), а дополнение — опрашивается
только там, где Яндекс кандидатов не дал или они не прошли лестницу.

Правовой режим (Юрист OSINT): Keenable подтвердил заказчику, что наш кейс
не является коммерческим использованием (2026-09-02). Храним только URL,
сниппеты/описания не сохраняются — как и для Яндекса. Лимиты чтятся через
src.quota (суточный потолок 3000 ≈ 90 000/мес с запасом под 100 000).
"""

import os
import time

import httpx

from src.dedup import normalize_domain
from src.quota import spend

API = "https://api.keenable.ai/v1/search"
APP_TITLE = "chk-osint"          # обязателен на публичном эндпоинте
_warned: set[str] = set()


def _warn_once(key: str, msg: str):
    if key not in _warned:
        _warned.add(key)
        print(msg)


def keenable_search(query: str, n: int = 20) -> list[dict]:
    """Кандидаты по запросу: [{url, domain, title}]. Пусто — источник молчит
    (нет квоты, отказ, сеть): дополнительный источник не валит этап."""
    key = os.environ.get("KEENABLE_API_KEY")
    url = API if key else API + "/public"
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    else:
        headers["X-Keenable-Title"] = APP_TITLE
        _warn_once("nokey", "ℹ Keenable: нет KEENABLE_API_KEY — публичный "
                            "эндпоинт (≤1000/час на IP)")
    if not spend("keenable"):
        return []
    for attempt in (1, 2):
        try:
            r = httpx.post(url, headers=headers,
                           json={"query": query, "max_results": n}, timeout=25)
        except Exception as e:  # noqa: BLE001
            _warn_once("net", f"⚠ Keenable недоступен: {type(e).__name__}")
            return []
        if r.status_code == 429 and attempt == 1:
            time.sleep(min(float(r.headers.get("Retry-After", 2)), 10))
            continue
        if r.status_code in (401, 403):
            _warn_once("auth", f"⛔ Keenable: отказ авторизации (код "
                               f"{r.status_code}) — проверь KEENABLE_API_KEY; "
                               f"источник пропущен")
            return []
        if r.status_code != 200:
            _warn_once(f"code{r.status_code}",
                       f"⚠ Keenable: код {r.status_code} {r.text[:120]}")
            return []
        out = []
        for hit in r.json().get("results", []):
            u = (hit.get("url") or "").strip()
            if u:
                out.append({"url": u, "domain": normalize_domain(u),
                            "title": (hit.get("title") or "")[:200]})
        return out
    return []
