"""Каскад доступа к сайтам — 4 уровня строго по возрастанию стоимости
(требование заказчика 2026-08-26, пп.3-6 второго промпта исправления).

Уровень 1 — Jina Reader (r.jina.ai): дёшево, быстро, основной путь.
Уровень 2 — прямой HTTP с браузерным User-Agent и заголовками, пауза.
Уровень 3 — Playwright headless: networkidle, ожидание селектора, скролл.
Уровень 4 — Playwright с полной эмуляцией: реальный UA, viewport, языковые
            заголовки, случайные задержки, имитация движения курсора.

Каждый следующий уровень применяется ТОЛЬКО к тому, что не взял предыдущий.
Каждая попытка фиксируется в таблице fetch_attempts: уровень, код ответа,
байты, сработал ли селектор, взят ли контент — после прогона видно, какой
уровень реально нужен.

ПРАВИЛО SUSPICIOUS_ZERO НА СТРАНИЦЕ: ответ 200 без признаков контента
(нет цен, нет названий услуг из справочника, объём меньше порога) — НЕ взят,
передаётся следующему уровню. Формальный успех не считается успехом.

ПРАВОВОЙ РЕЖИМ (Юрист OSINT, право вето на способ сбора):
- robots.txt чтится на ВСЕХ уровнях: запрет → «заблокировано robots.txt»,
  сайт идёт в ручной список заказчику, обход запрещён;
- CAPTCHA и авторизация НЕ обходятся никогда — сайт помечается недоступным;
- частота ≤1 запрос / RATE_DELAY_SEC на домен, эмуляция — только чтение
  публичных страниц, которые сайт показывает любому посетителю.
"""

import datetime
import os
import random
import re
import sqlite3
import time

import httpx

RATE_DELAY_SEC = 3
CONTENT_MIN_BYTES = 3000        # порог объёма; правится без кода в thresholds.yaml
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BROWSER_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
}
PRICE_RE = re.compile(r"\d[\d\s]{0,8}\s*(?:₽|руб\.?)", re.IGNORECASE)

_robots_cache: dict[str, list[tuple[str, str]] | None] = {}


def ensure_fetch_tables(db: sqlite3.Connection):
    db.execute("""CREATE TABLE IF NOT EXISTS fetch_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT, url TEXT, level INTEGER, status TEXT, bytes INTEGER,
        selector_hit INTEGER, content_ok INTEGER, note TEXT, ts TEXT)""")
    db.commit()


def _log(db, domain, url, level, status, nbytes, selector_hit, content_ok, note):
    if db is None:
        return
    db.execute("INSERT INTO fetch_attempts (domain, url, level, status, bytes, "
               "selector_hit, content_ok, note, ts) VALUES (?,?,?,?,?,?,?,?,?)",
               (domain, url, level, str(status), nbytes,
                None if selector_hit is None else int(selector_hit),
                int(content_ok), note,
                datetime.datetime.now().isoformat(timespec="seconds")))
    db.commit()


def has_content_signals(text: str | None, form_index: dict,
                        min_bytes: int = CONTENT_MIN_BYTES) -> bool:
    """Признак контента: цены ИЛИ названия услуг из справочника ИЛИ объём
    видимого текста не меньше порога (главная без цен — тоже контент).
    HTML сперва конвертируется в видимый текст (иначе словарь не совпадёт
    с разметкой, а разметка сойдёт за объём). 200 без признаков —
    suspicious_zero страницы, не взято."""
    if not text:
        return False
    from src.html_text import html_to_text
    visible = html_to_text(text)
    if PRICE_RE.search(visible):
        return True
    from src.mapper import normalize_service_name
    for line in visible.splitlines():
        s = line.strip(" -–—·|#*")
        if 4 <= len(s) <= 200 and normalize_service_name(s) in form_index:
            return True
    return len(visible.encode("utf-8")) >= min_bytes


# ── robots.txt: собственный матчер REP ────────────────────────────────────
# Стандартный urllib.robotparser читает Яндекс/Google-стиль правил
# («Disallow: /?», «Disallow: */?*») как запрет ВСЕГО сайта — ложная
# блокировка, доказано на alleya-nsk.ru и akriderm.com (2026-08-26).
# Здесь — семантика Google REP: * = подстановка, $ = конец, побеждает
# самое длинное правило, при равенстве Allow важнее Disallow.
def _parse_robots(text: str) -> list[tuple[str, str]]:
    """Правила группы User-agent: * → [(directive, pattern)]."""
    rules, in_star_group, seen_star = [], False, False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].replace("\xa0", " ").strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            in_star_group = (val == "*")
            seen_star = seen_star or in_star_group
        elif key in ("allow", "disallow") and in_star_group and val:
            rules.append((key, val))
    return rules


def _rule_matches(pattern: str, path: str) -> bool:
    regex = re.escape(pattern).replace(r"\*", ".*")
    if regex.endswith(r"\$"):
        regex = regex[:-2] + "$"
    return re.match(regex, path) is not None


def _robots_decision(rules: list[tuple[str, str]], path: str) -> bool:
    best_len, best_allow = -1, True
    for directive, pattern in rules:
        if _rule_matches(pattern, path):
            plen = len(pattern)
            allow = directive == "allow"
            if plen > best_len or (plen == best_len and allow):
                best_len, best_allow = plen, allow
    return best_allow


def robots_allows(url: str) -> bool:
    """robots.txt чтится на всех уровнях (Юрист OSINT). Недоступный robots.txt
    трактуется как разрешение (стандартная практика), запрет — как запрет."""
    m = re.match(r"(https?://[^/]+)(/.*)?$", url)
    if not m:
        return True
    base, path = m.group(1), m.group(2) or "/"
    if base not in _robots_cache:
        try:
            resp = httpx.get(f"{base}/robots.txt", timeout=10,
                             headers=BROWSER_HEADERS, follow_redirects=True)
            _robots_cache[base] = _parse_robots(resp.text) if resp.status_code == 200 else None
        except Exception:  # noqa: BLE001
            _robots_cache[base] = None
    rules = _robots_cache[base]
    return True if rules is None else _robots_decision(rules, path)


# ── Уровни ────────────────────────────────────────────────────────────────
def _level1_jina(url: str) -> tuple[str | None, str, int]:
    # Прогон 2026-08-26: Jina отдаёт 403 датацентровым IP GitHub Actions
    # (из песочницы — 200). Ключ JINA_API_KEY (бесплатный, jina.ai) поднимает
    # лимит и снимает блокировку; без ключа уровень честно падает на 403
    # и сайт берёт уровень 2 — каскад это и предусматривает.
    headers = {"User-Agent": BROWSER_UA}
    if os.environ.get("JINA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['JINA_API_KEY']}"
    try:
        r = httpx.get(f"https://r.jina.ai/{url}", timeout=60,
                      headers=headers, follow_redirects=True)
        return (r.text if r.status_code == 200 else None), str(r.status_code), len(r.content)
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__, 0


def _level2_direct(url: str) -> tuple[str | None, str, int]:
    time.sleep(1)
    try:
        r = httpx.get(url, timeout=30, headers=BROWSER_HEADERS, follow_redirects=True)
        return (r.text if r.status_code == 200 else None), str(r.status_code), len(r.content)
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__, 0


def _level3_headless(url: str) -> tuple[str | None, str, int, bool]:
    """networkidle + ожидание селектора + скролл до низа. Возвращает
    видимый текст страницы (построчный — его понимает extract_site)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            resp = page.goto(url, wait_until="networkidle", timeout=45000)
            selector_hit = False
            try:
                page.wait_for_selector("body", timeout=15000)
                selector_hit = True
            except Exception:  # noqa: BLE001
                pass
            page.mouse.wheel(0, 20000)   # скролл до низа — ленивая подгрузка
            page.wait_for_timeout(2000)
            text = page.inner_text("body")
            status = str(resp.status) if resp else "нет ответа"
            return text, status, len(text.encode("utf-8")), selector_hit
        finally:
            browser.close()


def _level4_emulation(url: str) -> tuple[str | None, str, int, bool]:
    """Полная эмуляция: реальный UA, viewport, языковые заголовки, случайные
    задержки, имитация движения курсора. Только чтение публичных страниц;
    CAPTCHA/логин не обходятся (правовой режим)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=BROWSER_UA, viewport={"width": 1366, "height": 768},
                locale="ru-RU", extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"})
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(1.0, 2.5))
            for _ in range(3):   # имитация движения курсора
                page.mouse.move(random.randint(100, 1200), random.randint(100, 700),
                                steps=random.randint(5, 15))
                time.sleep(random.uniform(0.3, 1.2))
            for _ in range(4):   # постепенный скролл
                page.mouse.wheel(0, random.randint(400, 900))
                time.sleep(random.uniform(0.4, 1.0))
            selector_hit = False
            try:
                page.wait_for_selector("body", timeout=15000)
                selector_hit = True
            except Exception:  # noqa: BLE001
                pass
            text = page.inner_text("body")
            status = str(resp.status) if resp else "нет ответа"
            return text, status, len(text.encode("utf-8")), selector_hit
        finally:
            browser.close()


def fetch_cascade(url: str, domain: str, form_index: dict,
                  db: sqlite3.Connection | None = None,
                  min_bytes: int = CONTENT_MIN_BYTES,
                  max_level: int = 4) -> tuple[str | None, dict]:
    """Одна страница через каскад. Возвращает (текст|None, итог попыток):
    {'level': взявший уровень|None, 'last_level', 'last_status', 'blocked_by_robots'}.
    """
    meta = {"level": None, "last_level": 0, "last_status": None,
            "blocked_by_robots": False}
    if not robots_allows(url):
        meta.update(blocked_by_robots=True, last_status="robots.txt запрещает")
        _log(db, domain, url, 0, "robots_disallow", 0, None, False,
             "обход запрещён robots.txt — сбор не выполняется (Юрист OSINT)")
        return None, meta

    runners = [(1, lambda: (*_level1_jina(url), None)),
               (2, lambda: (*_level2_direct(url), None)),
               (3, lambda: _level3_headless(url)),
               (4, lambda: _level4_emulation(url))]
    for level, run in runners:
        if level > max_level:
            break
        if level > 1:
            time.sleep(RATE_DELAY_SEC)
        try:
            text, status, nbytes, selector_hit = run()
        except ImportError:
            _log(db, domain, url, level, "playwright недоступен", 0, None, False,
                 "уровни 3-4 требуют playwright")
            meta.update(last_level=level, last_status="playwright недоступен")
            continue
        except Exception as exc:  # noqa: BLE001
            _log(db, domain, url, level, type(exc).__name__, 0, None, False,
                 str(exc)[:200])
            meta.update(last_level=level, last_status=type(exc).__name__)
            continue
        content_ok = text is not None and has_content_signals(text, form_index, min_bytes)
        note = None
        if text is not None and not content_ok:
            note = "suspicious_zero страницы: ответ есть, признаков контента нет — не взято"
        _log(db, domain, url, level, status, nbytes, selector_hit, content_ok, note)
        meta.update(last_level=level, last_status=status)
        if content_ok:
            meta["level"] = level
            return text, meta
    return None, meta
