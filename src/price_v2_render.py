"""Пересбор прайсов, волна 2: Playwright-рендер для SPA-доменов.

Применяется ТОЛЬКО к доменам, не прошедшим ворота качества после статики
(extract_v2.gate_ok=0): страницы с прайс-запахом рендерятся Chromium'ом
(networkidle + прокрутка + раскрытие details/aria-expanded), итоговый DOM
кладётся в тот же кэш (kind='html-js'), после чего домен пере-извлекается
тем же DOM-экстрактором. Правовой режим прежний: robots.txt уже учтён на
этапе краула (рендерим только уже разрешённые URL), пауза между страницами
домена ≥1 с, CAPTCHA не обходится.
"""

import asyncio
import gzip
import hashlib
import re
import sqlite3
import sys
import time

from playwright.async_api import async_playwright

DB = "data/price_v2.db"
CACHE = "data/price_html_cache"
PRICE_HINT = re.compile(r"прайс|price|цен|ceny|стоимост|тариф|uslug|услуг", re.I)
PER_DOMAIN = 8

CLICK_JS = """
async () => {
  for (const el of document.querySelectorAll('details:not([open])')) el.open = true;
  const btns = document.querySelectorAll(
    '[aria-expanded="false"], .accordion__header, .accordion-title, .spoiler__title,'
    + ' [class*="accordion"] [class*="head"], [class*="tab"][role="tab"]');
  let n = 0;
  for (const b of btns) { try { b.click(); n++; } catch(e){} if (n > 120) break; }
  window.scrollTo(0, document.body.scrollHeight);
  return n;
}
"""


async def render_domain(browser, domain, urls, con):
    ctx = await browser.new_context(viewport={"width": 1440, "height": 2200},
                                    locale="ru-RU", ignore_https_errors=True)
    page = await ctx.new_page()
    done = 0
    for url in urls[:PER_DOMAIN]:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=18000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            try:
                await page.evaluate(CLICK_JS)
                await page.wait_for_timeout(700)
            except Exception:
                pass
            html = await page.content()
            body = html.encode("utf-8", "ignore")
            sha = hashlib.sha1(body).hexdigest()
            with gzip.open(f"{CACHE}/{domain}/{sha}.gz", "wb") as f:
                f.write(body)
            con.execute("INSERT OR REPLACE INTO pages_v2 VALUES (?,?,?,?,?,?,?,datetime('now'))",
                        (domain, url + "#js", "html", 200, len(body), sha, 0))
            con.commit()
            done += 1
        except Exception:
            continue
        await asyncio.sleep(0.5)
    await ctx.close()
    return done


async def main(shard_file, workers=5):
    con = sqlite3.connect(DB, timeout=60)
    con.execute("pragma journal_mode=WAL")
    fail = {r[0] for r in con.execute("select domain from extract_v2 where gate_ok=0")}
    hasjs = {r[0] for r in con.execute("select distinct domain from pages_v2 where url like '%#js'")}
    fail -= hasjs
    todo = [l.strip() for l in open(shard_file, encoding="utf-8")
            if l.strip() in fail]
    urls_by = {}
    for d in todo:
        rows = [r[0] for r in con.execute(
            "select url from pages_v2 where domain=? and kind='html' and status=200 "
            "and url not like '%#js'", (d,))]
        pri = [u for u in rows if PRICE_HINT.search(u)] or rows
        urls_by[d] = pri
    print(f"рендер {shard_file}: доменов {len(todo)}", flush=True)
    async with async_playwright() as pw:
        import os
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        browser = await pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium",
            proxy={"server": proxy} if proxy else None,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--ignore-certificate-errors"])
        sem = asyncio.Semaphore(workers)

        async def one(d):
            async with sem:
                try:
                    return await render_domain(browser, d, urls_by[d], con)
                except Exception as e:                    # noqa: BLE001
                    print(d, "рендер-ошибка:", type(e).__name__, flush=True)
                    return 0
        k = 0
        for f in asyncio.as_completed([one(d) for d in todo]):
            await f
            k += 1
            if k % 20 == 0:
                print(f"  {k}/{len(todo)}", flush=True)
        await browser.close()
    print("рендер готов:", shard_file, flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5))
