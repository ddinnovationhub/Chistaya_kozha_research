"""Прайс-каскад (заказчик, 2026-08-28). ДОКУМЕНТ ПЕРВИЧЕН.

P0  ссылки на прайс-файлы, уже собранные паспортами прома (бесплатно)
P1  навигатор «по запаху»: sitemap → оценка ссылок (текст/URL/расширение) →
    приоритетный обход, глубина ≤3, бюджет ≤12 страниц на сайт + ОТДЕЛЬНЫЙ
    бюджет прайс-ветки (до 45 страниц одного меню, глубина +3)
P2  документ найден → скачать и парсить файл (pdfplumber/openpyxl); файлов
    несколько → позиции СУММИРУЮТСЯ с дедупликацией, а не берётся один
P3  документов нет → страницы ветки: статика → Jina, сумма по всей ветке
P4  Playwright с интерактивом (раскрытие details/aria-expanded/«показать
    все»); идёт по «сухим» страницам ветки и добирает меню, нарисованное
    скриптом
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
import os
import re
import sqlite3
import time
import zlib
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
_PRICE_URL_HINT = re.compile(
    r"прайс|price|цен|ceny|стоимост|тариф|pra[ij]s|tarif|stoimost", re.I)
# РАЗВЕТВЛЁННЫЙ ПРАЙС (заказчик, 2026-09-07: «на сайтах, на которые я
# опирался, прайсы разветвлённые. Одно меню, и чтобы скачать всё —
# необходимо походить по страницам»). Каждая категория услуг — своя
# страница одного меню; общего бюджета обхода (12 страниц на весь сайт)
# на такое меню не хватало, и в прайс попадала одна категория из тридцати.
# Прайс-ветка получает СВОЙ бюджет, отдельный от общего обхода.
PRICE_BRANCH_PAGES = 45      # страниц ветки прайса на домен (сверх общих 12)
PRICE_PAGES_CAP = 40         # сколько страниц ветки суммирует P3
P4_PAGES_CAP = 12            # сколько «сухих» страниц ветки рендерит браузер

_URL_HIGH = re.compile(
    r"/price|/ceny|/cens|/pra[ij]s|/tarif|/stoimost|/platn|/oplata|price-?list", re.I)
_FILE_EXT = re.compile(r"\.(pdf|xlsx?|docx?)([?#]|$)", re.I)
_SKIP_URL = re.compile(
    r"\.(jpe?g|png|gif|svg|webp|css|js|ico|mp4|zip)([?#]|$)"
    r"|^(mailto|tel|javascript):|#$", re.I)

# Число цены (разбор 2026-09-04, заказчик: «скорректировать выборку»).
# Прежний шаблон (\d[\d\s ]{1,9}) не знал ни копеек, ни точки-разделителя
# тысяч: «32040.00 ₽» захватывалось как «00» → 0 ₽ (10 368 позиций),
# «2.500 ₽» → 500 ₽, а склейка двух цен «2 020 30 500 ₽» → 2 030 500 ₽.
# Теперь: либо число с ГРУППАМИ РОВНО ПО ТРИ цифры (разделитель — пробел,
# неразрывный пробел, точка или запятая), либо сплошные цифры; в обоих
# случаях необязательный хвост копеек. Нормализация — в parse_price_value.
_PRICE_NUM = (r"\d{1,3}(?:[\s  .,]\d{3})+(?:[.,]\d{1,2})?"
              r"|\d{1,7}(?:[.,]\d{1,2})?")
_PRICE_LINE = re.compile(rf"({_PRICE_NUM})\s*(?:руб|₽|р\.)", re.I)
_PRICE_ONLY = re.compile(
    r"^(?:от|до)?\s*[\d\s .,]*(?:у\.?\s?е\.?[\s/]*)?[\d\s .,]+\s*"
    r"(?:руб\.?|₽|р\.)\s*$", re.I)
_CODE_LINE = re.compile(r"^[A-ZА-Я]{2,10}[\d.-]{1,8}$")
_FROM_RANGE = re.compile(r"\bот\b|\bдо\b|[-–—]\s*\d", re.I)
_SECTION = re.compile(r" > |^[А-ЯЁ\d\s,.()-]{8,120}$")
# Строка-АДРЕС ФИЛИАЛА, а не название услуги (разбор 2026-09-04, nika-nn.ru:
# блок «Цены по филиалам» — название услуги стоит выше, а дальше идут пары
# «адрес → цена»; парсер писал адрес в название, 17 639 позиций из 18 955).
_ADDR_LINE = re.compile(
    r"^(?:г\.|гор\.|город|пос\.|пгт|с\.|дер\.|мкр)\s|"
    r"\b(?:ул|пр-?кт|пр-т|просп|пер|ш|шоссе|б-р|бульвар|наб|пл|мкр)\.?\s"
    r"[А-ЯЁ]|,\s*д\.\s*\d", re.I)
# служебные подписи интерфейса — не название услуги
_UI_NOISE = re.compile(
    r"^(?:цены?\s+по\s+филиал|записаться|подробнее|в\s+корзину|выбрать"
    r"|заказать|показать|смотреть|все\s+цены|стоимость услуг)", re.I)


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

# ── ДВЕ БАЗЫ (заказчик, 2026-09-02: «было требование запускать параллельно;
# работа одного блокирует работу другого»). Прайсы живут в СВОЕЙ базе
# data/prices.db; из data/osint.db они только ЧИТАЮТ список компаний
# (присоединена read-only как схема «o»). Два конвейера пишут разные файлы —
# гит-конфликтов нет, test-40 и prices идут параллельно. ─────────────────
T40 = "t40_companies"          # в проде переопределяется на "o.t40_companies"
RZN = "rzn_licenses"           # и "o.rzn_licenses" (см. open_dbs)
PRICES_DB = "data/prices.db"
OSINT_DB = "data/osint.db"


def open_dbs(prices_path: str = PRICES_DB, osint_path: str = OSINT_DB,
             clean_orphans: bool = True) -> sqlite3.Connection:
    """Главная база — прайсы (запись); osint.db присоединена только на чтение.
    Разовая синхронизация: записи price_*, оставшиеся в osint.db от обкатки
    28.08 и от прогона test-40 со встроенным шагом прайсов, переносятся
    (по домену, идемпотентно) — работа не теряется и не повторяется."""
    global T40, RZN
    # uri=True: иначе «file:…?mode=ro» в ATTACH прочтётся как имя файла и
    # SQLite молча создаст пустую базу с таким именем
    db = sqlite3.connect(prices_path, uri=True)
    db.execute("PRAGMA busy_timeout=30000")
    # WAL НЕ включаем намеренно: воркфлоу коммитит один файл data/prices.db,
    # а WAL держит свежие транзакции в отдельном -wal — незачекпойнченная
    # работа потерялась бы при коммите. Блокировки лечатся короткими
    # транзакциями (журнал навигатора коммитится сразу, позиции пишутся
    # одной пачкой), а не сменой журнального режима.
    ensure_price_tables(db)
    db.execute("ATTACH DATABASE ? AS o", (f"file:{osint_path}?mode=ro",))
    T40, RZN = "o.t40_companies", "o.rzn_licenses"
    has = {r[0] for r in db.execute(
        "SELECT name FROM o.sqlite_master WHERE type='table'")}
    if "price_recipes" in has:
        db.execute("INSERT OR IGNORE INTO price_recipes SELECT * FROM o.price_recipes")
        if "price_items" in has:
            db.execute(
                "INSERT INTO price_items (inn, domain, url, section, code, name_raw, "
                "price_raw, price_value, currency, checked_at) "
                "SELECT inn, domain, url, section, code, name_raw, price_raw, "
                "price_value, currency, checked_at FROM o.price_items "
                "WHERE domain NOT IN (SELECT DISTINCT domain FROM price_items)")
        if "price_nav_log" in has:
            db.execute("INSERT INTO price_nav_log SELECT * FROM o.price_nav_log "
                       "WHERE domain NOT IN (SELECT DISTINCT domain FROM price_nav_log)")
        db.commit()
    # ЧИСТКА СИРОТ (заказчик, 2026-09-03): рецепт привязан к паре
    # (ИНН, домен=found_site на момент разбора). Если сайт у ИНН позже
    # сброшен лестницей/чёрным списком — привязка недоказана, прайс чужого
    # домена не должен числиться за компанией. Удаление снимает чекпойнт:
    # при новом подтверждённом сайте домен разберётся заново
    orphan_pairs = [] if not clean_orphans else db.execute(
        "SELECT r.domain, r.inn FROM price_recipes r WHERE NOT EXISTS "
        "(SELECT 1 FROM o.t40_companies c WHERE c.inn=r.inn "
        " AND c.found_site=r.domain)").fetchall()
    if orphan_pairs:
        for dom, inn in orphan_pairs:
            db.execute("DELETE FROM price_items WHERE domain=? AND inn=?", (dom, inn))
            db.execute("DELETE FROM price_recipes WHERE domain=? AND inn=?", (dom, inn))
            db.execute("DELETE FROM price_nav_log WHERE domain=?", (dom,))
        db.commit()
        print(f"ℹ прайсы: удалено {len(orphan_pairs)} рецептов-сирот "
              f"(сайт у ИНН сброшен — привязка недоказана)")
    return db


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
    # журнал перепрогона: чем закончился ПРЕЖНИЙ разбор домена. Нужен,
    # чтобы повторный проход не ухудшал уже собранное (сайт мог лечь,
    # прайс — переехать): результат хуже прежнего не записывается.
    db.execute("""CREATE TABLE IF NOT EXISTS price_rerun (
        domain TEXT PRIMARY KEY, prev_status TEXT, prev_level TEXT,
        prev_items INTEGER, prev_page TEXT, ts TEXT)""")
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


def browser_render(url: str, delay: float, timeout_ms: int = 45000) -> str:
    """P4 — БРАУЗЕРНЫЙ РЕНДЕР прайс-страницы. Возвращает HTML после
    выполнения скриптов или пустую строку.

    ЗАЧЕМ (заказчик, 2026-09-07, на примерах 5pmedicina.ru и agk24.ru: «у
    обоих есть прайсы»). Уровень P4 был описан в дизайне каскада и в шапке
    этого модуля, но в КОДЕ отсутствовал: после статики и Jina сразу
    ставился статус «прайс не найден». У сайтов, где цены рисует
    JavaScript, в сыром HTML нет ни одной цены — ни в тексте, ни в
    разметке, — поэтому парсер честно возвращал ноль, а конвейер называл
    это «прайса нет». Разбор базы: навигатор ОТКРЫЛ прайс-страницу у 225
    доменов из 536 «неудачных», то есть у 42% статус был неверен.

    Правовой режим тот же, что у остальных уровней: robots.txt чтится,
    пауза домена соблюдается, CAPTCHA и логины не обходятся."""
    from src.fetch_cascade import robots_allows
    if not robots_allows(url):
        return ""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("    ⚠ P4: playwright не установлен — уровень пропущен")
        return ""
    # прокси окружения (песочница агента гоняет весь HTTPS через прокси;
    # в Actions переменная не задана и ветка не работает). Без этого
    # chromium получает ERR_CONNECTION_RESET и уровень P4 не проверить
    import os
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    html = ""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                proxy={"server": proxy} if proxy else None)
            page = browser.new_page(
                viewport={"width": 1400, "height": 1000},
                user_agent=UA, locale="ru-RU",
                # сертификат подменяет сам прокси песочницы — проверять
                # его нечем; в Actions прокси нет и проверка обычная
                ignore_https_errors=bool(proxy))
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_timeout(1500)
                # прайсы часто свёрнуты в аккордеоны и вкладки: раскрываем
                for sel in ("details", "[aria-expanded='false']",
                            ".accordion__title", ".accordion-title",
                            ".t668__title", ".tabs__title", ".price__toggle"):
                    try:
                        for el in page.query_selector_all(sel)[:60]:
                            try:
                                el.click(timeout=700)
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001
                        pass
                # ленивая подгрузка длинных прайсов — прокрутка до низа
                for _ in range(6):
                    page.mouse.wheel(0, 20000)
                    page.wait_for_timeout(700)
                html = page.content()
            finally:
                browser.close()
        METER["http_requests"] += 1
        METER["bytes"] += len(html)
    except Exception as e:  # noqa: BLE001 — уровень не валит домен
        print(f"    ⚠ P4 {url[:60]}: {type(e).__name__} "
              f"{str(e).splitlines()[0][:90]}")
    METER["seconds_sleep"] += delay
    time.sleep(delay)
    return html


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
    row = db.execute(f"SELECT found_site, passport FROM {T40} "
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


def html_to_text(html: str) -> str:
    """Видимый текст страницы построчно. Парсер работает ТОЛЬКО по нему:
    в сыром HTML название и цена разделены тегами (дефект обкатки №1 —
    навигатор находил /price, а парсер брал 0-1 позицию из исходника)."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        return soup.get_text("\n")
    except Exception:  # noqa: BLE001
        return html


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
        href = urljoin(base_url, a["href"]).split("#", 1)[0]
        if not href or href in seen:
            continue
        seen.add(href)
        out.append((a.get_text(" ", strip=True)[:120], href))
    return out


def _same_host(domain: str, href: str) -> bool:
    host = re.sub(r"^www\.", "", domain.lower())
    return re.sub(r"^www\.", "", urlparse(href).netloc.lower()) == host


def _branch_child(base_url: str, href: str) -> bool:
    """href — ребёнок или сосед base_url по ОДНОМУ разделу меню.

    /price/ → /price/dermatologiya/ (ребёнок);
    /price/dermatologiya/ → /price/kosmetologiya/ (сосед по меню);
    /price/ → /uslugi/ — нет, это другой раздел."""
    b = [x for x in urlparse(base_url).path.split("/") if x]
    h = [x for x in urlparse(href).path.split("/") if x]
    if not b or not h:
        return False
    parent = b[:-1] if len(b) > 1 else b
    return len(h) > len(parent) and h[:len(parent)] == parent


def navigate(db: sqlite3.Connection, domain: str, delay: float,
             max_pages: int = 12, max_depth: int = 3,
             price_max_pages: int = PRICE_BRANCH_PAGES) -> dict:
    """Навигатор: приоритетная очередь по запаху. Возвращает
    {'files': [...], 'price_pages': [...], 'route': [...], 'pages_seen': n}.

    ДВА БЮДЖЕТА (заказчик, 2026-09-07). Общий обход ищет вход в прайс и
    ограничен max_pages — иначе навигатор гуляет по новостям и врачам.
    Как только страница опознана прайсовой, её дети и соседи по разделу
    («одно меню») переходят в ПРАЙС-ВЕТКУ со своим бюджетом
    price_max_pages и увеличенной глубиной: у разветвлённого прайса
    категорий бывает три десятка, и обрезать их общим бюджетом — значит
    записать в таблицу одну категорию из тридцати и назвать это прайсом."""
    start = f"https://{domain}/"
    # (score, depth, url, label, ветка_прайса)
    queue = [(90, 0, start, "главная", False)]
    for u in sitemap_price_urls(domain, delay):
        queue.append((85, 0, u, "sitemap", True))
    visited, files, price_pages, route = set(), [], [], []
    dry_branch = []          # страницы ветки, открывшиеся БЕЗ цен в статике:
    general_seen = branch_seen = 0    # кандидаты на браузерный рендер (P4)
    ts = time.strftime("%Y-%m-%d %H:%M")
    while queue:
        queue.sort(key=lambda x: -x[0])
        pick = None                    # берём лучший из тех, чей бюджет цел
        for idx, item in enumerate(queue):
            if item[4]:
                if branch_seen < price_max_pages:
                    pick = queue.pop(idx)
                    break
            elif general_seen < max_pages:
                pick = queue.pop(idx)
                break
        if pick is None:               # оба бюджета исчерпаны
            break
        score, depth, url, label, branch = pick
        if url in visited or not _same_host(domain, url):
            continue
        visited.add(url)
        if branch:
            branch_seen += 1
        else:
            general_seen += 1
        r = polite_get(url, delay)
        db.execute("INSERT INTO price_nav_log VALUES (?,?,?,?,?,?)",
                   (domain, url[:300], depth, score,
                    "ok" if r else "недоступна", ts))
        db.commit()      # короткая транзакция: иначе запись базы держится
                         # весь обход (до 12 страниц с паузами), и соседние
                         # потоки срываются с «database is locked»
        if not r:
            continue
        route.append({"url": url[:300], "label": label[:80], "depth": depth})
        text_prices = (len(_PRICE_LINE.findall(html_to_text(r.text)))
                       + len(parse_html_tables(r.text)))
        looks_price = bool(_PRICE_URL_HINT.search(url)
                           or _PRICE_URL_HINT.search(label))
        # порог ниже для страниц с прайс-адресом: категория разветвлённого
        # прайса («Трихология — 6 позиций») до восьми строк не дотягивает,
        # а прайсом является
        is_price = text_prices >= 8 or (text_prices >= 3 and looks_price)
        if is_price and url not in price_pages:
            price_pages.append(url)               # страница-прайс найдена
        on_services = bool(re.search(r"/uslugi|/servic|/napravlen", url, re.I))
        in_branch = branch or is_price or bool(_URL_HIGH.search(url))
        if in_branch and not is_price and url not in dry_branch:
            dry_branch.append(url)
        for lbl, href in page_links(r.text, url):
            s = link_scent(lbl, href)
            # кейс azbuka-samara (заказчик): цены живут на подстраницах
            # раздела «Услуги» без прайс-слов в якорях — детям раздела
            # услуг даётся минимальный запах, чтобы обход туда спустился
            if (s == 0 and on_services
                    and re.search(r"/uslugi|/servic|/napravlen", href, re.I)):
                s = 20
            if s >= 100:
                if href not in files:
                    files.append(href)            # документ — терминал
                continue
            if (in_branch and href not in visited
                    and not _SKIP_URL.search(href)
                    and not _FILE_EXT.search(href)
                    and _same_host(domain, href)
                    and _branch_child(url, href)
                    and depth + 1 <= max_depth + 3):
                queue.append((95, depth + 1, href, lbl, True))
            elif s >= 20 and depth + 1 <= max_depth and href not in visited:
                queue.append((s, depth + 1, href, lbl, False))
        if files:
            break                                  # документ первичен
    db.commit()
    return {"files": files, "price_pages": price_pages, "route": route,
            "dry_branch": dry_branch, "pages_seen": len(visited),
            "branch_pages": branch_seen, "reachable": bool(route)}


# --- парсер прайса: мультипаттерн -------------------------------------------

def normalize_price_number(captured: str) -> float | None:
    """«368.200» → 368200 · «32040.00» → 32040 · «4 100,50» → 4100.5.

    Разбор 2026-09-04: разделителем тысяч в прайсах бывает пробел, точка и
    запятая — и та же точка/запятая обозначает копейки. Неоднозначность
    снимается по длине ПОСЛЕДНЕЙ группы: ровно три цифры → это разряд тысяч
    («2.500 ₽» = 2500, ks-lazer.ru), одна-две → копейки («32040.00 ₽» =
    32040, duetclinic.ru)."""
    s = re.sub(r"[\s\u00a0\u202f]", "", captured or "")
    if not s:
        return None
    parts = re.split(r"[.,]", s)
    if len(parts) > 1 and len(parts[-1]) == 3:
        s = "".join(parts)                            # разделители — тысячи
    elif len(parts) > 1:
        s = "".join(parts[:-1]) + "." + parts[-1]     # хвост — копейки
    try:
        return float(s)
    except ValueError:
        return None


def parse_price_value(raw: str) -> tuple[float | None, str]:
    """Однозначная цена в рублях или None (вилки/«от» не досчитываются)."""
    if _FROM_RANGE.search(raw):
        return None, "RUB"
    m = _PRICE_LINE.search(raw)
    if not m:
        return None, ""
    val = normalize_price_number(m.group(1))
    return (val, "RUB") if val is not None else (None, "")


def parse_price_text(text: str) -> list[dict]:
    """Текст (рендер/markdown) → позиции. Паттерны: «название … цена» в одной
    строке; пары название→строка-цена; триплеты код/название/цена (ЕМЦ)."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    items, section, pending_code, pending_name = [], "", None, None
    last_service = None            # услуга блока «цены по филиалам»
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
                # адрес вместо названия: услуга — выше по блоку, адрес идёт
                # в раздел как филиал (иначе в таблице 17 тыс. «услуг»-адресов)
                if _ADDR_LINE.search(pending_name) and last_service:
                    name = last_service
                    sect = (f"{section} · филиал: {pending_name}" if section
                            else f"филиал: {pending_name}")[:200]
                else:
                    name, sect = pending_name, section
                items.append({"section": sect, "code": pending_code or "",
                              "name": name, "price_raw": ln,
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
            if pending_name and len(ln) < 20 and not _CODE_LINE.match(ln):
                pending_name = f"{pending_name}, {ln}"  # уточнение («1 зуба»)
            else:
                pending_name = ln                  # кандидат пары/триплета
            if (pending_name and len(pending_name) >= 6
                    and not _ADDR_LINE.search(pending_name)
                    and not _UI_NOISE.match(pending_name)):
                last_service = pending_name        # услуга блока филиалов
    return items


def _table_row(items: list, section: str, row: tuple) -> str:
    """Одна строка таблицы (xlsx/xls/таблица PDF) → позиция или раздел.
    Возвращает актуальный раздел."""
    cells = [c for c in row if c is not None and str(c).strip()]
    texts = [str(c).strip() for c in cells if not isinstance(c, (int, float))]
    nums = [c for c in cells if isinstance(c, (int, float))]
    if not nums:                        # цена бывает текстом «1 500 руб»
        for t in list(texts):
            v, _ = parse_price_value(t)
            if v is not None and _PRICE_ONLY.match(t):
                nums.append(v)
                texts.remove(t)
    if len(texts) == 1 and not nums and len(texts[0]) > 7:
        return texts[0][:200]
    if texts and nums:
        name = max(texts, key=len)
        if re.search(r"[а-яА-ЯёЁ]{4}", name):
            items.append({"section": section, "code": "", "name": name[:300],
                          "price_raw": str(nums[-1])[:60],
                          "price_value": float(nums[-1]), "currency": "RUB"})
    return section


_NAKED_PRICE = re.compile(r"^\d{2,7}$")
_NAKED_RANGE = re.compile(r"^(?:от\s*)?\d[\d\s ]{0,8}(?:[-–—]|до)\s*"
                          r"\d[\d\s ]{0,8}(?:руб\.?|₽)?$", re.I)


def parse_html_tables(html: str) -> list[dict]:
    """Таблицы «Услуга | Цена» с ГОЛЫМИ числами без «руб» (кейс
    azbuka-samara, заказчик: «от 200 до 800», «100-500»). Голые числа
    безопасны только в табличном контексте; телефоны отсекаются длиной
    (≤7 цифр) и форматом."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001
        return []
    items = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True)
                     for td in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue
            names = [c for c in cells if re.search(r"[а-яА-ЯёЁ]{4}", c)
                     and not _PRICE_LINE.search(c)]
            prices = []
            for c in cells:
                flat = re.sub(r"[\s ]", "", c)
                if (_NAKED_PRICE.match(flat) or _NAKED_RANGE.match(c)
                        or _PRICE_ONLY.match(c)):
                    prices.append(c)
            if names and prices:
                raw = prices[-1]
                flat = re.sub(r"[\s ]", "", raw)
                val = (float(flat) if _NAKED_PRICE.match(flat)
                       else parse_price_value(raw)[0])
                items.append({"section": "", "code": "",
                              "name": max(names, key=len)[:300],
                              "price_raw": raw[:60], "price_value": val,
                              "currency": "RUB"})
    return items


def parse_price_file(data: bytes, ext: str) -> list[dict]:
    """PDF/XLSX/.xls → позиции. .doc честно отдаётся в лабораторию."""
    import io
    ext = ext.lower().lstrip(".")
    if ext == "pdf":
        import pdfplumber
        text, items, section = [], [], ""
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages[:80]:
                text.append(page.extract_text() or "")
            got = parse_price_text("\n".join(text))
            if len(got) >= 10:
                return got
            # текстовый слой слаб (кейс avicenna72: 1 позиция) → таблицы PDF
            for page in pdf.pages[:80]:
                for tbl in page.extract_tables() or []:
                    for row in tbl:
                        section = _table_row(items, section, tuple(row))
        return items if len(items) > len(got) else got
    if ext in ("xlsx", "xls"):
        items, section = [], ""
        if data[:4] == b"\xd0\xcf\x11\xe0":        # старый .xls (OLE), не zip
            import xlrd                             # кейс avismed (заказчик)
            book = xlrd.open_workbook(file_contents=data)
            for sh in book.sheets():
                for i in range(sh.nrows):
                    row = tuple(c if str(c).strip() != "" else None
                                for c in sh.row_values(i))
                    section = _table_row(items, section, row)
            return items
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True,
                                    data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                section = _table_row(items, section, row)
        return items
    return []


# --- каскад по одной компании -----------------------------------------------

def _save_items(db, inn, domain, url, items):
    """url — источник по умолчанию; у позиции с разветвлённого прайса свой
    (`_url`): доказательство привязано к той странице, где цена и стоит,
    а не к первой странице меню (CLAUDE.md: факт без своего URL — не факт)."""
    ts = time.strftime("%Y-%m-%d")
    db.execute("DELETE FROM price_items WHERE domain=?", (domain,))
    # ОДНОЙ пачкой: построчная вставка 18 955 позиций (nika-nn) держала
    # запись базы минуты, и соседние потоки срывались с «database is locked»
    # даже на busy_timeout 30 с (2026-09-04)
    # ФИО врача в названии позиции/разделе прайса («Приём — Иванова М.П.»)
    # обезличивается до записи: 152-ФЗ, принцип минимизации (2026-09-04)
    from src.depersonalize import mask_fio
    db.executemany(
        "INSERT INTO price_items (inn, domain, url, section, code,"
        " name_raw, price_raw, price_value, currency, checked_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(inn, domain, (it.get("_url") or url)[:300],
          mask_fio(it["section"]), it["code"],
          mask_fio(it["name"])[:300], it["price_raw"][:100],
          it["price_value"], it["currency"], ts) for it in items])


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
    nav = {"files": [], "price_pages": [], "route": [], "pages_seen": 0,
           "reachable": None}
    if not files:
        nav = navigate(db, domain, delay)          # P1
        files = nav["files"]
    level, status, items, src_url = "", "", [], ""
    file_keys = set()
    for f in files:                                # P2 — документ первичен
        r = polite_get(f, delay)
        if not r:
            continue
        METER["files_downloaded"] += 1
        try:                                       # кривой файл ≠ смерть
            got = parse_price_file(r.content, f.rsplit(".", 1)[-1][:4])
        except Exception as e:  # noqa: BLE001
            print(f"    ⚠ файл {f[:80]}: {type(e).__name__} — пропущен")
            continue
        # СУММА ПО ФАЙЛАМ, а не «самый толстый» (2026-09-07): разветвлённый
        # прайс выкладывают несколькими документами — по одному на раздел;
        # прежнее правило «взять файл с наибольшим числом позиций» оставляло
        # от такого прайса один раздел из нескольких.
        fresh = [g for g in got
                 if (g["name"], g["price_raw"]) not in file_keys]
        for g in fresh:
            file_keys.add((g["name"], g["price_raw"]))
        if fresh:
            for g in fresh:
                g["_url"] = f
            items.extend(fresh)
            src_url = src_url or f
            level = "P2:документ"
    seen_keys = {(i["name"], i["price_raw"]) for i in items}
    dry_pages, pages_parsed = [], 0
    if not items:                                  # P3 — страницы (сумма!)
        pages = nav["price_pages"] or [f"https://{domain}/price/",
                                       f"https://{domain}/ceny/"]
        from src.fetch_cascade import _level1_jina
        # ВСЯ ВЕТКА, а не первые шесть страниц (заказчик, 2026-09-07:
        # «прайсы разветвлённые… чтобы скачать всё — надо походить по
        # страницам»). Позиции суммируются по всем страницам меню с
        # дедупликацией по (название, дословная цена).
        for pu in pages[:PRICE_PAGES_CAP]:
            r = polite_get(pu, delay)
            got = parse_price_text(html_to_text(r.text)) if r else []
            if r:                                  # + таблицы с голыми числами
                got.extend(parse_html_tables(r.text))
            if len(got) < 20:                      # детектор полноты → Jina
                jt, _, _ = _level1_jina(pu)
                METER["jina_requests"] += 1
                METER["bytes"] += len(jt or "")
                got2 = parse_price_text(jt or "")
                if len(got2) > len(got):
                    got = got2
                    level = "P3:jina"
            pages_parsed += 1
            fresh = [g for g in got
                     if (g["name"], g["price_raw"]) not in seen_keys]
            for g in fresh:
                seen_keys.add((g["name"], g["price_raw"]))
            if fresh:
                for g in fresh:
                    g["_url"] = pu
                items.extend(fresh)
                src_url = src_url or pu
                level = level or "P3:статика"
            elif r is not None and not got:
                # страница ветки ОТКРЫЛАСЬ, а цен из неё не извлеклось —
                # кандидат на браузерный рендер, даже если соседние
                # страницы уже что-то дали. Не открывшаяся (404 от догадки
                # «/price/») кандидатом не считается: рендерить нечего.
                dry_pages.append(pu)
    # P4 — БРАУЗЕРНЫЙ РЕНДЕР там, где страница прайса найдена, а цен из неё
    # не извлеклось (2026-09-07): у сайтов на Tilda/Bitrix/SPA цены рисует
    # JavaScript, в сыром HTML их нет ни одной. Без этого уровня 225 доменов
    # из 536 «неудачных» получали статус «прайс не найден» при открытой и
    # прочитанной прайс-странице.
    # страницы ветки, которые навигатор ОТКРЫЛ, а цен в статике не нашёл
    # (случай 5pmedicina.ru: обход прошёл 45 страниц меню, цен в сыром HTML
    # нет ни на одной — их рисует скрипт). Это главные кандидаты на рендер
    dry_pages += [u for u in nav.get("dry_branch", []) if u not in dry_pages]
    if not items or dry_pages:
        # кандидаты на рендер: «сухие» страницы ветки + всё, что навигатор
        # опознал как прайс. Если статика не показала НИЧЕГО (сайт целиком
        # рисуется скриптом — случай agk24.ru: пять страниц обхода, ни
        # одной ссылки), рендерится главная: меню прайса видно только там.
        pages4 = dry_pages + nav["price_pages"] + [
            s["url"] for s in nav["route"]
            if _PRICE_URL_HINT.search(s.get("url", ""))
            or _PRICE_URL_HINT.search(s.get("label", ""))]
        if not pages4 and not items:
            pages4 = [f"https://{domain}/"]
        queue4 = list(dict.fromkeys(pages4))
        rendered_n, k = 0, 0
        while k < len(queue4) and rendered_n < P4_PAGES_CAP:
            pu = queue4[k]
            k += 1
            rendered_n += 1        # бюджет тратит ЛЮБАЯ попытка, включая
            rendered = browser_render(pu, delay)   # неудачную: иначе при
            if not rendered:                       # неработающем браузере
                continue                           # обходится вся очередь
            got = parse_price_text(html_to_text(rendered))
            got.extend(parse_html_tables(rendered))
            fresh = [g for g in got
                     if (g["name"], g["price_raw"]) not in seen_keys]
            for g in fresh:
                seen_keys.add((g["name"], g["price_raw"]))
            if fresh:
                for g in fresh:
                    g["_url"] = pu
                items.extend(fresh)
                src_url = src_url or pu
                if "P4:браузер" not in level:      # метка ставится один раз,
                    level = (f"{level}+P4:браузер"     # сколько бы страниц
                             if level else "P4:браузер")  # ни отрендерили
            # МЕНЮ, НАРИСОВАННОЕ СКРИПТОМ, видно только после рендера:
            # ссылки на категории прайса добираются здесь же
            for lbl, href in page_links(rendered, pu):
                if (href not in queue4 and _same_host(domain, href)
                        and (_branch_child(pu, href)
                             or _PRICE_URL_HINT.search(href)
                             or _PRICE_URL_HINT.search(lbl or ""))
                        and not _SKIP_URL.search(href)
                        and not _FILE_EXT.search(href)):
                    queue4.append(href)
        pages_parsed += rendered_n

    prev = db.execute("SELECT prev_status, prev_level, prev_items, prev_page "
                      "FROM price_rerun WHERE domain=?", (domain,)).fetchone()
    if prev and len(items) < (prev[2] or 0):
        # ПЕРЕПРОГОН НЕ УХУДШАЕТ (2026-09-07). Домены гоняются повторно
        # ради разветвлённых прайсов; если сайт с тех пор лёг или прайс
        # переехал, новый проход даст меньше — прежний разбор остаётся как
        # был, а факт неудачной попытки уходит в примечание.
        db.execute("UPDATE price_recipes SET status=?, level=?, items_n=?, "
                   "price_page_url=?, note=? WHERE domain=?",
                   (prev[0], prev[1], prev[2], prev[3],
                    f"перепрогон {ts}: получено {len(items)} позиций из "
                    f"{prev[2]} — прежний разбор сохранён", domain))
        db.execute("DELETE FROM price_rerun WHERE domain=?", (domain,))
        db.commit()
        return {"domain": domain, "status": prev[0], "level": prev[1],
                "items": prev[2], "kept": True}
    db.execute("DELETE FROM price_rerun WHERE domain=?", (domain,))
    if items:
        _save_items(db, inn, domain, src_url, items)
        status = "прайс извлечён"
    elif nav["price_pages"] or any(
            _PRICE_URL_HINT.search(s.get("url", "")) for s in nav["route"]):
        # страница прайса НАЙДЕНА и прочитана, но цен в ней нет даже после
        # рендера — это не «прайса нет», а «не смогли извлечь». Разные вещи,
        # и в таблице они должны выглядеть по-разному (CLAUDE.md: «нет
        # страницы ≠ нет услуги»)
        status = "страница прайса найдена, цены не извлечены"
        level = level or "P5:не извлечено"
    elif nav["reachable"] is False:
        # ТРИ РАЗНЫХ ИСХОДА, а не один (CLAUDE.md: «нет страницы ≠ нет
        # услуги»; разбор 2026-09-04: из 335 строк P5 сайт не открылся у 41).
        status = "сайт недоступен на дату проверки"
        level = level or "P5:сайт недоступен"
    elif files:
        status = "прайс-файл не разобран на дату проверки"
        level = level or "P5:файл не разобран"
    else:
        status = "прайс не найден на дату проверки"  # P5 — честный статус
        level = level or "P5"
    db.execute("INSERT OR REPLACE INTO price_recipes VALUES "
               "(?,?,?,?,?,?,?,?,?,?,?)",
               (domain, inn, level, status, src_url,
                json.dumps(files, ensure_ascii=False),
                json.dumps(nav["route"], ensure_ascii=False),
                len({i['section'] for i in items}), len(items),
                f"страниц навигатора: {nav['pages_seen']} · ветка прайса: "
                f"{nav.get('branch_pages', 0)} · разобрано страниц: "
                f"{pages_parsed}", ts))
    db.commit()
    return {"domain": domain, "status": status, "level": level,
            "items": len(items), "files_found": len(files)}


# ── ШАРДИРОВАНИЕ ПО ДОМЕНАМ (заказчик, 2026-09-07: «можно на большее
# число?» — 15 шардов). У прайсов нет платных квот (HTTP + Jina + локальный
# Playwright), поэтому делить нечего и шардов может быть больше, чем у
# поиска. Разбиение — стабильный хэш домена (crc32), НЕ диапазоны строк:
# один домен всегда попадает в один и тот же шард, сколько бы волн ни шло,
# и шарды не пересекаются по доменам — вежливость к сайту не страдает. ──

def shard_of(domain: str, shards: int) -> int:
    """Номер шарда домена, 1..shards. Детерминирован между процессами
    (crc32, не hash() — у того соль на каждый запуск интерпретатора)."""
    key = re.sub(r"^www\.", "", (domain or "").strip().lower())
    return zlib.crc32(key.encode("utf-8")) % shards + 1


def _shard_env() -> tuple[int, int]:
    """(мой номер, всего шардов) из окружения; (1, 1) = без шардирования."""
    try:
        shards = int(os.environ.get("PRICE_SHARDS", "1") or 1)
        shard = int(os.environ.get("PRICE_SHARD", "1") or 1)
    except ValueError:
        return 1, 1
    if shards < 2 or not (1 <= shard <= shards):
        return 1, 1
    return shard, shards


def run_batch(db: sqlite3.Connection, limit: int = 40,
              budget_sec: float = 0, workers: int = 1,
              db_factory=None) -> list[dict]:
    """Обкатка: первые N компаний с найденным сайтом (потом — по фильтру
    заказчика). Чекпойнт подомённо, перезапуск продолжает с места."""
    ensure_price_tables(db)
    # ФИЛЬТР ЗАКАЗЧИКА (2026-08-28, разбор пачки 1): в прайс-контур идут
    # ТОЛЬКО компании, в чьей мед-лицензии есть дерматовенерология и/или
    # онкология и/или косметология. Искать прайсы заведомо непрофильных —
    # бессмысленная трата времени и лимитов (130 из 200 в пачке 1).
    rows = db.execute(
        f"SELECT c.inn, c.found_site FROM {T40} c "
        "WHERE c.found_site IS NOT NULL AND c.found_site<>'' "
        f"AND EXISTS (SELECT 1 FROM {RZN} l WHERE l.inn=c.inn "
        "  AND l.is_med=1 AND (l.specialties LIKE '%дерматовенерологи%' "
        "  OR l.specialties LIKE '%онкологи%' "
        "  OR l.specialties LIKE '%косметологи%')) "
        "AND NOT EXISTS (SELECT 1 FROM price_recipes r "
        "  WHERE r.domain=c.found_site AND r.status NOT IN ('', 'в работе')) "
        "ORDER BY c.row_no").fetchall()
    # лимит применяется ПОСЛЕ фильтра шарда: иначе SQL-LIMIT отдал бы шарду
    # первые N строк базы, из которых свои — лишь каждая пятнадцатая
    shard, shards = _shard_env()
    if shards > 1:
        print(f"шард {shard}/{shards}: беру только свои домены", flush=True)
    out, seen_domains, todo = [], set(), []
    for inn, site in rows:
        if site in seen_domains:                   # один домен — один разбор
            continue
        seen_domains.add(site)
        if shards > 1 and shard_of(site, shards) != shard:
            continue
        if len(todo) >= limit:
            break
        todo.append((inn, site))
    t_start = time.time()

    # ПАРАЛЛЕЛЬНО по доменам (заказчик, 2026-09-02: «это очень долго»):
    # пауза вежливости — внутри домена, домены разные; у каждого потока своя
    # связка соединений (SQLite не делит соединение между потоками)
    def _one(item):
        inn, site = item
        wdb = db_factory() if db_factory else db
        try:
            t0 = time.time()
            res = run_company(wdb, inn, site)
            res["_sec"] = time.time() - t0
            return res
        finally:
            if db_factory:
                wdb.close()

    if workers <= 1 or db_factory is None:
        for item in todo:
            if budget_sec and time.time() - t_start > budget_sec:
                print("⏱ прайс-каскад: бюджет времени исчерпан — остаток на "
                      "следующий прогон (чекпойнт подомённо)", flush=True)
                break
            res = _one(item)
            print(f"  {res['domain']}: {res['status']} ({res.get('items', 0)} "
                  f"позиций, {res.get('level', '')}, {res['_sec']:.0f} с)", flush=True)
            out.append(res)
    else:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            chunk = workers * 2
            for i in range(0, len(todo), chunk):
                if budget_sec and time.time() - t_start > budget_sec:
                    print("⏱ прайс-каскад: бюджет времени исчерпан — остаток на "
                          "следующий прогон (чекпойнт подомённо)", flush=True)
                    break
                for fut in cf.as_completed([ex.submit(_one, it)
                                            for it in todo[i:i + chunk]]):
                    try:
                        res = fut.result()
                    except Exception as e:  # noqa: BLE001 — домен на повтор
                        print(f"  ⚠ домен упал: {type(e).__name__}: "
                              f"{str(e)[:160]}", flush=True)
                        continue
                    print(f"  {res['domain']}: {res['status']} ({res.get('items', 0)} "
                          f"позиций, {res.get('level', '')}, {res['_sec']:.0f} с)",
                          flush=True)
                    out.append(res)
    print(f"РАСХОД: HTTP {METER['http_requests']} зап. · "
          f"Jina {METER['jina_requests']} зап. · "
          f"файлов {METER['files_downloaded']} · "
          f"{METER['bytes'] / 1e6:.1f} МБ · "
          f"пауз вежливости {METER['seconds_sleep'] / 60:.0f} мин · 0 ₽")
    return out


def remaining(db: sqlite3.Connection) -> int:
    """Профильные компании с сайтом, чей домен ещё не разобран."""
    ensure_price_tables(db)
    return db.execute(
        f"SELECT COUNT(DISTINCT c.found_site) FROM {T40} c "
        "WHERE c.found_site IS NOT NULL AND c.found_site<>'' "
        f"AND EXISTS (SELECT 1 FROM {RZN} l WHERE l.inn=c.inn "
        "  AND l.is_med=1 AND (l.specialties LIKE '%дерматовенерологи%' "
        "  OR l.specialties LIKE '%онкологи%' "
        "  OR l.specialties LIKE '%косметологи%')) "
        "AND NOT EXISTS (SELECT 1 FROM price_recipes r "
        "  WHERE r.domain=c.found_site AND r.status NOT IN ('', 'в работе'))"
    ).fetchone()[0]


_BRANCH_HINT = re.compile(
    r"прайс|price|цен|ceny|стоимост|тариф|uslugi|servic|napravlen", re.I)


def rerun_branch(db: sqlite3.Connection, mode: str = "all",
                 apply: bool = True) -> dict:
    """Снимает чекпойнт с доменов, которые надо пройти каскадом заново
    после доработки разветвлённого прайса (заказчик, 2026-09-07: «прайсы
    разветвлённые… чтобы скачать всё — надо походить по страницам»).

    Кого гоняем заново:
    · failed  — всё, кроме «прайс извлечён»: у них теперь есть P4 по всей
                ветке и своя глубина обхода;
    · branchy — успешные домены, в маршруте которых ДВЕ И БОЛЬШЕ страниц с
                прайс-адресом: прежний код суммировал максимум шесть
                страниц и не спускался в подкатегории, поэтому такой прайс
                собран частично;
    · all     — и то и другое.

    Прежний результат домена записывается в price_rerun: если новый проход
    даст меньше позиций, он не заменит собой старый."""
    ensure_price_tables(db)
    out = {"на перепрогон: неудачные": 0, "на перепрогон: разветвлённые": 0}
    picked = []
    for dom, inn, status, level, items_n, page, route in db.execute(
            "SELECT domain, inn, status, level, items_n, price_page_url, route "
            "FROM price_recipes"):
        ok = status == "прайс извлечён"
        if not ok and mode in ("all", "failed"):
            picked.append((dom, status, level, items_n, page))
            out["на перепрогон: неудачные"] += 1
            continue
        if ok and mode in ("all", "branchy"):
            try:
                rt = json.loads(route or "[]")
            except (ValueError, TypeError):
                rt = []
            hits = {s.get("url", "") for s in rt
                    if _BRANCH_HINT.search(s.get("url", ""))}
            if len(hits) >= 2:
                picked.append((dom, status, level, items_n, page))
                out["на перепрогон: разветвлённые"] += 1
    if apply and picked:
        ts = time.strftime("%Y-%m-%d %H:%M")
        db.executemany(
            "INSERT OR REPLACE INTO price_rerun VALUES (?,?,?,?,?,?)",
            [(d, st, lv, it or 0, pg, ts) for d, st, lv, it, pg in picked])
        # чекпойнт снимается статусом «в работе», а не удалением строки:
        # маршрут навигатора и прежние счётчики нужны и для отбора, и для
        # гварда «перепрогон не ухудшает»
        db.executemany("UPDATE price_recipes SET status='в работе' "
                       "WHERE domain=?", [(d,) for d, *_ in picked])
        db.commit()
    out["итого"] = len(picked)
    return out


def reparse(db: sqlite3.Connection, apply: bool = True) -> dict:
    """Применяет исправления парсера 2026-09-04 к УЖЕ СОБРАННОЙ базе, не
    ходя в сеть заново (дословная цена и маршрут сохранены — этого хватает):

    1. price_value пересчитывается из дословной цены (копейки, точка как
       разделитель тысяч, склейка двух цен в строке);
    2. статус P5 расщепляется по журналу навигатора на «сайт недоступен» /
       «прайс-файл не разобран» / «прайс не найден»;
    3. домены, где в названия услуг попали адреса филиалов, теряют
       чекпойнт — там название услуги в базе утеряно, нужен повторный
       разбор сайта новым парсером (следующий прогон каскада сделает сам).

    apply=False — только посчитать, ничего не менять."""
    ensure_price_tables(db)
    out = {"цен пересчитано": 0,
           "статус «сайт недоступен»": 0, "статус «файл не разобран»": 0,
           "доменов на повторный разбор": 0, "позиций удалено": 0}

    # 1 — цены. Пересчёт ТОЛЬКО УТОЧНЯЕТ: если новый разбор ничего не дал,
    # прежнее значение остаётся (часть цен пришла из числовых ячеек таблиц,
    # где дословная запись — голое «4900» без знака рубля).
    fixes = []
    for pid, raw, old in db.execute(
            "SELECT id, price_raw, price_value FROM price_items"):
        text = str(raw or "")
        new, _ = parse_price_value(text)
        if new is None and not _FROM_RANGE.search(text):
            new = normalize_price_number(text)     # голое число из таблицы
        if new is not None and new != old:
            fixes.append((new, pid))
            out["цен пересчитано"] += 1
    if apply and fixes:
        db.executemany("UPDATE price_items SET price_value=? WHERE id=?", fixes)

    # 2 — статусы: сайт открывался или нет (журнал навигатора уже есть)
    for dom, furls in db.execute(
            "SELECT domain, file_urls FROM price_recipes "
            "WHERE status='прайс не найден на дату проверки'"):
        log = [v[0] for v in db.execute(
            "SELECT verdict FROM price_nav_log WHERE domain=?", (dom,))]
        if log and all(v == "недоступна" for v in log):
            out["статус «сайт недоступен»"] += 1
            if apply:
                db.execute("UPDATE price_recipes SET status=?, level=? "
                           "WHERE domain=?", ("сайт недоступен на дату проверки",
                                              "P5:сайт недоступен", dom))
        elif not log and furls not in ("", "[]", None):
            out["статус «файл не разобран»"] += 1
            if apply:
                db.execute("UPDATE price_recipes SET status=?, level=? "
                           "WHERE domain=?",
                           ("прайс-файл не разобран на дату проверки",
                            "P5:файл не разобран", dom))

    # 3 — домены с адресами вместо названий услуг
    # Домены, чей прайс нужно разобрать ЗАНОВО (из базы уже не чинится):
    #  · адрес филиала попал в название услуги (nika-nn);
    #  · РАЗОРВАННАЯ ЦЕНА — старый шаблон резал строку по «00» из копеек,
    #    целая часть осталась в названии («…гигиена 4000,»), в цене «00 руб»
    #    (радугаздоровья.рф, neplacebo.ru): восстанавливать надо и цену,
    #    и название.
    addr_re = re.compile(r"^(?:г|гор|пос|пгт|с|дер)\.\s|,\s*(?:д|ул)\.\s")
    torn_re = re.compile(r"[\d][.,]\s*$")
    tot, broken = {}, {}
    for dom, name, raw, val in db.execute(
            "SELECT domain, name_raw, price_raw, price_value FROM price_items"):
        tot[dom] = tot.get(dom, 0) + 1
        nm, rw = str(name or ""), str(raw or "")
        if addr_re.search(nm) or (val == 0 and torn_re.search(nm)
                                  and re.match(r"^\d{1,2}\s*(?:руб|₽)", rw)):
            broken[dom] = broken.get(dom, 0) + 1
    bad = [d for d, n in broken.items() if n / tot[d] >= 0.05]
    out["доменов на повторный разбор"] = len(bad)
    # недоступный сайт — не приговор: чекпойнт снимается, каскад повторит
    # попытку (позиций у таких доменов нет, терять нечего)
    retry = [r[0] for r in db.execute(
        "SELECT domain FROM price_recipes "
        "WHERE status='сайт недоступен на дату проверки'")]
    out["недоступных на повторную попытку"] = len(retry)
    if apply:
        for dom in bad:
            out["позиций удалено"] += db.execute(
                "DELETE FROM price_items WHERE domain=?", (dom,)).rowcount
            db.execute("DELETE FROM price_recipes WHERE domain=?", (dom,))
        for dom in retry:
            db.execute("DELETE FROM price_recipes WHERE domain=?", (dom,))
        for pid, name in list(db.execute(
                "SELECT id, name_raw FROM price_items")):
            if addr_re.search(str(name or "")):    # единичный мусор в чистом
                db.execute("DELETE FROM price_items WHERE id=?", (pid,))
                out["позиций удалено"] += 1
    if apply:
        db.commit()
    return out


def export_prices(db: sqlite3.Connection, path: str | None = None,
                  wb=None) -> str:
    """Выгрузка: Рецепты_доменов / Позиции / Выбросы_на_проверку.
    wb передан — листы дописываются в общий сводный файл (combined_export)."""
    import openpyxl
    from openpyxl.styles import Font

    from src.xlsx_utils import xl_row
    standalone = wb is None
    wb = wb if wb is not None else openpyxl.Workbook()
    bold = Font(bold=True)
    ws = wb.active if standalone else wb.create_sheet("Прайсы_рецепты")
    ws.title = "Прайсы_рецепты" if not standalone else "Рецепты_доменов"
    ws.append(["№ строки", "Компания", "Домен", "ИНН", "Уровень каскада",
               "Статус", "Страница/файл прайса", "Позиций", "Разделов",
               "Примечание"])
    for c in ws[1]:
        c.font = bold
    # ПОРЯДОК СТРОК БАЗЫ, а не группировка по статусу (заказчик, 2026-09-07:
    # «после 606 строки повально прайсы не найдены»). Прежнее ORDER BY status
    # складывало все 604 успешных домена в начало листа, а все неуспешные —
    # подряд следом: выглядело так, будто с 606-й строки конвейер сломался,
    # хотя по номерам строк выборки успех распределён ровно (39-64% в каждой
    # сотне). Теперь лист идёт в том же порядке, что и лист ИТОГ, и строки
    # сопоставляются с ним по номеру.
    try:
        rows = db.execute(
            "SELECT c.row_no, c.name, r.domain, r.inn, r.level, r.status, "
            "r.price_page_url, r.items_n, r.sections_n, r.note "
            f"FROM price_recipes r LEFT JOIN {T40} c ON c.found_site=r.domain "
            "ORDER BY COALESCE(c.row_no, 999999), r.domain").fetchall()
    except sqlite3.OperationalError:      # прайсовая база без основной
        rows = [(None, None) + tuple(r) for r in db.execute(
            "SELECT domain, inn, level, status, price_page_url, items_n, "
            "sections_n, note FROM price_recipes ORDER BY domain")]
    for r in rows:
        ws.append(xl_row(r))
    ws2 = wb.create_sheet("Позиции")
    ws2.append(["ИНН", "Домен", "Раздел", "Код", "Название (дословно)",
                "Цена (дословно)", "Цена, руб", "URL источника", "Дата"])
    for c in ws2[1]:
        c.font = bold
    for r in db.execute("SELECT inn, domain, section, code, name_raw, "
                        "price_raw, price_value, url, checked_at "
                        "FROM price_items ORDER BY domain, id"):
        ws2.append(xl_row(r))
    ws3 = wb.create_sheet("Выбросы_на_проверку")
    ws3.append(["Домен", "Название", "Цена дословно", "Цена, руб", "URL"])
    for c in ws3[1]:
        c.font = bold
    for r in db.execute("SELECT domain, name_raw, price_raw, price_value, url "
                        "FROM price_items WHERE price_value<50 "
                        "OR price_value>1000000 ORDER BY price_value"):
        ws3.append(xl_row(r))
    if not standalone:
        return ""
    path = path or f"output/Прайсы_профиль_{time.strftime('%Y-%m-%d')}.xlsx"
    wb.save(path)
    return path


if __name__ == "__main__":
    import sys
    db = open_dbs()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "remaining":                          # для самопродолжения
        print(remaining(db))
        sys.exit(0)
    if cmd == "probe" and len(sys.argv) > 2:       # обкатка одного домена
        print(run_company(db, sys.argv[3] if len(sys.argv) > 3 else "",
                          sys.argv[2]))
    elif cmd == "rerun":                           # перепрогон ветки прайса
        mode = "all"
        for a in sys.argv[2:]:
            if a in ("failed", "branchy", "all"):
                mode = a
        dry = "--dry" in sys.argv
        print("перепрогон:", rerun_branch(db, mode, apply=not dry))
    elif cmd == "reparse":                         # исправления к готовой базе
        dry = len(sys.argv) > 2 and sys.argv[2] == "--dry"
        print("реparse:", reparse(db, apply=not dry))
    elif cmd == "export":
        print("файл:", export_prices(db))
    elif cmd == "run":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
        b = float(sys.argv[3]) if len(sys.argv) > 3 else 0
        # чистку сирот делает только главное соединение: одновременный
        # DELETE из шести потоков ронял домены «database is locked»
        workers = int(os.environ.get("PRICE_WORKERS", "6") or 6)
        res = run_batch(db, n, b, workers=workers,
                        db_factory=lambda: open_dbs(clean_orphans=False))
        ok = sum(1 for r in res if r.get("items"))
        print(f"Итог: {ok}/{len(res)} с извлечённым прайсом")
