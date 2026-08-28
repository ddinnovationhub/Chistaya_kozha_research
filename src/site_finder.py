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
    r"\b(ооо|оао|зао|пао|ао|ип|ано|мц|гк|нпф|тд|фирма|компания|клиник\w*|центр\w*"
    r"|медицинск\w*|медицин\w*|медика|доктор\w*|врачебн\w*"
    r"|group|llc|ltd|clinic)\b|[«»\"'#,.()]", re.IGNORECASE)

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


# ── ТРОЙНАЯ ПРОВЕРКА связи «компания ↔ сайт» (разбор дефектов фазы 1,
# 2026-08-26, с корректировками заказчика) ────────────────────────────────
# Лестница признаков:
#   1. ИНН компании на сайте — сильнейший, однозначен (закон о ЗПП велит
#      публиковать; ищем на главной, /kontakty, /contacts, /rekvizity, /about);
#   2. АДРЕС организации в городе компании — только адрес в контакт-блоке
#      или подвале (город в связке с адресными маркерами), НЕ упоминание
#      города в тексте (сайт федеральной сети перечисляет все города —
#      корректировка заказчика №1); >3 городов на странице = федеральная
#      сеть, город перестаёт различать, подтверждение только по ИНН;
#   3. Название — НЕ признак вообще (источник исходного дефекта).

_ADDR_MARKER_RE = re.compile(
    r"\bг\.|\bул\.|улиц|просп|проезд|переул|шоссе|бульвар|офис|стр\.|корп"
    r"|\bадрес|обособленн", re.IGNORECASE)

_CITY_ROOTS = {
    "Уфа": ["уфа", "уфе", "уфы"], "Пермь": ["пермь", "перми"],
    "Самара": ["самар"], "Казань": ["казан"], "Красноярск": ["красноярск"],
    "Нижний Новгород": ["новгород"], "Тюмень": ["тюмен"],
    "Челябинск": ["челябинск"], "Ростов-на-Дону": ["ростов"],
    "Новосибирск": ["новосибирск"], "Екатеринбург": ["екатеринбург"],
    "Краснодар": ["краснодар"], "Воронеж": ["воронеж"], "Саратов": ["саратов"],
    "Махачкала": ["махачкал"], "Омск": ["омск"], "Барнаул": ["барнаул"],
    "Волгоград": ["волгоград"],
}
_ALL_CITY_ROOTS = sorted({r for v in _CITY_ROOTS.values() for r in v})

_CONTACT_PATHS = ("", "/kontakty", "/contacts", "/rekvizity", "/kontakty/",
                  "/o-nas", "/about")

# ── ГИБКАЯ НАВИГАЦИЯ (заказчик, 2026-08-27: «нужно ориентироваться на
# каждом сайте, а не тупо херачить по заранее подготовленным маршрутам»).
# Эмпирика scratchpad/flexible_probe_test.py: на 27 доменах пилота, где
# жёсткие пути дали НОЛЬ, выбор ссылок ПО ТЕКСТУ с главной подтвердил 8
# (+30%). Сайт сам говорит, где его контакты: тексты ссылок «контакты /
# реквизиты / о нас / политика конфиденциальности / оферта» ведут к
# страницам с ИНН и адресом независимо от структуры URL. ─────────────────

_CONTACT_LINK_RE = re.compile(
    r"контакт|реквизит|о нас|о компании|о клинике|о центре|о заводе"
    r"|политик|конфиденциальн|оферт|правовая информ|лиценз"
    r"|documents|contacts|about|privacy|requisit", re.IGNORECASE)


def flexible_contact_texts(domain: str, max_pages: int = 6,
                           pause: float = 3.0) -> list[str]:
    """Главная + страницы, выбранные по ТЕКСТУ ссылок главной (и по href как
    резерв). Пауза между запросами внутри домена — правовой режим ≤1/3с."""
    from urllib.parse import urljoin

    from src.html_text import _soup, looks_like_html

    def _get(url):
        try:
            r = httpx.get(url, timeout=10, headers=BROWSER_HEADERS,
                          follow_redirects=True)
            return r.text[:200000] if r.status_code < 400 and len(r.text) > 200 else None
        except Exception:  # noqa: BLE001
            return None

    home = _get(f"https://{domain}") or _get(f"http://{domain}")
    if not home:
        return []
    texts = [home]
    if not looks_like_html(home):
        return texts
    links, seen = [], set()
    for a in _soup(home).find_all("a", href=True):
        label = a.get_text(" ", strip=True) or ""
        if _CONTACT_LINK_RE.search(label) or _CONTACT_LINK_RE.search(a["href"]):
            u = urljoin(f"https://{domain}/", a["href"]).split("#")[0]
            if u not in seen and domain in u:
                seen.add(u)
                links.append(u)
    for u in links:
        if len(texts) >= max_pages:
            break
        time.sleep(pause)
        t = _get(u)
        if t:
            texts.append(t)
    return texts


# ── Адрес места деятельности ИЗ ЛИЦЕНЗИИ РЗН (заказчик, 2026-08-27):
# «улица + дом» из приложения лицензии, найденные на сайте, подтверждают
# связь компания↔сайт не хуже ИНН — у многих клиник ИНН на сайте нет,
# а адреса точек в контактах есть (кейс А2МЕД САМАРА). ─────────────────────

_LIC_STREET_RE = re.compile(
    r"(?:ул\.?|улица|просп\w*|пр-т|пер\.?|переулок|шоссе|ш\.|б-р|бульвар"
    r"|наб\.?|набережная|проезд|линия|тракт|дорога)\s*\.?\s*"
    r"([А-ЯЁ][А-ЯЁа-яё0-9\- .]{2,40}?)\s*,")
_LIC_HOUSE_RE = re.compile(r"\b(?:д\.?|дом)\s*(\d+\s*[а-яА-Я]?(?:\s*/\s*\d+)?)")


def license_addr_patterns(addresses: list[str]) -> list[tuple[str, str]]:
    """Адреса лицензии → пары (улица, дом) для поиска на сайте."""
    out, seen = [], set()
    for a in addresses:
        sm = _LIC_STREET_RE.search(a)
        hm = _LIC_HOUSE_RE.search(a)
        if not (sm and hm):
            continue
        street = sm.group(1).strip(" .").lower()
        house = re.sub(r"\s+", "", hm.group(1)).lower()
        if len(street) >= 4 and (street, house) not in seen:
            seen.add((street, house))
            out.append((street, house))
    return out


def license_addr_in_text(text: str, patterns: list[tuple[str, str]]
                         ) -> tuple[str, str] | None:
    """Первая пара «улица + дом» из лицензии, найденная на странице:
    улица дословно, номер дома в окне ±120 символов от неё."""
    low = text.lower()
    for street, house in patterns:
        for m in re.finditer(re.escape(street), low):
            window = low[max(0, m.start() - 120):m.end() + 120]
            h = house.replace("/", r"\s*/\s*")
            if re.search(rf"\b{h}\b", window):
                return street, house
    return None


def _addr_in_city(text: str, city_roots: list[str]) -> bool:
    """Город в СВЯЗКЕ с адресным маркером (окно ±150 символов) —
    признак адреса организации, а не упоминания города в тексте."""
    low = text.lower()
    for rt in city_roots:
        for m in re.finditer(re.escape(rt), low):
            window = low[max(0, m.start() - 150):m.end() + 150]
            if _ADDR_MARKER_RE.search(window):
                return True
    return False


def count_cities_mentioned(text: str) -> int:
    low = text.lower()
    return sum(1 for rt in _ALL_CITY_ROOTS if rt in low)


def fetch_contact_texts(domain: str) -> list[str]:
    """Главная + контактные/реквизитные страницы (до 3 удачных)."""
    texts = []
    for path in _CONTACT_PATHS:
        try:
            r = httpx.get(f"https://{domain}{path}", timeout=10,
                          headers=BROWSER_HEADERS, follow_redirects=True)
            if r.status_code < 400 and len(r.text) > 200:
                texts.append(r.text[:200000])
        except Exception:  # noqa: BLE001
            if path == "":
                break
        if len(texts) >= 3:
            break
    return texts


def triple_check(domain: str, inn: str, city: str,
                 pages_hint: list[str] | None = None,
                 license_addrs: list[str] | None = None,
                 license_numbers: list[str] | None = None) -> dict:
    """Проверка связи компания↔домен ПО ПРИОРИТЕТУ (заказчик, 2026-08-28,
    разбор пачки 1: azbuka-samara подтвердилась ДВУМ юрлицам, gastro74 —
    чужому, потому что нижняя ступень принимала «любой адрес в городе»):
      1. ИНН на сайте;
      2. НОМЕР ЛИЦЕНЗИИ на сайте (уникален как ИНН; номера уже в rzn_licenses);
      3. ПОЛНЫЙ адрес места деятельности из лицензии — улица И дом.
    Ступень «адрес в городе» УДАЛЕНА: город+похожее название — не
    доказательство. Возвращает {'verdict': 'ИНН'|'номер лицензии'|
    'адрес лицензии'|None, 'fed_network': bool, 'evidence': str,
    'reachable': bool}."""
    texts = pages_hint if pages_hint is not None else fetch_contact_texts(domain)
    if not texts:
        return {"verdict": None, "fed_network": False,
                "evidence": "сайт недоступен", "reachable": False}
    full = "\n".join(texts)
    if re.search(rf"(?<!\d){re.escape(inn)}(?!\d)", full):
        return {"verdict": "ИНН", "fed_network": False,
                "evidence": f"ИНН {inn} найден на сайте", "reachable": True}
    # ОТРИЦАТЕЛЬНЫЙ ПРИЗНАК (заказчик, 2026-08-27, кейс АДРЕМ→smitra.ru):
    # на сайте опубликованы реквизиты ДРУГОГО юрлица, нашего ИНН нет —
    # адресные подтверждения блокируются (соседняя клиника на той же улице
    # проходит по адресу; «пустая ячейка честнее неверной»)
    foreign = None
    for m in re.finditer(r"ИНН[:\s№]{0,4}(\d[\d\s]{8,12}\d)", full):
        digits = re.sub(r"\s", "", m.group(1))
        if len(digits) in (10, 12) and digits != inn:
            foreign = digits
            break
    if foreign:
        return {"verdict": None, "fed_network": False,
                "evidence": f"на сайте реквизиты другого юрлица (ИНН {foreign}"
                            f") — адресные признаки не применяются",
                "reachable": True}
    # приоритет 2: номер лицензии — уникальный идентификатор юрлица;
    # сравнение без пробелов (на сайтах пишут «Л041-01162 / 63-00347183»)
    if license_numbers:
        flat = re.sub(r"[\s ]", "", full).upper()
        for num in license_numbers:
            n = re.sub(r"[\s ]", "", num or "").upper()
            if len(n) >= 8 and n in flat:
                return {"verdict": "номер лицензии", "fed_network": False,
                        "evidence": f"номер лицензии {num} найден на сайте",
                        "reachable": True}
    if license_addrs:
        hit = license_addr_in_text(full, license_addr_patterns(license_addrs))
        if hit:
            return {"verdict": "адрес лицензии", "fed_network": False,
                    "evidence": f"адрес места деятельности из лицензии РЗН "
                                f"(«{hit[0]}, {hit[1]}») найден на сайте",
                    "reachable": True}
    fed = count_cities_mentioned(full) > 3
    if fed:
        return {"verdict": None, "fed_network": True,
                "evidence": "федеральная сеть (>3 городов на сайте) — "
                            "подтверждение только по ИНН, ИНН не найден",
                "reachable": True}
    # ступень «адрес в городе» удалена (заказчик, 2026-08-28): город плюс
    # похожее название подтверждали чужие сайты (azbuka-samara, gastro74)
    return {"verdict": None, "fed_network": False,
            "evidence": "ни ИНН, ни номер лицензии, ни полный адрес "
                        "лицензии (улица+дом) не найдены",
            "reachable": True}


def annul_translit_and_prior(db: sqlite3.Connection) -> dict:
    """Аннулирование name-based назначений (заказчик, 2026-08-26):
    650 транслитерации + 42 прежней базы. Сайт, суждения, позиции связи —
    в NULL; статус «сайт не найден» до поисковой достройки."""
    n = {"транслит": 0, "прежняя база": 0}
    for src_like, key in (("транслитерация названия", "транслит"),
                          ("прежняя база discovery", "прежняя база")):
        n[key] = db.execute(
            "UPDATE companies SET site=NULL, site_status=NULL, "
            "site_source='аннулирован: назначение по названию (дефект 1)', "
            "med_judgment=NULL, med_basis=NULL, profile_judgment=NULL, "
            "profile_matches_n=NULL, profile_matches=NULL, positions_seen=NULL, "
            "price_file_url=NULL, fetch_status='сайт не найден', "
            "fetch_level=NULL, pages_seen=NULL "
            "WHERE site_source=?", (src_like,)).rowcount
    db.commit()
    return n


def recheck_spark_sites(db: sqlite3.Connection, budget_sec: float = 500,
                        workers: int = 12) -> dict:
    """Тройная перепроверка СПАРК-сайтов (не аннулируются заранее —
    источник реестровый; получают грейд подтверждения). Идемпотентно:
    берёт только домены без грейда тройной проверки."""
    rows = list(db.execute(
        "SELECT inn, name, city, site FROM companies WHERE site IS NOT NULL "
        "AND site_source LIKE 'СПАРК%' "
        "AND site_status NOT LIKE 'подтверждён%' "
        "AND site_status NOT LIKE 'СПАРК, не подтверждён%'"))
    by_dom: dict[str, list] = {}
    for r in rows:
        by_dom.setdefault(r[3], []).append(r)
    t0 = time.time()
    stats = {"ИНН": 0, "адрес": 0, "не подтверждён": 0, "федеральная сеть": 0,
             "недоступен": 0, "done": 0}

    def work(dom):
        # домен скачивается один раз; каждая компания домена проверяется
        # отдельно по своим ИНН/городу (сети — легитимный случай)
        texts = fetch_contact_texts(dom)
        return dom, [(inn, triple_check(dom, inn, city, pages_hint=texts))
                     for inn, name, city, _d in by_dom[dom]]

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        doms = list(by_dom)
        chunk = workers * 3
        for i in range(0, len(doms), chunk):
            if time.time() - t0 > budget_sec:
                break
            futs = {ex.submit(work, d): d for d in doms[i:i + chunk]}
            for fut in cf.as_completed(futs):
                dom, results = fut.result()
                for inn, chk in results:
                    if not chk["reachable"]:
                        st, key = "СПАРК, не подтверждён (сайт недоступен при проверке)", "недоступен"
                    elif chk["verdict"] == "ИНН":
                        st, key = "подтверждён ИНН", "ИНН"
                    elif chk["verdict"] == "адрес":
                        st, key = "подтверждён адресом в городе", "адрес"
                    elif chk["fed_network"]:
                        st, key = "СПАРК, не подтверждён (федеральная сеть, ИНН не найден)", "федеральная сеть"
                    else:
                        st, key = "СПАРК, не подтверждён (ни ИНН, ни адрес)", "не подтверждён"
                    stats[key] += 1
                    db.execute("UPDATE companies SET site_status=? WHERE inn=?",
                               (st, inn))
                stats["done"] += 1
                db.commit()
    stats["left"] = len(by_dom) - stats["done"]
    return stats


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
    """[ВЫВЕДЕНА ИЗ ФЛОУ — решение заказчика 2026-08-26, разбор дефектов
    фазы 1]: вероятность ошибки — свойство метода (82 домена достались 218
    компаниям разных городов; название — не признак). Все её назначения
    аннулированы (annul_translit_and_prior). Замена — поисковая достройка
    src/search_sites.py с тройной проверкой ИНН/адрес. Код сохранён."""
    # «Нерабочий или чужой — пометить, отправить на достройку»: компании
    # с отбитым СПАРК-сайтом ('СПАРК, отбит') тоже достраиваются
    rows = list(db.execute(
        "SELECT inn, name FROM companies WHERE site IS NULL "
        "AND (site_source IS NULL OR site_source NOT LIKE 'транслит%')"))
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
                # probe уже подтвердил доступность и соответствие названию
                db.execute("UPDATE companies SET site=?, "
                           "site_status='ok (транслитерация, подтверждён содержимым)', "
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
