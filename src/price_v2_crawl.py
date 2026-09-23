"""Пересбор прайсов, этап 1: параллельный краулер HTML (заказчик, 2026-09-24).

Причина пересбора: 60% доменов были собраны через текстовую выжимку без
DOM-структуры, отсюда мусорные позиции («Записаться, 700 ₽»), склейки и
недоборы. Новый конвейер работает с сырым HTML.

Что делает: для КАЖДОГО домена прайс-контура (все домены из price_items и
price_recipes — вся база ОСИНТ, включая прежние P5 «не извлечено»):
  · seed = главная + все известные прайс-URL (страницы price_items,
    price_page_url и маршруты из price_recipes);
  · обход по ссылкам с прайс-запахом и внутри прайс-ветки, глубина ≤2 от
    seed, бюджет HTML-страниц на домен + бюджет файлов (PDF/XLSX);
  · HTML сохраняется в кэш data/price_html_cache/{domain}/{sha1}.gz
    (в .gitignore), мета — в data/price_v2.db (pages_v2).

Правовой режим: robots.txt соблюдается (urllib.robotparser + Crawl-delay),
пауза между запросами к одному домену ≥1.0 с, CAPTCHA/логины не обходятся.
Шардирование: список доменов режется на N частей, каждый шард — свой
процесс (python -m src.price_v2_crawl shard_file), пишут в одну БД (WAL).
"""

import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CACHE = "data/price_html_cache"
DB = "data/price_v2.db"
PAGE_BUDGET = 45
FILE_BUDGET = 10
DELAY = 1.0

PRICE_HINT = re.compile(
    r"прайс|price|цен|ceny|стоимост|тариф|pra[ij]s|tarif|stoimost|услуг|uslug",
    re.I)
FILE_EXT = re.compile(r"\.(pdf|xlsx?|docx?)($|\?)", re.I)
SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|ico|css|js|mp4|avi|zip|rar|woff2?|ttf)($|\?)", re.I)

_db_lock = threading.Lock()


def db():
    con = sqlite3.connect(DB, timeout=60)
    con.execute("pragma journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS pages_v2(
        domain TEXT, url TEXT PRIMARY KEY, kind TEXT, status INTEGER,
        bytes INTEGER, sha1 TEXT, depth INTEGER, fetched_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS crawl_v2(
        domain TEXT PRIMARY KEY, inn TEXT, pages INTEGER, files INTEGER,
        status TEXT, note TEXT, finished_at TEXT)""")
    return con


def seeds():
    p = sqlite3.connect("file:data/prices.db?mode=ro", uri=True)
    doms = {}
    for d, inn in p.execute("select distinct domain, inn from price_items"):
        doms.setdefault(d, {"inn": inn, "urls": set()})
    for d, inn, ppu, route in p.execute(
            "select domain, inn, price_page_url, route from price_recipes"):
        doms.setdefault(d, {"inn": inn, "urls": set()})
        if ppu:
            doms[d]["urls"].add(ppu)
        try:
            for step in json.loads(route or "[]"):
                u = step.get("url")
                if u and PRICE_HINT.search(u):
                    doms[d]["urls"].add(u)
        except Exception:
            pass
    for d, u in p.execute("select distinct domain, url from price_items"):
        if u:
            doms[d]["urls"].add(u)
    for d, v in doms.items():
        v["urls"].add(f"https://{d}/")
    return doms


def fetch(client, url):
    try:
        r = client.get(url, timeout=25)
        return r
    except Exception:
        return None


def crawl_domain(domain, inn, seed_urls):
    con = db()
    os.makedirs(f"{CACHE}/{domain}", exist_ok=True)
    rp = urllib.robotparser.RobotFileParser()
    delay = DELAY
    try:
        rp.set_url(f"https://{domain}/robots.txt")
        rp.read()
        cd = rp.crawl_delay(UA) or rp.crawl_delay("*")
        if cd:
            delay = max(delay, min(float(cd), 10.0))
    except Exception:
        rp = None

    def allowed(u):
        if rp is None:
            return True
        try:
            return rp.can_fetch("*", u)
        except Exception:
            return True

    seen, queue = set(), []
    for u in sorted(seed_urls):
        queue.append((u, 0))
    pages = files = 0
    price_paths = {urlparse(u).path.rstrip("/") for u in seed_urls
                   if PRICE_HINT.search(u)}
    with httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                      verify=False) as client:
        while queue and pages < PAGE_BUDGET:
            url, depth = queue.pop(0)
            key = url.split("#")[0].rstrip("/")
            if key in seen or not allowed(url):
                continue
            seen.add(key)
            if SKIP_EXT.search(url):
                continue
            is_file = bool(FILE_EXT.search(url))
            if is_file and files >= FILE_BUDGET:
                continue
            r = fetch(client, url)
            time.sleep(delay)
            if r is None:
                continue
            body = r.content or b""
            sha = hashlib.sha1(body).hexdigest()
            if r.status_code == 200 and body:
                with gzip.open(f"{CACHE}/{domain}/{sha}.gz", "wb") as f:
                    f.write(body)
            with _db_lock:
                con.execute("INSERT OR REPLACE INTO pages_v2 VALUES (?,?,?,?,?,?,?,datetime('now'))",
                            (domain, url, "file" if is_file else "html",
                             r.status_code, len(body), sha, depth))
                con.commit()
            if is_file:
                files += 1
                continue
            pages += 1
            if depth >= 2 or r.status_code != 200:
                continue
            try:
                html = body.decode(r.encoding or "utf-8", "ignore")
            except Exception:
                continue
            for m in re.finditer(r'<a[^>]+href=["\']([^"\'#]+)["\'][^>]*>(.{0,120}?)</a>',
                                 html, re.I | re.S):
                href, text = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
                absu = urljoin(url, href)
                pu = urlparse(absu)
                if pu.netloc.replace("www.", "") != domain.replace("www.", ""):
                    continue
                in_branch = any(pp and pu.path.startswith(pp + "/")
                                for pp in price_paths)
                if FILE_EXT.search(absu) and PRICE_HINT.search(absu + " " + text):
                    queue.append((absu, depth + 1))
                elif PRICE_HINT.search(pu.path + " " + text) or in_branch:
                    queue.append((absu, depth + 1))
    with _db_lock:
        con.execute("INSERT OR REPLACE INTO crawl_v2 VALUES (?,?,?,?,?,?,datetime('now'))",
                    (domain, inn, pages, files,
                     "ok" if pages or files else "пусто", "", ))
        con.commit()
    con.close()
    return domain, pages, files


def main(shard_file, workers=12):
    doms = seeds()
    todo = [l.strip() for l in open(shard_file, encoding="utf-8") if l.strip()]
    con = db()
    done = {r[0] for r in con.execute("select domain from crawl_v2")}
    con.close()
    todo = [d for d in todo if d in doms and d not in done]
    print(f"шард {os.path.basename(shard_file)}: доменов {len(todo)}", flush=True)
    n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(crawl_domain, d, doms[d]["inn"], doms[d]["urls"])
                for d in todo]
        for f in futs:
            try:
                d, p, fl = f.result()
                n += 1
                if n % 10 == 0:
                    print(f"  {n}/{len(todo)}", flush=True)
            except Exception as e:                       # noqa: BLE001
                print("  ошибка:", type(e).__name__, e, flush=True)
    print("шард готов:", shard_file, flush=True)


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 12)
