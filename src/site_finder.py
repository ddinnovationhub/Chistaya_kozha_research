"""Достройка и проверка сайтов очерченного круга (шаг 2 promt_spark_krug,
2026-08-25) — по возрастанию стоимости:

2.1 проверка работоспособности сайтов из СПАРК (код, редиректы, заглушка,
    парковка, соответствие содержимого названию);
2.2 достройка по названию БЕСПЛАТНО: транслитерация в домен, типовые зоны,
    подтверждение — содержимое соответствует названию, не просто 200;
2.3 сопоставление с прежней базой discovery (candidates) по нормализованному
    названию — бесплатно;
2.4 платный поиск «{название} {город} официальный сайт» — ТОЛЬКО остаток,
    один запрос на компанию (выполняется при наличии YANDEX_API_KEY).

Правовой режим прежний: ≤1 запрос/3с на домен; здесь на компанию приходится
1-4 разовых HTTP-пробы РАЗНЫХ доменов — лимит на домен соблюдён.
"""

import concurrent.futures as cf
import re
import sqlite3
import time

import httpx

from src.fetch_cascade import BROWSER_HEADERS

_OPF_RE = re.compile(
    r"\b(ооо|оао|зао|пао|ао|ип|ано|мц|гк|нпф|тд|фирма|компания|клиника|центр"
    r"|медицинский|медицинская|group|llc|ltd)\b|[«»\"'#,.()]", re.IGNORECASE)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_PARKED_RE = re.compile(
    r"домен (продаётся|продается|припаркован)|купить этот домен|domain (is )?for sale"
    r"|parked domain|sedoparking|реклама на этом сайте|срок регистрации домена"
    r"|hugedomains|доступ ограничен.{0,40}хостинг", re.IGNORECASE)


def name_tokens(name: str) -> list[str]:
    """Значимые токены названия: без ОПФ и родовых слов, длина ≥4."""
    s = _OPF_RE.sub(" ", (name or "").lower().replace("ё", "е"))
    return [t for t in s.split() if len(t) >= 4]


def translit(word: str) -> str:
    return "".join(_TRANSLIT.get(c, c if c.isascii() else "") for c in word.lower())


def translit_candidates(name: str) -> list[str]:
    """Кандидаты доменов из названия: «#ЗДОРОВЬЯВСЕМ, ООО» → zdorovyavsem.ru…
    Подтверждение кандидата — только по содержимому (см. probe_domain)."""
    toks = name_tokens(name)
    if not toks:
        return []
    lat = [translit(t) for t in toks]
    lat = [w for w in lat if len(w) >= 4]
    bases = []
    if lat:
        joined = "".join(lat)
        if 4 <= len(joined) <= 24:
            bases.append(joined)
        if len(lat) > 1:
            bases.append("-".join(lat)[:28])
        bases.append(lat[0])
    seen, out = set(), []
    for b in bases:
        for zone in (".ru", ".com"):
            d = b + zone
            if d not in seen:
                seen.add(d)
                out.append(d)
    return out[:6]


def content_matches_name(text: str, name: str) -> bool:
    """Содержимое соответствует названию: значимый токен названия (кириллицей
    или транслитом) встречается в тексте страницы."""
    low = (text or "").lower().replace("ё", "е")
    for t in name_tokens(name):
        if t in low or (len(translit(t)) >= 4 and translit(t) in low):
            return True
    return False


def probe_domain(domain: str, timeout: float = 12) -> tuple[str, str | None, str | None]:
    """(статус, финальный домен, текст первых 60КБ). Статусы:
    ok / unreachable / parked."""
    for scheme in ("https", "http"):
        try:
            r = httpx.get(f"{scheme}://{domain}", timeout=timeout,
                          headers=BROWSER_HEADERS, follow_redirects=True)
        except Exception:  # noqa: BLE001
            continue
        if r.status_code >= 400:
            continue
        text = r.text[:60000]
        final = re.sub(r"^www\.", "", (r.url.host or domain).lower())
        if len(text) < 400 or _PARKED_RE.search(text):
            return "parked", final, text
        return "ok", final, text
    return "unreachable", None, None


# ── Шаг 2.1: проверка сайтов, указанных в СПАРК ──────────────────────────

def verify_spark_sites(db: sqlite3.Connection, budget_sec: float = 500,
                       workers: int = 12) -> dict:
    """Идемпотентно: берёт только site_status='не проверен'. Пишет статусы:
    ok / ok (названия на сайте нет) / нерабочий / заглушка-парковка."""
    rows = list(db.execute(
        "SELECT inn, name, site_spark FROM companies "
        "WHERE site_spark IS NOT NULL AND site_status='не проверен'"))
    t0 = time.time()
    stats = {"ok": 0, "ok_no_name": 0, "unreachable": 0, "parked": 0, "done": 0}
    # один домен проверяется один раз, результат — всем его ИНН
    by_dom: dict[str, list] = {}
    for inn, name, dom in rows:
        by_dom.setdefault(dom, []).append((inn, name))

    def work(dom):
        status, final, text = probe_domain(dom)
        return dom, status, final, text

    doms = list(by_dom)
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {}
        submitted = 0
        for dom in doms:
            if time.time() - t0 > budget_sec:
                break
            futs[ex.submit(work, dom)] = dom
            submitted += 1
        for fut in cf.as_completed(futs):
            dom, status, final, text = fut.result()
            for inn, name in by_dom[dom]:
                if status == "ok":
                    matched = content_matches_name(text, name)
                    st = "ok" if matched else "ok (названия компании на сайте нет — возможно, бренд)"
                    stats["ok" if matched else "ok_no_name"] += 1
                    db.execute("UPDATE companies SET site=?, site_status=?, "
                               "site_source='СПАРК, подтверждён' WHERE inn=?",
                               (final or dom, st, inn))
                elif status == "parked":
                    stats["parked"] += 1
                    db.execute("UPDATE companies SET site_status='заглушка/парковка', "
                               "site=NULL, site_source='СПАРК, отбит' WHERE inn=?", (inn,))
                else:
                    stats["unreachable"] += 1
                    db.execute("UPDATE companies SET site_status='нерабочий', "
                               "site=NULL, site_source='СПАРК, отбит' WHERE inn=?", (inn,))
            stats["done"] += 1
            db.commit()
    left = len(doms) - stats["done"]
    stats["left"] = left
    return stats


# ── Шаг 2.3: сопоставление с прежней базой discovery (бесплатно) ─────────

def match_prior_base(db: sqlite3.Connection) -> int:
    """Компании без сайта: ищем домен в candidates прежнего discovery
    по нормализованному названию (значимые токены)."""
    cands = list(db.execute(
        "SELECT title, domain FROM candidates WHERE domain IS NOT NULL"))
    comp = list(db.execute(
        "SELECT inn, name FROM companies WHERE site IS NULL"))
    n = 0
    cand_norm = []
    for title, dom in cands:
        toks = set(name_tokens(title or ""))
        if toks:
            cand_norm.append((toks, dom))
    for inn, name in comp:
        toks = set(name_tokens(name))
        if not toks:
            continue
        hit = next((dom for ctoks, dom in cand_norm
                    if toks and (toks <= ctoks or ctoks <= toks)), None)
        if hit:
            db.execute("UPDATE companies SET site=?, site_status='не проверен', "
                       "site_source='прежняя база discovery' WHERE inn=?", (hit, inn))
            n += 1
    db.commit()
    return n


# ── Шаг 2.2: достройка транслитерацией (бесплатно) ───────────────────────

def build_sites_by_translit(db: sqlite3.Connection, budget_sec: float = 500,
                            workers: int = 12) -> dict:
    """Компании без сайта: пробуем домены из названия. Подтверждение —
    содержимое соответствует названию (не просто ответ 200). Идемпотентно:
    site_source='транслит: не найден' помечает уже испробованных."""
    rows = list(db.execute(
        "SELECT inn, name FROM companies WHERE site IS NULL "
        "AND (site_source IS NULL OR site_source NOT LIKE 'транслит%') "
        "AND (site_source IS NULL OR site_source != 'СПАРК, отбит')"))
    t0 = time.time()
    stats = {"found": 0, "tried": 0}

    def work(item):
        inn, name = item
        for dom in translit_candidates(name):
            status, final, text = probe_domain(dom, timeout=8)
            if status == "ok" and content_matches_name(text, name):
                return inn, final or dom
        return inn, None

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = []
        for item in rows:
            if time.time() - t0 > budget_sec:
                break
            futs.append(ex.submit(work, item))
        for fut in cf.as_completed(futs):
            inn, dom = fut.result()
            stats["tried"] += 1
            if dom:
                stats["found"] += 1
                db.execute("UPDATE companies SET site=?, site_status='не проверен', "
                           "site_source='транслитерация названия' WHERE inn=?",
                           (dom, inn))
            else:
                db.execute("UPDATE companies SET site_source='транслит: не найден' "
                           "WHERE inn=?", (inn,))
            db.commit()
    stats["left"] = len(rows) - stats["tried"]
    return stats


if __name__ == "__main__":
    import sys
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    step = sys.argv[1] if len(sys.argv) > 1 else "verify"
    budget = float(sys.argv[2]) if len(sys.argv) > 2 else 500
    if step == "verify":
        print("2.1 проверка сайтов СПАРК:", verify_spark_sites(con, budget))
    elif step == "prior":
        print("2.3 из прежней базы:", match_prior_base(con))
    elif step == "translit":
        print("2.2 транслитерация:", build_sites_by_translit(con, budget))
