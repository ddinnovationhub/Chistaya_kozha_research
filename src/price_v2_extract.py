"""Пересбор прайсов, этап 2: DOM-экстрактор с рецептом на домен.

Идея (утверждена заказчиком 2026-09-24): вместо текстовой выжимки — разбор
HTML-структуры. Для домена автоматически подбирается «рецепт»: повторяющийся
DOM-паттерн строки прайса. Применяется код, а не суждение, поэтому все
страницы домена извлекаются одинаково. Ворота качества — на клинику.

Алгоритм подбора рецепта на странице:
  1) все текстовые узлы с ценой (число ≥ 2 знаков, рядом ₽/руб/р., или
     чистое число в ячейке таблицы/спане цены);
  2) для каждого — ближайший предок-«строка», содержащий и цену, и текст
     названия (≥ 12 символов без цены);
  3) сигнатура строки = путь тег.класс → группировка; паттерны с ≥ 4
     повторами на странице считаются прайс-строками;
  4) исключаются зоны header/footer/nav/aside/menu и узлы-кнопки;
  5) отдельная ветка — <table>: колонка названий + колонка цен.

Ворота качества (на домен): ≥70% позиций с ценой; UI-стоп-слов ≤2%;
медианная длина названия ≥15; дублей ≤15%. Не прошёл — статус
«прайс не извлечён надёжно», в базу мусор не пишется.
"""

import collections
import gzip
import os
import re
import sqlite3
import statistics
import sys

from bs4 import BeautifulSoup

CACHE = "data/price_html_cache"
DB = "data/price_v2.db"

KILL_TAGS = ["script", "style", "noscript", "svg", "iframe", "form"]
KILL_ZONES = re.compile(
    r"header|footer|nav|menu|breadcrumb|cookie|popup|modal|sidebar|widget-cart|"
    r"basket|social|subscribe", re.I)
BTN = re.compile(r"btn|button|order|zapis|callback|more|link-arrow", re.I)
UI_SUB = re.compile(
    r"оставить заявку|запис[аь]ться(\s+(на\s+)?при[её]м)?|онлайн[- ]запись|"
    r"запись онлайн|заказать звонок|обратный звонок|узнать цену|подробнее|"
    r"читать далее|смотреть все|показать все|в корзину|заказать|выбрать",
    re.I)
UI_STOP = re.compile(
    r"^(записаться|запись|заказать|подробнее|узнать|позвонить|смотреть|читать|"
    r"перейти|оставить заявку|вызвать|купить|в корзину|цена|стоимость|руб)\b|"
    r"запис[ьа]тьс|узнать цену|заказать звонок|обратный звонок|подробнее",
    re.I)
PRICE_RE = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[   ]\d{3})+|\d{3,7})(?:[.,]\d{2})?"
    r"\s*(?:₽|руб|р\.|р\b|rub)?", re.I)
CUR_HINT = re.compile(r"₽|руб|р\.|price|cena|цен|стоимост", re.I)


def parse_price(text):
    """Однозначная цена из короткого текста; иначе None (вилки не досчитываем)."""
    t = re.sub(r"[   ]", " ", text or "").strip()
    if len(t) > 40 or re.search(r"[%№]", t):
        return None
    ms = [m for m in PRICE_RE.finditer(t)]
    if len(ms) != 1:
        return None
    v = float(ms[0].group(1).replace(" ", "").replace(" ", ""))
    if not (100 <= v <= 5_000_000):
        return None
    if not (CUR_HINT.search(t) or t.replace(" ", "").replace(" ", "")
            .replace(ms[0].group(0).strip(), "") in ("", "от", "-", "—")):
        # число без валютного контекста допустимо, только если кроме него
        # в тексте ничего нет (ячейка цены)
        rest = PRICE_RE.sub("", t).strip(" .,–—-от")
        if rest:
            return None
    return v


def sig(node):
    parts = []
    cur = node
    for _ in range(3):
        if cur is None or cur.name is None:
            break
        cls = ".".join(sorted(cur.get("class", []))[:2])
        parts.append(f"{cur.name}.{cls}")
        cur = cur.parent
    return ">".join(parts)


def clean_name(row, price_texts):
    txt = row.get_text(" ", strip=True)
    for pt in price_texts:
        txt = txt.replace(pt, " ")
    txt = UI_SUB.sub(" ", txt)
    txt = UI_STOP.sub(" ", txt)
    txt = re.sub(r"^\s*(?:₽|руб\.?|р\.?)\s+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" .,;:–—-")
    if txt.lower() in {"р", "руб", "₽", "от", "цена"}:
        return ""
    return txt


def extract_page(html):
    """→ список (название, цена, метод). Таблицы + паттерн-майнинг."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(KILL_TAGS):
        t.decompose()
    for t in soup.find_all(attrs={"class": KILL_ZONES}):
        t.decompose()
    for t in soup.find_all(["header", "footer", "nav", "aside"]):
        t.decompose()
    out = []

    # --- ветка 1: таблицы
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        got = []
        for tr in rows:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            price = None
            pi = None
            for i in range(len(cells) - 1, 0, -1):
                price = parse_price(cells[i])
                if price is not None:
                    pi = i
                    break
            if price is None:
                continue
            name = max(cells[:pi], key=len, default="")
            name = re.sub(r"\s+", " ", name).strip()
            if len(name) >= 5 and not UI_STOP.match(name):
                got.append((name, price, "таблица"))
        if len(got) >= 3:
            out.extend(got)
        for tr in rows:
            tr.decompose()

    # --- ветка 2: паттерн-майнинг повторяющихся строк
    cand = collections.defaultdict(list)
    for el in soup.find_all(string=PRICE_RE):
        ptxt = str(el).strip()
        price = parse_price(ptxt)
        if price is None:
            continue
        row = el.parent
        hops = 0
        while row is not None and hops < 4:
            txt = row.get_text(" ", strip=True)
            name_len = len(PRICE_RE.sub("", txt))
            if name_len >= 12 and len(txt) < 500:
                break
            row = row.parent
            hops += 1
        if row is None or row.name in (None, "body", "html"):
            continue
        if BTN.search(" ".join(row.get("class", []))):
            continue
        cand[sig(row)].append((row, ptxt, price))
    for signature, items in cand.items():
        if len(items) < 4:
            continue
        seen_rows = set()
        for row, ptxt, price in items:
            if id(row) in seen_rows:
                continue
            seen_rows.add(id(row))
            name = clean_name(row, [ptxt])
            if len(name) >= 5:
                out.append((name, price, f"паттерн:{len(items)}"))
    # дедуп в рамках страницы
    ded = {}
    for name, price, m in out:
        ded.setdefault((name.lower(), price), (name, price, m))
    return list(ded.values())


def gates(items):
    """Метрики качества по домену → (ок?, dict метрик)."""
    if not items:
        return False, {"позиций": 0}
    names = [n for n, p, m in items]
    with_price = sum(1 for n, p, m in items if p)
    ui = sum(1 for n in names if UI_STOP.search(n))
    med = statistics.median([len(n) for n in names])
    dup = 1 - len({n.lower() for n in names}) / len(names)
    mt = {"позиций": len(items), "с ценой": round(with_price / len(items), 2),
          "UI-мусор": round(ui / len(items), 3), "мед. длина": med,
          "дубли": round(dup, 2)}
    ok = (with_price / len(items) >= 0.7 and ui / len(items) <= 0.02
          and med >= 15 and dup <= 0.15 and len(items) >= 5)
    return ok, mt


def extract_file(path, url):
    """PDF/XLSX прайс → строки (название, цена). Только однозначные цены."""
    out = []
    try:
        if re.search(r"\.xlsx?($|\?)", url, re.I):
            import openpyxl, io
            wb = openpyxl.load_workbook(io.BytesIO(gzip.open(path, "rb").read()),
                                        read_only=True, data_only=True)
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [c for c in row if c is not None]
                    if len(cells) < 2:
                        continue
                    texts = [str(c).strip() for c in cells]
                    price = None
                    for t in reversed(texts):
                        price = parse_price(t)
                        if price:
                            break
                    if not price:
                        continue
                    name = max((t for t in texts if not parse_price(t)),
                               key=len, default="")
                    if len(name) >= 5 and not UI_STOP.match(name):
                        out.append((name, price, "xlsx"))
        elif re.search(r"\.pdf($|\?)", url, re.I):
            import pdfplumber, io
            with pdfplumber.open(io.BytesIO(gzip.open(path, "rb").read())) as pdf:
                for pg in pdf.pages[:60]:
                    for tb in (pg.extract_tables() or []):
                        for row in tb:
                            cells = [str(c).strip() for c in row if c]
                            if len(cells) < 2:
                                continue
                            price = None
                            pi = None
                            for i in range(len(cells) - 1, 0, -1):
                                price = parse_price(cells[i])
                                if price:
                                    pi = i
                                    break
                            if not price:
                                continue
                            name = max(cells[:pi], key=len, default="")
                            name = re.sub(r"\s+", " ", name)
                            if len(name) >= 5 and not UI_STOP.match(name):
                                out.append((name, price, "pdf"))
    except Exception:
        return out
    return out


def run_domain(domain, con):
    rows = con.execute(
        "select url, sha1 from pages_v2 where domain=? and kind='html' and status=200",
        (domain,)).fetchall()
    frows = con.execute(
        "select url, sha1 from pages_v2 where domain=? and kind='file' and status=200",
        (domain,)).fetchall()
    items_all = []
    for url, sha in frows:
        path = f"{CACHE}/{domain}/{sha}.gz"
        if os.path.exists(path):
            for n, p2, m in extract_file(path, url):
                items_all.append((url, n, p2, m))
    per_page = {}
    for url, sha in rows:
        path = f"{CACHE}/{domain}/{sha}.gz"
        if not os.path.exists(path):
            continue
        try:
            html = gzip.open(path, "rb").read().decode("utf-8", "ignore")
        except Exception:
            continue
        got = extract_page(html)
        per_page[url] = len(got)
        for n, p, m in got:
            items_all.append((url, n, p, m))
    ded = {}
    for url, n, p, m in items_all:
        ded.setdefault((n.lower(), p), (url, n, p, m))
    items = list(ded.values())
    ok, mt = gates([(n, p, m) for _, n, p, m in items])
    return items, ok, mt


def main(shard_file):
    con = sqlite3.connect(DB, timeout=60)
    con.execute("pragma journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS items_v2(
        domain TEXT, inn TEXT, url TEXT, name TEXT, price REAL, method TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS extract_v2(
        domain TEXT PRIMARY KEY, inn TEXT, items INTEGER, gate_ok INTEGER,
        metrics TEXT, done_at TEXT)""")
    con.commit()
    inns = {d: i for d, i in con.execute("select domain, inn from crawl_v2")}
    todo = [l.strip() for l in open(shard_file, encoding="utf-8")
            if l.strip() and l.strip() in inns]
    done = {r[0] for r in con.execute("select domain from extract_v2")}
    todo = [d for d in todo if d not in done]
    import json
    for k, domain in enumerate(todo, 1):
        try:
            items, ok, mt = run_domain(domain, con)
            con.execute("delete from items_v2 where domain=?", (domain,))
            if ok:
                con.executemany("insert into items_v2 values (?,?,?,?,?,?)",
                                [(domain, inns[domain], u, n, p, m)
                                 for u, n, p, m in items])
            con.execute("insert or replace into extract_v2 values (?,?,?,?,?,datetime('now'))",
                        (domain, inns[domain], len(items), int(ok),
                         json.dumps(mt, ensure_ascii=False)))
            con.commit()
        except Exception as e:                            # noqa: BLE001
            print(domain, "ошибка:", type(e).__name__, e, flush=True)
        if k % 25 == 0:
            print(f"  извлечение {k}/{len(todo)}", flush=True)
    print("экстракция готова:", shard_file, flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
