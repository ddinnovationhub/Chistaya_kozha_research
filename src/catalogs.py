"""Слой L1 — рубрики каталогов (2ГИС, Яндекс Карты) через Playwright.

ПРАВИЛО «ПОДОЗРИТЕЛЬНЫЙ НОЛЬ» (блокер заказчика 2026-08-25): ноль результатов
без исключения — НЕ успех. Такой запрос получает статус suspicious_zero,
сырой HTML сохраняется в data/l1_diag/, в отчёт идёт явная строка качества.
Молчаливый ноль запрещён так же, как молчаливое «отложено».

Инструментировка каждого запроса: HTTP-статус, размер HTML в байтах,
число совпадений селектора — пишется в диагностику чекпойнта.

Диагностика 2026-08-25 (см. PROGRESS):
- 2ГИС: сырой HTML = пустой SPA-каркас (~11 КБ, ноль /firm/) — данные только
  через XHR после рендера; нужен реальный рендер и ожидание ПО СЕЛЕКТОРУ.
- Яндекс.Карты: рабочий URL — /maps/{region_id}/{slug}/search/{query}/
  (страница ?text= редиректит на yandex.com и списка не даёт — ЭТО была
  причина нуля в первом прогоне); SSR отдаёт лишь часть выдачи (5 из 25),
  полный список требует рендера.

Правовой режим (sources.yaml): сохраняется только факт существования карточки
+ название + URL карточки. Темп ≤1 запрос/3 с.
"""

import asyncio
import datetime
import gzip
import os
import pathlib
import sqlite3

RATE_DELAY_SEC = 3
SELECTOR_2GIS = "a[href*='/firm/']"
SELECTOR_YMAPS = "a[href*='/maps/org/']"

# Регионы Яндекс.Карт: город → (region_id, slug) для URL /maps/{id}/{slug}/search/
# Заполняется по мере городов; id проверен живым запросом (200 + выдача).
YANDEX_MAPS_REGIONS = {
    "Новосибирск": (65, "novosibirsk"),   # проверено 2026-08-25: HTTP 200, орг-ссылки в SSR
}

_DIAG_DIR = pathlib.Path("data/l1_diag")


def _save_html(city: str, tag: str, content: str) -> str:
    d = _DIAG_DIR / city
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{tag}.html.gz"
    path.write_bytes(gzip.compress(content.encode("utf-8", errors="replace")))
    return str(path)


async def _grab(page, url: str, selector: str, wait_ms: int = 15000) -> dict:
    """Открыть страницу, дождаться селектора (или таймаута), снять метрики."""
    resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    status = resp.status if resp else None
    try:
        await page.wait_for_selector(selector, timeout=wait_ms)
    except Exception:  # noqa: BLE001 — отсутствие селектора само по себе диагноз
        pass
    # скролл подталкивает ленивую подгрузку выдачи
    for _ in range(3):
        await page.mouse.wheel(0, 2000)
        await page.wait_for_timeout(1200)
    content = await page.content()
    cards = await page.eval_on_selector_all(
        selector, "els => els.map(e => ({title: e.getAttribute('aria-label') || e.textContent?.trim(), href: e.href}))")
    return {"http_status": status, "bytes": len(content), "selector": selector,
            "selector_hits": len(cards), "cards": cards, "content": content}


def _dedupe_cards(cards: list[dict], domain: str) -> list[dict]:
    seen, out = set(), []
    for c in cards:
        if not c.get("title") or not c.get("href"):
            continue
        key = c["href"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": c["title"], "url": key, "domain": domain})
    return out


async def run_l1_async(city: str, city_slug: str, l1_queries: list[dict],
                       db: sqlite3.Connection) -> dict:
    from playwright.async_api import async_playwright
    from src.discovery import CandidateQueue

    queue = CandidateQueue(db)
    executed = errors = suspicious = new_total = 0
    diag = []

    ym = YANDEX_MAPS_REGIONS.get(city)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await (await browser.new_context(locale="ru-RU")).new_page()
        for qi, q in enumerate(l1_queries):
            rubric = q["text"].rsplit(" ", 1)[0]
            n_results = n_new = 0
            failures = []
            targets = [("gis2", "2gis.ru",
                        f"https://2gis.ru/{city_slug}/search/{rubric}", SELECTOR_2GIS)]
            if ym:
                targets.append(("yandex_maps", "maps.yandex.ru",
                                f"https://yandex.ru/maps/{ym[0]}/{ym[1]}/search/{rubric} {city}/",
                                SELECTOR_YMAPS))
            else:
                failures.append("yandex_maps: region_id города нет в YANDEX_MAPS_REGIONS — каталог пропущен")

            for src_id, domain, url, selector in targets:
                try:
                    g = await _grab(page, url, selector)
                    # Антибот-заглушки (разбор прогона 2026-08-26): Карты отдают
                    # страницу «limited» (158 байт), 2ГИС — «2gis captcha».
                    # CAPTCHA не обходится (правовой режим) — честный blocked.
                    low = g["content"][:3000].lower()
                    if (g["bytes"] < 1000 and ">limited<" in low) or "captcha" in low:
                        wall = "limited (антибот Яндекс.Карт)" if "limited" in low \
                            else "CAPTCHA (антибот 2ГИС)"
                        failures.append(f"{src_id}: заглушка {wall} — датацентровый IP "
                                        f"Actions заблокирован каталогом")
                        diag.append({"query": q["text"], "catalog": src_id, "url": url,
                                     "http_status": g["http_status"], "bytes": g["bytes"],
                                     "blocked_wall": wall,
                                     "saved_html": _save_html(
                                         city, f"{src_id}_{q['query_id']}", g["content"])})
                        await asyncio.sleep(RATE_DELAY_SEC)
                        continue
                    cards = _dedupe_cards(g["cards"], domain)
                    n_results += len(cards)
                    n_new += sum(queue.add(c, q["query_id"], src_id) for c in cards)
                    rec = {"query": q["text"], "catalog": src_id, "url": url,
                           "http_status": g["http_status"], "bytes": g["bytes"],
                           "selector": selector, "selector_hits": g["selector_hits"]}
                    # сохранить HTML: первая рубрика каждого каталога + любой ноль
                    if qi == 0 or g["selector_hits"] == 0:
                        rec["saved_html"] = _save_html(
                            city, f"{src_id}_{q['query_id']}", g["content"])
                    diag.append(rec)
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{src_id}: {type(exc).__name__}: {str(exc)[:120]}")
                    diag.append({"query": q["text"], "catalog": src_id, "url": url,
                                 "error": f"{type(exc).__name__}"})
                await asyncio.sleep(RATE_DELAY_SEC)

            executed += 1
            new_total += n_new
            if failures and n_results == 0:
                errors += 1
                status = "blocked"
            elif n_results == 0:
                suspicious += 1        # ноль без исключения — НЕ успех
                status = "suspicious_zero"
            elif n_new == 0:
                status = "0_new"
            else:
                status = "ok"
            db.execute("INSERT OR REPLACE INTO queries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (q["query_id"], q["layer"], q["template_id"], city, q["text"],
                        q["source"], datetime.datetime.now().isoformat(timespec="seconds"),
                        n_results, n_new, q.get("wordstat_freq"), status))
            db.commit()
        await browser.close()

    result = {"l1_executed": executed, "l1_errors": errors,
              "l1_suspicious_zero": suspicious, "l1_new_candidates": new_total,
              "diag": diag}
    notes = []
    if suspicious:
        notes.append(f"ПОДОЗРИТЕЛЬНЫЙ НОЛЬ: {suspicious} рубрик L1 вернули 0 карточек без "
                     f"ошибок — сырые HTML сохранены в data/l1_diag/{city}/, разобрать до "
                     f"доверия слою; полнота занижена")
    if errors:
        walls = {d["blocked_wall"] for d in diag if d.get("blocked_wall")}
        notes.append(f"рубрик с ошибками/блокировками каталогов: {errors}"
                     + (f" [{'; '.join(sorted(walls))}]" if walls else "")
                     + " — полнота занижена; антибот-заглушки НЕ обходятся "
                       "(правовой режим), нужен запуск вне датацентрового IP")
    if not ym:
        notes.append("Яндекс.Карты пропущены: нет region_id города")
    if notes:
        result["quality_note"] = "; ".join(notes)
    return result


def run_l1(city: str, l1_queries: list[dict], db: sqlite3.Connection) -> dict:
    from src.query_gen import city_code
    if os.environ.get("SKIP_L1"):
        return {"l1_executed": 0, "l1_errors": 0, "l1_suspicious_zero": 0,
                "l1_new_candidates": 0,
                "quality_note": ("L1 выполняется отдельно с локальной машины "
                                 "(python -m src.run_l1; решение заказчика 2026-08-26: "
                                 "каталоги блокируют IP Actions, CAPTCHA не обходим)")}
    try:
        return asyncio.run(run_l1_async(city, city_code(city), l1_queries, db))
    except Exception as exc:  # noqa: BLE001 — каталоги не должны ронять весь прогон
        return {"l1_executed": 0, "l1_errors": len(l1_queries), "l1_suspicious_zero": 0,
                "l1_new_candidates": 0,
                "quality_note": f"рубричный слой (L1) не выполнялся ({type(exc).__name__}) — полнота занижена"}
