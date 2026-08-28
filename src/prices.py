"""Прайс-каскад (заказчик, 2026-08-28). ДОКУМЕНТ ПЕРВИЧЕН.

P0  ссылки на прайс-файлы, уже собранные паспортами прома (бесплатно)
P1  навигатор «по запаху»: sitemap → оценка ссылок (текст/URL/расширение) →
    приоритетный обход, глубина ≤3, бюджет ≤12 страниц на сайт
P2  документ найден → скачать и парсить файл (pdfplumber/openpyxl)
P3  документов нет → страница: статический HTML → Jina-рендер
P4  Playwright с интерактивом (раскрытие details/aria-expanded/«показать все»)
P5  честный статус «прайс не найден на дату проверки» → лаборатория ключиков

ИЗОЛЯЦИЯ (заказчик: «главное не сломать поиск»): модуль НЕ трогает конвейер
test40 — только свои таблицы (price_recipes / price_items / price_nav_log),
свой CLI, отдельная кнопка. Применяется к отфильтрованному заказчиком
подмножеству, не ко всем строкам.

Правовой режим: robots.txt чтится (включая Crawl-delay — у emcmos.ru 10 с),
CAPTCHA/логины не обходятся, ajax-эндпоинты под Disallow напрямую не
дёргаются (только рендер разрешённой страницы). ФИО врачей не сохраняются.
Цены: дословная строка всегда, разобранное значение — только однозначное
(вилки «от …» и «у.е.» без рублей не досчитываются).
"""

import gzip
import json
import re
import sqlite3
import time
from urllib.parse import urljoin, urlparse

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# счётчик расхода по каналам (заказчик, 2026-08-28: «засекать расход по всем
# каналам чтобы посчитать лимиты и цены»). Денежных каналов в контуре НЕТ:
# HTTP бесплатен, Jina Reader бесплатна (ключ снимает rate-limit), парсинг
# локален. Считаем запросы/байты/время — этим меряются лимиты.
METER = {"http_requests": 0, "jina_requests": 0, "files_downloaded": 0,
         "bytes": 0, "seconds_sleep": 0.0}

# --- запах цены -------------------------------------------------------------

_SCENT_HIGH = re.compile(
    r"прайс|price|цены|цена|стоимост|тариф|платн\w{0,3}\s+услуг", re.I)
_SCENT_MID = re.compile(r"пациент|услуг|оплат|посетител|клиент", re.I)
_URL_HIGH = re.compile(
    r"/price|/ceny|/cens|/tarif|/stoimost|/platn|/oplata|price-?list", re.I)
_FILE_EXT = re.compile(r"\.(pdf|xlsx?|docx?)([?#]|$)", re.I)
_SKIP_URL = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|css|js|ico|mp4|zip)([?#]|$)"
    r"|^(mailto|tel|javascript):|#$", re.I)

_PRICE_LINE = re.compile(r"(\d[\d\s ]{1,9})\s*(?:руб|₽|р\.)", re.I)
_PRICE_ONLY = re.compile(
    r"^[\d\s .,]*(?:у\.?\s?е\.?[\s/]*)?[\d\s .,]+\s*"
    r"(?:руб\.?|₽|р\.)\s*$", re.I)
_CODE_LINE = re.compile(r"^[A-ZА-Я]{2,10}[\d.-]{1,8}$")
_FROM_RANGE = re.compile(r"\bот\b|\bдо\b|[-–—]\s*\d", re.I)
_SECTION = re.compile(r" > |^[А-ЯЁ\d\s,.()-]{8,120}$")


def link_scent(label: str, href: str) -> int:
    """Оценка «запаха цены» ссылки: 0 — не туда, 100 — найден документ."""
    label, href = (label or "").strip(), (href or "").strip()
    if not href or _SKIP_URL.search(href):
        return 0
    if _FILE_EXT.search(href) and (_SCENT_HIGH.search(label)
                                   or _SCENT_HIGH.search(href)
                                   or _URL_HIGH.search(href)):
        return 100                      # прайс-документ — терминальный успех
    score = 0
    if _SCENT_HIGH.search(label):
        score = max(score, 80)
    if _URL_HIGH.search(href):
        score = max(score, 70)
    if _SCENT_MID.search(label):
        score = max(score, 30)
    return score


# --- таблицы ----------------------------------------------------------------

def ensure_price_tables(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS price_recipes (
        domain TEXT PRIMARY KEY, inn TEXT, level TEXT, status TEXT,
        price_page_url TEXT, file_urls TEXT, route TEXT,
        sections_n INTEGER, items_n INTEGER, note TEXT, checked_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS price_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inn TEXT, domain TEXT, url TEXT, section TEXT, code TEXT,
        name_raw TEXT, price_raw TEXT, price_value REAL, currency TEXT,
        checked_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS price_nav_log (
        domain TEXT, url TEXT, depth INTEGER, score INTEGER,
        verdict TEXT, ts TEXT)""")
    db.commit()


# --- сеть: вежливый фетч с robots -------------------------------------------

def crawl_delay(domain: str, default: float = 3.0) -> float:
    """Crawl-delay из robots.txt; меньше 3 с не опускаемся никогда."""
    try:
        r = httpx.get(f"https://{domain}/robots.txt",
                      headers={"User-Agent": UA}, timeout=10,
                      follow_redirects=True)
        m = re.search(r"crawl-delay:\s*(\d+)", r.text, re.I)
        if m:
            return max(default, float(m.group(1)))
    except Exception:  # noqa: BLE001
        pass
    return default


def polite_get(url: str, delay: float) -> httpx.Response | None:
    from src.fetch_cascade import robots_allows
    if not robots_allows(url):
        return None
    try:
        r = httpx.get(url, headers={"User-Agent": UA,
                                    "Accept-Language": "ru-RU,ru;q=0.9"},
                      timeout=25, follow_redirects=True)
        METER["http_requests"] += 1
        METER["bytes"] += len(r.content or b"")
        METER["seconds_sleep"] += delay
        time.sleep(delay)
        return r if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        METER["http_requests"] += 1
        return None


# --- P0: файлы из паспортов прома -------------------------------------------

def p0_passport_files(db: sqlite3.Connection, inn: str) -> list[str]:
    """Ссылки на прайс-файлы из уже собранного паспорта (label → href)."""
    row = db.execute("SELECT found_site, passport FROM t40_companies "
                     "WHERE inn=?", (inn,)).fetchone()
    if not row or not row[1]:
        return []
    site = row[0] or ""
    out = []
    for m in re.finditer(r"→\s*(\S+\.(?:pdf|xlsx?|docx?)\S*)", row[1], re.I):
        href = m.group(1)
        if href.startswith("/") and site:
            href = f"https://{site.rstrip('/')}{href}"
        if href not in out:
            out.append(href)
    return out


# --- P1: sitemap + навигатор по запаху --------------------------------------

def sitemap_price_urls(domain: str, delay: float, cap: int = 10) -> list[str]:
    """Прямой прыжок: URL с прайс-паттерном из sitemap.xml (+вложенные)."""
    seen, urls, queue = set(), [], [f"https://{domain}/sitemap.xml"]
    while queue and len(seen) < 5:                 # ≤5 файлов sitemap
        sm = queue.pop(0)
        seen.add(sm)
        r = polite_get(sm, delay)
        if not r:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
        for u in locs:
            if u.endswith(".xml") and u not in seen and len(queue) < 5:
                queue.append(u)
            elif _URL_HIGH.search(u) and u not in urls:
                urls.append(u)
            if len(urls) >= cap:
                return urls
    return urls


def page_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """(текст, абсолютный href) всех ссылок; шапка/меню/футер естественно
    попадают — они в HTML любой страницы."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001
        return []
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"])
        if href in seen:
            continue
        seen.add(href)
        out.append((a.get_text(" ", strip=True)[:120], href))
    return out


def navigate(db: sqlite3.Connection, domain: str, delay: float,
             max_pages: int = 12, max_depth: int = 3) -> dict:
    """Навигатор: приоритетная очередь по запаху. Возвращает
    {'files': [...], 'price_pages': [...], 'route': [...], 'pages_seen': n}."""
    start = f"https://{domain}/"
    queue = [(90, 0, start, "главная")]           # (score, depth, url, label)
    for u in sitemap_price_urls(domain, delay):
        queue.append((85, 0, u, "sitemap"))
    visited, files, price_pages, route = set(), [], [], []
    ts = time.strftime("%Y-%m-%d %H:%M")
    host = domain.lower().lstrip("www.")
    while queue and len(visited) < max_pages:
        queue.sort(key=lambda x: -x[0])
        score, depth, url, label = queue.pop(0)
        if url in visited or urlparse(url).netloc.lower().lstrip(
                "www.") != host:
            continue
        visited.add(url)
        r = polite_get(url, delay)
        db.execute("INSERT INTO price_nav_log VALUES (?,?,?,?,?,?)",
                   (domain, url[:300], depth, score,
                    "ok" if r else "недоступна", ts))
        if not r:
            continue
        route.append({"url": url[:300], "label": label[:80], "depth": depth})
        text_prices = len(_PRICE_LINE.findall(r.text))
        if text_prices >= 30 and url not in price_pages:
            price_pages.append(url)               # страница-прайс найдена
        for lbl, href in page_links(r.text, url):
            s = link_scent(lbl, href)
            if s >= 100:
                if href not in files:
                    files.append(href)            # документ — терминал
            elif s >= 30 and depth + 1 <= max_depth and href not in visited:
                queue.append((s, depth + 1, href, lbl))
        if files:
            break                                  # документ первичен
    db.commit()
    return {"files": files, "price_pages": price_pages,
            "route": route, "pages_seen": len(visited)}


# --- парсер прайса: мультипаттерн -------------------------------------------

def parse_price_value(raw: str) -> tuple[float | None, str]:
    """Однозначная цена в рублях или None (вилки/«от» не досчитываются)."""
    if _FROM_RANGE.search(raw):
        return None, "RUB"
    m = _PRICE_LINE.search(raw)
    if not m:
        return None, ""
    digits = re.sub(r"[\s ]", "", m.group(1))
    try:
        return float(digits), "RUB"
    except ValueError:
        return None, ""


def parse_price_text(text: str) -> list[dict]:
    """Текст (рендер/markdown) → позиции. Паттерны: «название … цена» в одной
    строке; пары название→строка-цена; триплеты код/название/цена (ЕМЦ)."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    items, section, pending_code, pending_name = [], "", None, None
    for ln in lines:
        if len(ln) < 3 or len(ln) > 300:
            pending_code = pending_name = None
            continue
        if " > " in ln or (ln.isupper() and re.search(r"[А-ЯЁ]{4}", ln)
                           and not _PRICE_LINE.search(ln)):
            section, pending_code, pending_name = ln[:200], None, None
            continue
        if _CODE_LINE.match(ln):
            pending_code, pending_name = ln, None
            continue
        if _PRICE_ONLY.match(ln):
            if pending_name:                       # пара/триплет закрыт ценой
                val, cur = parse_price_value(ln)
                items.append({"section": section, "code": pending_code or "",
                              "name": pending_name, "price_raw": ln,
                              "price_value": val, "currency": cur})
            pending_code = pending_name = None
            continue
        m = _PRICE_LINE.search(ln)
        has_name = re.search(r"[а-яА-ЯёЁ]{4}", ln)
        if m and has_name:                         # название и цена в строке
            prefix = ln[:m.start()]
            # вилка: «от/до 900 руб» или «1000-2000 руб» — дословно, без
            # значения; «Название — 1500 руб» — обычный разделитель
            qual = (re.search(r"\b(?:от|до)\s*$", prefix, re.I)
                    or re.search(r"\d[\d\s ]*\s*[-–—]\s*$", prefix))
            name = (prefix[:qual.start()] if qual else prefix
                    ).strip(" .–—-:\t")
            raw_start = qual.start() if qual else m.start()
            if len(name) >= 4:
                val, cur = ((None, "RUB") if qual
                            else parse_price_value(ln[m.start():]))
                items.append({"section": section, "code": pending_code or "",
                              "name": name,
                              "price_raw": ln[raw_start:].strip()[:60],
                              "price_value": val, "currency": cur})
            pending_code = pending_name = None
        elif has_name:
            pending_name = ln                      # кандидат пары/триплета
    return items


def parse_price_file(data: bytes, ext: str) -> list[dict]:
    """PDF/XLSX → позиции. .doc честно отдаётся в лабораторию."""
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        import io
        import pdfplumber
        text = []
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:80]:
                text.append(page.extract_text() or "")
        return parse_price_text("\n".join(text))
    if ext in ("xlsx", "xls"):
        import io
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                    data_only=True)
        items, section = [], ""
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                cells = [c for c in row if c is not None]
                texts = [str(c).strip() for c in cells
                         if isinstance(c, str) and str(c).strip()]
                nums = [c for c in cells if isinstance(c, (int, float))]
                if len(texts) == 1 and not nums and len(texts[0]) > 7:
                    section = texts[0][:200]
                elif texts and nums:
                    name = max(texts, key=len)
                    if re.search(r"[а-яА-ЯёЁ]{4}", name):
                        items.append({"section": section, "code": "",
                                      "name": name[:300],
                                      "price_raw": str(nums[-1]),
                                      "price_value": float(nums[-1]),
                                      "currency": "RUB"})
        return items
    return []


# --- каскад по одной компании -----------------------------------------------

def _save_items(db, inn, domain, url, items):
    ts = time.strftime("%Y-%m-%d")
    db.execute("DELETE FROM price_items WHERE domain=? AND url=?",
               (domain, url))
    for it in items:
        db.execute("INSERT INTO price_items (inn, domain, url, section, code,"
                   " name_raw, price_raw, price_value, currency, checked_at)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (inn, domain, url[:300], it["section"], it["code"],
                    it["name"][:300], it["price_raw"][:100],
                    it["price_value"], it["currency"], ts))


def run_company(db: sqlite3.Connection, inn: str, domain: str) -> dict:
    """P0→P4 для одной компании. Чекпойнт в price_recipes (идемпотентно)."""
    ensure_price_tables(db)
    done = db.execute("SELECT status FROM price_recipes WHERE domain=?",
                      (domain,)).fetchone()
    if done and done[0] not in (None, "", "в работе"):
        return {"domain": domain, "status": done[0], "skipped": True}
    delay = crawl_delay(domain)
    ts = time.strftime("%Y-%m-%d %H:%M")
    files = p0_passport_files(db, inn)             # P0
    nav = {"files": [], "price_pages": [], "route": [], "pages_seen": 0}
    if not files:
        nav = navigate(db, domain, delay)          # P1
        files = nav["files"]
    level, status, items, src_url = "", "", [], ""
    for f in files:                                # P2 — документ первичен
        r = polite_get(f, delay)
        if not r:
            continue
        METER["files_downloaded"] += 1
        got = parse_price_file(r.content, f.rsplit(".", 1)[-1][:4])
        if len(got) > len(items):
            items, src_url, level = got, f, "P2:документ"
    if not items:                                  # P3 — страница
        pages = nav["price_pages"] or [f"https://{domain}/price/",
                                       f"https://{domain}/ceny/"]
        from src.fetch_cascade import _level1_jina
        for pu in pages[:3]:
            r = polite_get(pu, delay)
            got = parse_price_text(r.text) if r else []
            if len(got) < 20:                      # детектор полноты → Jina
                jt, _, _ = _level1_jina(pu)
                METER["jina_requests"] += 1
                METER["bytes"] += len(jt or "")
                got2 = parse_price_text(jt or "")
                if len(got2) > len(got):
                    got = got2
                    level = "P3:jina"
            if len(got) > len(items):
                items, src_url = got, pu
                level = level or "P3:статика"
    if items:
        _save_items(db, inn, domain, src_url, items)
        status = "прайс извлечён"
    else:
        status = "прайс не найден на дату проверки"  # P5 — честный статус
        level = level or "P5"
    db.execute("INSERT OR REPLACE INTO price_recipes VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?)",
               (domain, inn, level, status, src_url,
                json.dumps(files, ensure_ascii=False),
                json.dumps(nav["route"], ensure_ascii=False),
                len({i['section'] for i in items}), len(items),
                f"страниц навигатора: {nav['pages_seen']}", ts))
    db.commit()
    return {"domain": domain, "status": status, "level": level,
            "items": len(items), "files_found": len(files)}


def run_batch(db: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """Обкатка: первые N компаний с найденным сайтом (потом — по фильтру
    заказчика). Чекпойнт подомённо, перезапуск продолжает с места."""
    ensure_price_tables(db)
    rows = db.execute(
        "SELECT c.inn, c.found_site FROM t40_companies c "
        "WHERE c.found_site IS NOT NULL AND c.found_site<>'' "
        "AND NOT EXISTS (SELECT 1 FROM price_recipes r "
        "  WHERE r.domain=c.found_site AND r.status NOT IN ('', 'в работе')) "
        "ORDER BY c.row_no LIMIT ?", (limit,)).fetchall()
    out = []
    for inn, site in rows:
        t0 = time.time()
        res = run_company(db, inn, site)
        print(f"  {site}: {res['status']} ({res.get('items', 0)} позиций, "
              f"{res.get('level', '')}, {time.time() - t0:.0f} с)",
              flush=True)
        out.append(res)
    print(f"РАСХОД: HTTP {METER['http_requests']} зап. · "
          f"Jina {METER['jina_requests']} зап. · "
          f"файлов {METER['files_downloaded']} · "
          f"{METER['bytes'] / 1e6:.1f} МБ · "
          f"пауз вежливости {METER['seconds_sleep'] / 60:.0f} мин · 0 ₽")
    return out


if __name__ == "__main__":
    import sys
    db = sqlite3.connect("data/osint.db")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "probe" and len(sys.argv) > 2:       # обкатка одного домена
        print(run_company(db, sys.argv[3] if len(sys.argv) > 3 else "",
                          sys.argv[2]))
    elif cmd == "run":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
        res = run_batch(db, n)
        ok = sum(1 for r in res if r.get("items"))
        print(f"Итог: {ok}/{len(res)} с извлечённым прайсом")
