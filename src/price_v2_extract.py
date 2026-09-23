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
    r"basket|social|subscribe|"
    # карусели карточек врачей: «Ярощук М.С. … от 3000 ₽ Тургеневская»
    # (mcvrach.ru expert__item, kdmcenter.ru «Стаж - 9 лет …») — цена приёма
    # в карточке врача не строка прайса
    r"expert|doctor|vrach|staff|team|specialist|sotrudnik|employe", re.I)
BTN = re.compile(r"btn|button|order|zapis|callback|more|link-arrow", re.I)
UI_SUB = re.compile(
    r"оставить заявку|запис[аь]ться(\s+(на\s+)?при[её]м)?|онлайн[- ]запись|"
    r"запись онлайн|заказать звонок|обратный звонок|узнать цену|подробнее|"
    r"читать далее|смотреть все|показать все|показать скидку|показать цену|"
    r"скрыть скидку|в корзину|заказать|выбрать|читать полностью|развернуть|свернуть",
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
PRICEISH = re.compile(r"price|прайс|prais|ceny|цен|stoimost|стоимост|tarif", re.I)


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


LETTERS = re.compile(r"[а-яёa-z]", re.I)
EMB_PRICE = re.compile(r"\d{3,}\s*(?:₽|руб\.?|р\b)", re.I)
CUR_WORD = re.compile(r"\bруб\b|₽", re.I)
# одно-словные рубрики — это раздел прайса, а не позиция (clinic-sl.ru:
# «консультации» ×6 с разными ценами)
GENERIC_ONE = {
    "консультация", "консультации", "диагностика", "приём", "прием", "приемы",
    "услуга", "услуги", "лечение", "обследование", "обследования", "процедура",
    "процедуры", "анализы", "анализ", "исследования", "стоимость", "цена",
    "цены", "прочее", "другое", "разное", "акции", "скидки", "программы",
    "комплексы", "манипуляции", "операции", "инъекции", "аппаратные",
}
# медицинская/косметологическая лексика для доли «медицинскости» домена:
# прайс бухгалтерии или стройки (ekaterinburg.billprof.ru — «ООО УСН 15%»)
# не должен пройти ворота, каким бы чистым он ни был структурно
MED_LEX = re.compile(
    r"врач|доктор|консультац|при[её]м|осмотр|диагност|терап|хирург|дермат|"
    r"косметолог|кожи|кожн|лица|лицо|тел[ао]\b|волос|губ[ыа]?\b|шеи|живота|"
    r"спины|массаж|пилинг|чистк|инъекц|лазер|удален|уз[ди]\b|узи|экг|мрт|кт\b|"
    r"анализ|кровь|крови|мочи|мазок|соскоб|биопси|гистолог|цитолог|вакцин|"
    r"привив|капельниц|блокад|плазм|ботул|ботокс|диспорт|филлер|мезотерап|"
    r"биоревит|контурн|эпиляц|депиляц|склеротерап|физиотерап|рефлексотерап|"
    r"гинеколог|уролог|невролог|кардиолог|офтальмолог|лор\b|отоларинголог|"
    r"эндокринолог|гастроэнтеролог|педиатр|психиатр|психолог|психотерап|"
    r"стоматолог|зуб[ао]?в?\b|имплант|ортодонт|аллерголог|онколог|маммолог|"
    r"проктолог|флеболог|трихолог|подолог|ревматолог|пульмонолог|нефролог|"
    r"гематолог|инфекционист|венеролог|андролог|сомнолог|диетолог|нарколог|"
    r"логопед|остеопат|мануальн|иглоукалыв|гирудотерап|озонотерап|"
    r"новообразован|папиллом|бородав|родин[коа]|невус|кератом|липом|атером|"
    r"гемангиом|мозол|вросш|ноготь|ногт|рубц|шрам|акне|угр[еи]|купероз|"
    r"пигмент|растяжк|целлюлит|морщин|омоложен|лифтинг|подтяжк|коррекц|"
    r"склер|беремен|роды|родов|эко\b|икси|спермограмм|дуплекс|допплер|"
    r"рентген|флюорограф|денситометр|колоноскоп|гастроскоп|фгдс|эндоскоп|"
    r"реабилитац|лфк|справк|медкнижк|медосмотр|профосмотр|санаци|наркоз|"
    r"анестез|шприц|перевязк|швов|шов\b|дренаж|пункц|катетер|тейпирован|"
    r"чек-?ап|check-?up|скрининг|тест\b|панел[ьи]|гормон|витамин|ферритин|"
    r"глюкоз|холестерин|инсулин|антител|пцр|ифа\b|игх\b|"
    # зоны тела прайсов эпиляции/массажа и ногтевой сервис подологии
    r"подмышк|бикини|голен[ьие]|бедр[ао]|предплеч|ягодиц|декольте|скул|"
    r"подбородок|щ[её]к|лоб\b|переносиц|межбров|усик|бакенбард|зона\b|зоны\b|"
    r"маникюр|педикюр|медкомисс|медицинск|клиник|стопы|кист[ьи]\b|стоп\b",
    re.I)
# ФИО как «позиция» (annurclinic.ru: «Капралова Аделя Маратовна») — это
# карточка врача, не услуга; вдобавок 152-ФЗ: ФИО врачей не собираем
FIO_RE = re.compile(
    r"^[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+"
    r"[А-ЯЁ][а-яё]*(?:вна|ична|инична|евич|ович|ич|оглы|кызы)$")
JUNK_START = re.compile(r"^(?:на сайте|доступно|недоступно|опыт\b|стаж\b)", re.I)
# полное ФИО внутри строки — карточка врача, не услуга (и 152-ФЗ)
FIO_INNER = re.compile(
    r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]*(?:вна|ична|инична|евич|ович|ич|оглы|кызы)\b")
BREADCRUMB = re.compile(r"^\s*/|\bглавная\b.*/|/\s*$|^главная\b", re.I)


def valid_name(name):
    """Отсев мусорных «названий» (сверка 2026-09-23): последовательности цифр,
    склейки нескольких услуг в одну строку, цены внутри названия."""
    if len(LETTERS.findall(name)) < 5:
        return False                       # «2000 2000», «600 500»
    if sum(c.isdigit() for c in name) / max(len(name), 1) > 0.35:
        return False                       # преимущественно цифры
    if "●" in name or "•" in name:
        return False                       # склейка пунктов списка
    if EMB_PRICE.search(name):
        return False                       # цена другой позиции внутри названия
    if CUR_WORD.search(name):
        return False                       # «контракта руб. /» — обрезок строки цен
    if name.lower().strip(" .,;:–—-") in GENERIC_ONE:
        return False                       # рубрика раздела, не позиция
    if FIO_RE.match(name.strip()):
        return False                       # ФИО врача — не услуга (и 152-ФЗ)
    if FIO_INNER.search(name):
        return False                       # карточка врача с полным ФИО внутри
    if JUNK_START.match(name.strip()):
        return False                       # «на сайте», «Доступно», «Стаж…»
    if BREADCRUMB.search(name):
        return False                       # хлебные крошки «/ Главная … /»
    words = name.lower().split()
    if len(words) >= 4:                    # повтор начального фрагмента —
        head = " ".join(words[:2])         # конкатенация двух позиций
        if len(head) >= 12 and head in " ".join(words[2:]):
            return False
    return True


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


GENERIC_LBL = re.compile(
    r"цены?(\s+по\s+филиалам)?|стоимость(\s+услуг)?|прайс(-лист)?|price|₽|руб\.?",
    re.I)


def clean_text(txt):
    txt = UI_SUB.sub(" ", txt)
    txt = UI_STOP.sub(" ", txt)
    txt = re.sub(r"^\s*(?:₽|руб\.?|р\.?)\s+", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" .,;:–—-●•*")
    txt = re.sub(r"(?:\s+(?:руб\.?|₽|р\.)|\s+от)+$", "", txt, flags=re.I).rstrip(" .,;:–—-")
    # хвостовое голое число ≥4 знаков — приклеенная цена (зачёркнутая/вторая колонка);
    # дозировки препаратов («Диспорт 300») трёхзначны и не трогаются
    txt = re.sub(r"\s+\d{4,7}$", "", txt).rstrip(" .,;:–—-")
    txt = re.sub(r"\s+(?:Описание|Подробности)$", "", txt)
    txt = re.sub(r"\s*цены?\s+по\s+филиалам\s*$", "", txt, flags=re.I)
    # хвост «Врачи: Фамилия И.О., …» и любые «Фамилия И.О.» — не часть услуги,
    # и по 152-ФЗ ФИО врачей не собираем
    txt = re.sub(r"\s*врачи?:.*$", "", txt, flags=re.I)
    txt = re.sub(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.(?:\s*,)?", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" .,;:–—-")
    if txt.lower() in {"р", "руб", "₽", "от", "цена"}:
        return ""
    return txt


def clean_name(row, price_texts):
    txt = row.get_text(" ", strip=True)
    for pt in price_texts:
        txt = txt.replace(pt, " ")
    return clean_text(txt)


def row_name(row):
    """Название строки прайса структурно: самый длинный прямой потомок
    без цены внутри (аналог «колонки названий» в таблице). Спасает вёрстку,
    где строка содержит и название, и блок цен по филиалам (nika-nn.ru)."""
    best = ""
    for ch in row.find_all(True, recursive=False):
        t = ch.get_text(" ", strip=True)
        if t and not PRICE_RE.search(t) and len(t) > len(best):
            best = t
    return clean_text(best) if best else ""


def nearest_heading(node, cap=300):
    """Ближайший предшествующий заголовок (h1–h6/caption) — раздел прайса.
    «Верхняя губа» без раздела «Эпиляция лица» и «РАБОТНИКИ ЖКХ» без
    «Медосмотры» нечитаемы (заказчик, 2026-09-23): раздел тянется вместе
    со строкой, а не выбрасывается."""
    n = 0
    for prev in node.previous_elements:
        n += 1
        if n > cap:
            break
        if getattr(prev, "name", None) in ("h1", "h2", "h3", "h4", "h5", "h6",
                                           "caption"):
            t = clean_text(prev.get_text(" ", strip=True))
            if 3 <= len(t) <= 120 and not PRICE_RE.search(t):
                return t
    return ""


def extract_page(html):
    """→ список (название, цена, метод, раздел). Таблицы + паттерн-майнинг."""
    soup = BeautifulSoup(html, "lxml")
    for t in soup(KILL_TAGS):
        t.decompose()
    # зачистка зон — С ПРЕДОХРАНИТЕЛЯМИ (дефект 2026-09-23: класс темы на <body>
    # вида «ast-hfb-header … jet-mega-menu-location» сносил всю страницу;
    # «dropdown-menu-price» — контент прайса, а не навигация)
    page_len = len(soup.get_text()) or 1
    for t in soup.find_all(attrs={"class": KILL_ZONES}):
        if t.decomposed or t.name in ("body", "html", "main"):
            continue
        cls = " ".join(t.get("class", []))
        if PRICEISH.search(cls):
            continue
        if len(t.get_text()) > 0.4 * page_len:
            continue
        t.decompose()
    # незакрытый <header> (nika-nn.ru) заставляет lxml вложить в него всю
    # страницу — структурный тег с большей частью текста не сносится;
    # базу доли пересчитываем после зачистки зон
    page_len = len(soup.get_text()) or 1
    for t in soup.find_all(["header", "footer", "nav", "aside"]):
        if t.decomposed or len(t.get_text()) > 0.4 * page_len:
            continue
        t.decompose()
    out = []

    # --- ветка 1: таблицы
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 3:
            continue
        sec = nearest_heading(table)
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
            name = clean_text(max(cells[:pi], key=len, default=""))
            if len(name) >= 5 and not UI_STOP.match(name):
                got.append((name, price, "таблица", sec))
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
        while row is not None and hops < 6:
            txt = row.get_text(" ", strip=True)
            # длина содержательной части: без цен, служебных ярлыков («Цены по
            # филиалам») и UI-текста («Запись онлайн») — иначе подъём
            # останавливается, не дойдя до названия услуги (ekbclinic.ru)
            name_len = len(GENERIC_LBL.sub("", UI_SUB.sub("", PRICE_RE.sub("", txt))))
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
            name = row_name(row) or clean_name(row, [ptxt])
            if len(name) >= 5:
                out.append((name, price, f"паттерн:{len(items)}",
                            nearest_heading(row)))
    # дедуп в рамках страницы
    ded = {}
    for name, price, m, sec in out:
        ded.setdefault((name.lower(), price), (name, price, m, sec))
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
    med_share = sum(1 for n in names if MED_LEX.search(n)) / len(names)
    mt = {"позиций": len(items), "с ценой": round(with_price / len(items), 2),
          "UI-мусор": round(ui / len(items), 3), "мед. длина": med,
          "дубли": round(dup, 2), "мед. доля": round(med_share, 2)}
    # дубли ≤0.6, а не ≤0.15: после дедупа (название, цена) остаток «дублей» —
    # это одна услуга по разным ценам (филиалы/категории), легитимно (azmc.ru,
    # effi-clinic.ru срезались зря, дефект 2026-09-23).
    # мед. доля ≥0.2: домен, чей прайс не о медицине (бухуслуги на
    # ekaterinburg.billprof.ru), не проходит, какой бы чистой ни была структура
    ok = (with_price / len(items) >= 0.7 and ui / len(items) <= 0.02
          and med >= 15 and dup <= 0.6 and len(items) >= 5
          and med_share > 0.2)
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
                cur_sec = ""
                for row in ws.iter_rows(values_only=True):
                    cells = [c for c in row if c is not None]
                    if not cells:
                        continue
                    texts = [str(c).strip() for c in cells]
                    price = None
                    for t in reversed(texts):
                        price = parse_price(t)
                        if price:
                            break
                    if not price:
                        # строка без цены — бегущий заголовок раздела
                        t = clean_text(" ".join(texts))
                        if 5 <= len(t) <= 100:
                            cur_sec = t
                        continue
                    if len(cells) < 2:
                        continue
                    name = clean_text(max((t for t in texts if not parse_price(t)),
                                          key=len, default=""))
                    if len(name) >= 5 and not UI_STOP.match(name):
                        out.append((name, price, "xlsx", cur_sec))
        elif re.search(r"\.pdf($|\?)", url, re.I):
            import pdfplumber, io
            with pdfplumber.open(io.BytesIO(gzip.open(path, "rb").read())) as pdf:
                cur_sec = ""
                for pg in pdf.pages[:60]:
                    for tb in (pg.extract_tables() or []):
                        for row in tb:
                            cells = [str(c).strip() for c in row if c]
                            if not cells:
                                continue
                            price = None
                            pi = None
                            for i in range(len(cells) - 1, 0, -1):
                                price = parse_price(cells[i])
                                if price:
                                    pi = i
                                    break
                            if not price:
                                t = clean_text(" ".join(cells))
                                if 5 <= len(t) <= 100:
                                    cur_sec = t
                                continue
                            if len(cells) < 2:
                                continue
                            name = clean_text(max(cells[:pi], key=len, default=""))
                            if len(name) >= 5 and not UI_STOP.match(name):
                                out.append((name, price, "pdf", cur_sec))
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
            for n, p2, m, sec in extract_file(path, url):
                items_all.append((url, n, p2, m, sec))
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
        for n, p, m, sec in got:
            items_all.append((url, n, p, m, sec))
    ded = {}
    for url, n, p, m, sec in items_all:
        key = (n.lower(), p)
        if key not in ded or (not ded[key][4] and sec):
            ded[key] = (url, n, p, m, sec)
    items = [it for it in ded.values() if valid_name(it[1])]
    ok, mt = gates([(n, p, m) for _, n, p, m, _ in items])
    return items, ok, mt


def main(shard_file):
    con = sqlite3.connect(DB, timeout=60)
    con.execute("pragma journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS items_v2(
        domain TEXT, inn TEXT, url TEXT, name TEXT, price REAL, method TEXT)""")
    cols = [r[1] for r in con.execute("pragma table_info(items_v2)")]
    if "section" not in cols:
        con.execute("ALTER TABLE items_v2 ADD COLUMN section TEXT")
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
                con.executemany("insert into items_v2 values (?,?,?,?,?,?,?)",
                                [(domain, inns[domain], u, n, p, m, sec)
                                 for u, n, p, m, sec in items])
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
