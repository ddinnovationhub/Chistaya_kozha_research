"""Слой L1 — рубрики каталогов (2ГИС, Яндекс Карты) через Playwright.

Решение заказчика 2026-08-25 по L1: вариант (а) — контур поднят до боевого
прогона, с автоматическим фолбэком в (б): если каталог недоступен/блокирует,
в чекпойнт и журнал пишется quality_note «рубричный слой не выполнялся,
полнота занижена» — молчаливого «отложено» нет.

Правовой режим (sources.yaml): из каталогов сохраняется ТОЛЬКО факт
существования карточки + название + URL карточки (для 2ГИС — и адрес из
подписи карточки, если виден в выдаче). Содержимое карточек не сохраняется.
Темп ≤1 запрос/3 с, честный браузерный User-Agent.

ВАЖНО: в песочнице разработки браузерный HTTPS через MITM-прокси не работает
(ERR_CONNECTION_RESET на любом сайте, проверено 2026-08-25) — контур
проверяется первым прогоном в GitHub Actions, где прокси нет.
"""

import asyncio
import datetime
import os
import sqlite3

from src.dedup import normalize_domain

RATE_DELAY_SEC = 3


async def _collect_2gis(page, city_slug: str, rubric: str) -> list[dict]:
    url = f"https://2gis.ru/{city_slug}/search/{rubric}"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)
    cards = await page.eval_on_selector_all(
        "a[href*='/firm/']",
        "els => els.map(e => ({title: e.textContent?.trim(), href: e.href}))")
    seen, out = set(), []
    for c in cards:
        if not c.get("title") or not c.get("href"):
            continue
        key = c["href"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": c["title"], "url": key, "domain": "2gis.ru"})
    return out


async def _collect_yandex_maps(page, city: str, rubric: str) -> list[dict]:
    url = f"https://yandex.ru/maps/?text={rubric} {city}"
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(5000)
    cards = await page.eval_on_selector_all(
        "a[href*='/maps/org/']",
        "els => els.map(e => ({title: e.getAttribute('aria-label') || e.textContent?.trim(), href: e.href}))")
    seen, out = set(), []
    for c in cards:
        if not c.get("title") or not c.get("href"):
            continue
        key = c["href"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": c["title"], "url": key, "domain": "maps.yandex.ru"})
    return out


async def run_l1_async(city: str, city_slug: str, l1_queries: list[dict],
                       db: sqlite3.Connection) -> dict:
    from playwright.async_api import async_playwright
    from src.discovery import CandidateQueue

    queue = CandidateQueue(db)
    executed, errors, new_total = 0, 0, 0
    notes = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="ru-RU")
        page = await ctx.new_page()
        for q in l1_queries:
            rubric = q["text"].rsplit(" ", 1)[0]  # текст = «{рубрика} {город}»
            n_results = n_new = 0
            status = "ok"
            try:
                for collector, src_id in ((_collect_2gis, "gis2"),
                                          (_collect_yandex_maps, "yandex_maps")):
                    if collector is _collect_2gis:
                        cards = await collector(page, city_slug, rubric)
                    else:
                        cards = await collector(page, city, rubric)
                    n_results += len(cards)
                    n_new += sum(queue.add(c, q["query_id"], src_id) for c in cards)
                    await asyncio.sleep(RATE_DELAY_SEC)
                executed += 1
                status = "ok" if n_new else ("0_results" if not n_results else "0_new")
            except Exception as exc:  # noqa: BLE001 — блок каталога не роняет прогон
                errors += 1
                status = "blocked"
                notes.append(f"{q['query_id']}: {type(exc).__name__}")
            new_total += n_new
            db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (q["query_id"], q["layer"], q["template_id"], city, q["text"],
                        q["source"], datetime.datetime.now().isoformat(timespec="seconds"),
                        n_results, n_new, q.get("wordstat_freq"), status))
            db.commit()
        await browser.close()

    result = {"l1_executed": executed, "l1_errors": errors, "l1_new_candidates": new_total}
    if errors or not executed:
        result["quality_note"] = ("рубричный слой (L1) не выполнялся или выполнен частично "
                                  f"({executed} из {len(l1_queries)} рубрик, ошибок {errors}) — "
                                  "полнота занижена")
        result["error_details"] = notes[:5]
    return result


def run_l1(city: str, l1_queries: list[dict], db: sqlite3.Connection) -> dict:
    """Синхронная обёртка. city_slug для 2ГИС берётся из city_code-транслита."""
    from src.query_gen import city_code
    if os.environ.get("SKIP_L1"):
        return {"l1_executed": 0, "l1_errors": 0, "l1_new_candidates": 0,
                "quality_note": "рубричный слой (L1) отключён переменной SKIP_L1 — полнота занижена"}
    try:
        return asyncio.run(run_l1_async(city, city_code(city), l1_queries, db))
    except Exception as exc:  # noqa: BLE001 — каталоги не должны ронять весь прогон
        return {"l1_executed": 0, "l1_errors": len(l1_queries), "l1_new_candidates": 0,
                "quality_note": "рубричный слой (L1) не выполнялся "
                                f"({type(exc).__name__}) — полнота занижена"}
