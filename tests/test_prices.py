"""Прайс-каскад (2026-08-28): запах ссылок, мультипаттерновый парсер,
P0 из паспортов, чекпойнт рецептов. Конвейер test40 не затрагивается."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_link_scent_ladder():
    from src.prices import link_scent
    assert link_scent("Прайс-лист", "/upload/price2026.pdf") == 100
    assert link_scent("Скачать", "/docs/ceny.xlsx") == 100   # запах в href
    assert link_scent("Цены", "/uslugi/") == 80
    assert link_scent("Наши услуги", "/price/") == 70
    assert link_scent("Пациентам", "/patients/") == 30
    assert link_scent("Фото", "/gallery.jpg") == 0
    assert link_scent("Позвонить", "tel:+7495") == 0


def test_parse_emc_triplets():
    """Структура ЕМЦ: код / название / цена — соседними строками."""
    from src.prices import parse_price_text
    text = """АМБУЛАТОРНО-ПОЛИКЛИНИЧЕСКИЕ УСЛУГИ > ДЕРМАТОЛОГИЯ
DRMT7
Дерматоскопия - неинвазивная микроскопия кожи прибором DELTA20
124 у. е. / 12 437 руб.
DRMT9
Криохирургия (моллюски, бородавки и т.д.) 1 элемента
127 у. е. / 12 738 руб."""
    items = parse_price_text(text)
    assert len(items) == 2
    assert items[0]["code"] == "DRMT7"
    assert items[0]["name"].startswith("Дерматоскопия")
    assert items[0]["price_value"] == 12437
    assert "ДЕРМАТОЛОГИЯ" in items[0]["section"]


def test_parse_inline_and_ranges():
    from src.prices import parse_price_text
    items = parse_price_text("""Приём дерматолога первичный — 1 500 руб.
Удаление новообразований от 900 руб.
просто текст без цены""")
    assert len(items) == 2
    assert items[0]["price_value"] == 1500
    # вилка «от …» — дословно, значение не досчитывается
    assert items[1]["price_value"] is None
    assert "от 900" in items[1]["price_raw"]


def test_p0_passport_files_and_recipe_checkpoint():
    from src.prices import ensure_price_tables, p0_passport_files
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t40_companies (inn TEXT, found_site TEXT, "
               "passport TEXT)")
    passport = ("ПРАЙС-ФАЙЛЫ:\n  Прайс → /upload/price.pdf\n"
                "  Цены → https://clinic.ru/ceny.xlsx")
    db.execute("INSERT INTO t40_companies VALUES ('123', 'clinic.ru', ?)",
               (passport,))
    files = p0_passport_files(db, "123")
    assert "https://clinic.ru/upload/price.pdf" in files
    assert "https://clinic.ru/ceny.xlsx" in files
    ensure_price_tables(db)
    ensure_price_tables(db)          # идемпотентно
    db.execute("INSERT INTO price_recipes (domain, status) "
               "VALUES ('clinic.ru', 'прайс извлечён')")
    from src.prices import run_company
    res = run_company(db, "123", "clinic.ru")   # чекпойнт: без сети, скип
    assert res["skipped"] is True


def test_parse_xlsx_price(tmp_path):
    import openpyxl

    from src.prices import parse_price_file
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Дерматология"])
    ws.append(["Приём врача-дерматовенеролога", 1500])
    ws.append(["Дерматоскопия", 900.0])
    f = tmp_path / "p.xlsx"
    wb.save(f)
    items = parse_price_file(f.read_bytes(), "xlsx")
    assert len(items) == 2
    assert items[0]["name"].startswith("Приём")
    assert items[0]["price_value"] == 1500
    assert items[0]["section"] == "Дерматология"


def test_price_value_parsing():
    from src.prices import parse_price_value
    assert parse_price_value("12 437 руб.")[0] == 12437
    assert parse_price_value("1500 ₽")[0] == 1500
    assert parse_price_value("от 1 000 руб")[0] is None      # вилка
    assert parse_price_value("1000-2000 руб")[0] is None     # диапазон


def test_parse_html_tables_naked_prices():
    """Кейс azbuka-samara (заказчик, пачка 1): таблица «Услуга | Цена» с
    голыми числами без «руб»; телефоны не принимаются за цены."""
    from src.prices import parse_html_tables
    html = """<table>
    <tr><th>Услуга</th><th>Цена</th></tr>
    <tr><td>Прием дерматовенеролога первичный</td><td>1 200</td></tr>
    <tr><td>Удаление новообразований</td><td>от 200 до 800</td></tr>
    <tr><td>Регистратура</td><td>8 (846) 231-27-04</td></tr>
    </table>"""
    items = parse_html_tables(html)
    assert len(items) == 2                       # телефон отброшен
    assert items[0]["price_value"] == 1200
    assert items[1]["price_value"] is None       # вилка — дословно
    assert "от 200 до 800" in items[1]["price_raw"]


def test_open_dbs_separate_file_and_legacy_sync(tmp_path, monkeypatch):
    """Две базы (заказчик, 2026-09-02): прайсы пишутся в свою базу, osint.db
    присоединена только на чтение; записи обкатки из osint.db переносятся
    один раз, идемпотентно; повторное открытие ничего не дублирует."""
    import sqlite3

    from src import prices
    osint = tmp_path / "osint.db"
    o = sqlite3.connect(osint)
    o.execute("CREATE TABLE t40_companies (inn TEXT, found_site TEXT, passport TEXT)")
    o.execute("CREATE TABLE rzn_licenses (inn TEXT, is_med INTEGER, specialties TEXT)")
    o.execute("INSERT INTO t40_companies VALUES ('1','a.ru',NULL),('2','b.ru',NULL)")
    o.execute("INSERT INTO rzn_licenses VALUES ('1',1,'дерматовенерология'),('2',1,'косметология')")
    prices.ensure_price_tables(o)
    o.execute("INSERT INTO price_recipes VALUES ('a.ru','1','P3','прайс извлечён','u','[]','[]',1,2,'','2026-08-28')")
    o.execute("INSERT INTO price_items (inn, domain, url, section, code, name_raw, price_raw, "
              "price_value, currency, checked_at) VALUES ('1','a.ru','u','s','','Приём','100',100,'руб','2026-08-28')")
    o.commit(); o.close()
    pdb_path = tmp_path / "prices.db"
    db = prices.open_dbs(str(pdb_path), str(osint))
    assert prices.T40 == "o.t40_companies"
    assert db.execute("SELECT COUNT(*) FROM price_recipes").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM price_items").fetchone()[0] == 1
    assert prices.remaining(db) == 1                      # b.ru ещё не разобран
    db.close()
    db = prices.open_dbs(str(pdb_path), str(osint))       # повторно — без дублей
    assert db.execute("SELECT COUNT(*) FROM price_items").fetchone()[0] == 1
    # osint.db только на чтение: запись в схему o невозможна
    import pytest
    with pytest.raises(sqlite3.OperationalError):
        db.execute("INSERT INTO o.t40_companies VALUES ('3','c.ru',NULL)")
    prices.T40, prices.RZN = "t40_companies", "rzn_licenses"   # вернуть дефолт


def test_export_survives_null_bytes(tmp_path, monkeypatch):
    """Run 33618854799: нулевой байт \\x00 в name_raw уронил выгрузку целиком
    (IllegalCharacterError). Санация общая (src/xlsx_utils), мусор виден как «·»."""
    import sqlite3

    import openpyxl

    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("INSERT INTO price_recipes VALUES ('a.ru','1','P3','прайс извлечён',"
               "'u','[]','[]',1,1,'зам\x02етка','2026-09-02')")
    db.execute("INSERT INTO price_items (inn, domain, url, section, code, name_raw, "
               "price_raw, price_value, currency, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
               ("1", "a.ru", "u", "s", "", "Сыворотка \x00 со старением",
                "100", 100, "руб", "2026-09-02"))
    out = tmp_path / "прайсы.xlsx"
    monkeypatch.setattr(prices, "T40", "t40_companies")
    path = prices.export_prices(db, str(out))
    ws = openpyxl.load_workbook(path)["Позиции"]
    val = ws.cell(2, 5).value
    assert "Сыворотка" in val and "\x00" not in val and "·" in val


def test_price_number_real_formats():
    """Разбор 2026-09-04 (заказчик: «скорректировать выборку»): прежний
    шаблон не знал ни копеек, ни точки-разделителя тысяч — 10 368 позиций
    получили 0 ₽ при верной дословной цене, ещё 602 — обрезанную."""
    from src.prices import parse_price_value
    cases = [
        ("32040.00 ₽", 32040.0),      # duetclinic: копейки → был 0 ₽
        ("400,00 ₽", 400.0),          # копейки через запятую
        ("4 100.00 ₽", 4100.0),       # пробел-тысячи + копейки
        ("2.500 ₽", 2500.0),          # ks-lazer: точка-тысячи → было 500 ₽
        ("36.000₽", 36000.0),         # gladkospace → было 0 ₽
        ("368.200₽", 368200.0),       # dr-albrekht → было 200 ₽
        ("1 500,50 ₽", 1500.5),
        ("3 000 ₽", 3000.0),
        ("1005 ₽", 1005.0),
        ("350 руб.", 350.0),
        ("0 ₽", 0.0),                 # честный ноль на сайте остаётся нулём
    ]
    for raw, want in cases:
        assert parse_price_value(raw)[0] == want, raw
    # склейка двух цен в строке (euromednsk): берётся последняя, а не «2 030 500»
    assert parse_price_value("Ж 2 020 30 500 ₽")[0] == 30500.0
    # вилки по-прежнему не досчитываются
    assert parse_price_value("от 900 руб")[0] is None


def test_branch_address_is_not_service_name():
    """nika-nn.ru: блок «Цены по филиалам» — название услуги стоит выше,
    дальше пары «адрес → цена». Парсер писал адрес в название услуги
    (17 639 позиций из 18 955); теперь адрес идёт в раздел как филиал."""
    from src.prices import parse_price_text
    items = parse_price_text(
        "Кошка rFel d1 IgE\n2 100.00 ₽\nЦены по филиалам\nКошка rFel d1 IgE\n"
        "г. Арзамас, пр. Ленина, д. 135\n2500 ₽\n"
        "г. Кстово, ул. Лукерьинская, д. 1\n2100 ₽")
    assert [i["name"] for i in items] == ["Кошка rFel d1 IgE"] * 3
    assert items[1]["section"] == "филиал: г. Арзамас, пр. Ленина, д. 135"
    assert items[2]["price_value"] == 2100.0
    # обычная пара «название → цена» не задета
    plain = parse_price_text("Приём дерматолога первичный\n1 800 ₽")
    assert plain[0]["name"] == "Приём дерматолога первичный"
    assert plain[0]["price_value"] == 1800.0


def test_reparse_fixes_values_and_statuses(tmp_path):
    """reparse чинит готовую базу без выхода в сеть: пересчёт цен из
    дословной записи и расщепление P5 на три честных исхода."""
    import sqlite3

    from src.prices import ensure_price_tables, reparse
    db = sqlite3.connect(":memory:")
    ensure_price_tables(db)
    db.execute("INSERT INTO price_items (inn, domain, url, section, code, "
               "name_raw, price_raw, price_value, currency, checked_at) "
               "VALUES ('1','a.ru','u','','','Услуга','32040.00 ₽',0.0,"
               "'RUB','2026-09-03')")
    for dom, note in (("down.ru", "недоступна"), ("live.ru", "ok")):
        db.execute("INSERT INTO price_recipes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (dom, "1", "P5", "прайс не найден на дату проверки",
                    "", "[]", "[]", 0, 0, "", "2026-09-03"))
        db.execute("INSERT INTO price_nav_log VALUES (?,?,?,?,?,?)",
                   (dom, f"https://{dom}/", 0, 90, note, "2026-09-03"))
    res = reparse(db)
    assert res["цен пересчитано"] == 1
    assert db.execute("SELECT price_value FROM price_items").fetchone()[0] == 32040.0
    # недоступный сайт снимается с чекпойнта — каскад повторит попытку;
    # сайт, который открылся и прайса не дал, остаётся честным P5
    st = dict(db.execute("SELECT domain, status FROM price_recipes"))
    assert res["недоступных на повторную попытку"] == 1
    assert "down.ru" not in st
    assert st["live.ru"] == "прайс не найден на дату проверки"


def test_reparse_resets_domains_with_address_names():
    """Домены, где в названия попали адреса, теряют чекпойнт — название
    услуги в базе утеряно, нужен повторный разбор сайта новым парсером."""
    import sqlite3

    from src.prices import ensure_price_tables, reparse
    db = sqlite3.connect(":memory:")
    ensure_price_tables(db)
    db.execute("INSERT INTO price_recipes VALUES ('n.ru','1','P3:статика',"
               "'прайс извлечён','','[]','[]',1,6,'','2026-09-03')")
    for i in range(6):
        db.execute("INSERT INTO price_items (inn, domain, url, section, code, "
                   "name_raw, price_raw, price_value, currency, checked_at) "
                   "VALUES ('1','n.ru','u','','',?,'100 ₽',100.0,'RUB','d')",
                   (f"г. Кстово, ул. Лукерьинская, д. {i}",))
    db.execute("INSERT INTO price_recipes VALUES ('ok.ru','2','P3:статика',"
               "'прайс извлечён','','[]','[]',1,1,'','2026-09-03')")
    db.execute("INSERT INTO price_items (inn, domain, url, section, code, "
               "name_raw, price_raw, price_value, currency, checked_at) "
               "VALUES ('2','ok.ru','u','','','Приём дерматолога','900 ₽',"
               "900.0,'RUB','d')")
    res = reparse(db)
    assert res["доменов на повторный разбор"] == 1
    assert res["позиций удалено"] == 6
    doms = {r[0] for r in db.execute("SELECT domain FROM price_recipes")}
    assert doms == {"ok.ru"}          # чистый домен не тронут


def test_p4_runs_when_price_page_found_but_empty(monkeypatch, tmp_path):
    """Уровень P4 был описан в дизайне, но в коде отсутствовал: после
    статики и Jina сразу ставился статус «прайс не найден». У сайтов, где
    цены рисует JavaScript (Tilda, Bitrix, SPA), в сыром HTML нет ни одной
    цены — заказчик поймал это на 5pmedicina.ru и agk24.ru (2026-09-07).
    Разбор базы: навигатор открыл прайс-страницу у 225 доменов из 536
    «неудачных»."""
    import sqlite3

    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"

    # навигатор нашёл прайс-страницу, но статика с неё ничего не дала
    monkeypatch.setattr(prices, "navigate", lambda db, d, delay, **k: {
        "files": [], "price_pages": ["https://x.ru/price"],
        "route": [{"url": "https://x.ru/price", "label": "Цены", "depth": 1}],
        "pages_seen": 2, "reachable": True})
    monkeypatch.setattr(prices, "polite_get", lambda u, d: None)
    monkeypatch.setattr(prices, "p0_passport_files", lambda db, inn: [])

    calls = []

    def fake_browser(url, delay, **kw):
        calls.append(url)
        return ("<html><body>Приём дерматолога первичный 1 800 ₽"
                "<br>Удаление невуса 2 500 ₽</body></html>")

    monkeypatch.setattr(prices, "browser_render", fake_browser)
    res = prices.run_company(db, "77", "x.ru")

    assert calls == ["https://x.ru/price"]        # P4 вызван на найденной странице
    assert res["status"] == "прайс извлечён"
    assert res["level"] == "P4:браузер"
    names = [r[0] for r in db.execute("SELECT name_raw FROM price_items")]
    assert any("дерматолог" in n for n in names)


def test_found_page_without_prices_is_not_called_missing(monkeypatch):
    """Когда страница прайса открыта, а цен нет даже после рендера, это
    «не извлечено», а не «прайса нет» — разные вещи, и в таблице должны
    выглядеть по-разному (CLAUDE.md: «нет страницы ≠ нет услуги»)."""
    import sqlite3

    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"
    monkeypatch.setattr(prices, "navigate", lambda db, d, delay, **k: {
        "files": [], "price_pages": ["https://y.ru/ceny"],
        "route": [{"url": "https://y.ru/ceny", "label": "Цены", "depth": 1}],
        "pages_seen": 3, "reachable": True})
    monkeypatch.setattr(prices, "polite_get", lambda u, d: None)
    monkeypatch.setattr(prices, "p0_passport_files", lambda db, inn: [])
    monkeypatch.setattr(prices, "browser_render", lambda u, d, **k: "")

    res = prices.run_company(db, "88", "y.ru")
    assert res["status"] == "страница прайса найдена, цены не извлечены"


# --- РАЗВЕТВЛЁННЫЙ ПРАЙС (заказчик, 2026-09-07) ------------------------------
# «На сайтах, на которые я опирался, прайсы разветвлённые. Одно меню, и
# чтобы скачать всё — необходимо походить по страницам».

class _Resp:
    """Минимальный ответ вместо httpx.Response."""

    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = 200


def _category_html(n: int) -> str:
    return ("<html><body>"
            f"<h1>Раздел {n}</h1>"
            f"Приём дерматолога {n} 1 800 ₽<br>"
            f"Удаление невуса {n} 2 500 ₽<br>"
            f"Дерматоскопия {n} 900 ₽<br>"
            f"Криодеструкция {n} 1 200 ₽"
            "</body></html>")


def test_navigate_gives_price_branch_its_own_budget(monkeypatch):
    """Общий бюджет обхода — 12 страниц на весь сайт. Разветвлённое меню
    прайса из двадцати категорий в него не помещалось: в таблицу попадала
    одна категория из двадцати и называлась прайсом. Ветка прайса должна
    иметь СВОЙ бюджет."""
    import sqlite3

    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    monkeypatch.setattr(prices, "sitemap_price_urls", lambda d, delay: [])

    menu = "".join(f'<a href="/price/cat{i:02d}/">Раздел {i}</a>'
                   for i in range(1, 21))
    pages = {"https://d.ru/": '<html><a href="/price/">Цены</a></html>',
             "https://d.ru/price/": f"<html><body>{menu}</body></html>"}
    for i in range(1, 21):
        pages[f"https://d.ru/price/cat{i:02d}/"] = _category_html(i)

    monkeypatch.setattr(prices, "polite_get",
                        lambda u, d: _Resp(pages[u]) if u in pages else None)
    nav = prices.navigate(db, "d.ru", 0.0)

    # все двадцать категорий пройдены, хотя общий бюджет — 12 страниц
    assert len(nav["price_pages"]) == 20, nav["price_pages"]
    assert nav["branch_pages"] >= 20
    assert nav["pages_seen"] == 22          # главная + меню + 20 категорий


def test_p3_sums_prices_across_whole_branch(monkeypatch):
    """Позиции суммируются по ВСЕМ страницам меню (прежний код брал
    первые шесть и терял остальные)."""
    import sqlite3

    from src import fetch_cascade, prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"

    urls = [f"https://b.ru/price/cat{i:02d}/" for i in range(1, 13)]
    body = {u: _category_html(i) for i, u in enumerate(urls, 1)}
    monkeypatch.setattr(prices, "p0_passport_files", lambda db, inn: [])
    monkeypatch.setattr(prices, "navigate", lambda db, d, delay, **k: {
        "files": [], "price_pages": urls,
        "route": [{"url": u, "label": "Цены", "depth": 2} for u in urls],
        "pages_seen": 14, "branch_pages": 12, "reachable": True})
    monkeypatch.setattr(prices, "polite_get",
                        lambda u, d: _Resp(body[u]) if u in body else None)
    monkeypatch.setattr(fetch_cascade, "_level1_jina", lambda u: ("", "", ""))
    monkeypatch.setattr(prices, "browser_render", lambda u, d, **k: "")

    res = prices.run_company(db, "99", "b.ru")
    assert res["status"] == "прайс извлечён"
    assert res["items"] >= 40, res            # 12 разделов × 4 позиции
    doms = db.execute("SELECT COUNT(DISTINCT url) FROM price_items").fetchone()
    assert doms[0] == 12                      # каждая страница ветки в базе


def test_p4_follows_menu_drawn_by_script(monkeypatch):
    """Меню прайса, нарисованное скриптом, в сыром HTML отсутствует —
    ссылки на категории видны только после рендера. P4 обязан пройти по
    ним, иначе с разветвлённого JS-прайса берётся одна страница."""
    import sqlite3

    from src import fetch_cascade, prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"

    monkeypatch.setattr(prices, "p0_passport_files", lambda db, inn: [])
    monkeypatch.setattr(prices, "navigate", lambda db, d, delay, **k: {
        "files": [], "price_pages": ["https://z.ru/price/"],
        "route": [{"url": "https://z.ru/price/", "label": "Цены", "depth": 1}],
        "pages_seen": 2, "branch_pages": 1, "reachable": True})
    monkeypatch.setattr(prices, "polite_get", lambda u, d: None)
    monkeypatch.setattr(fetch_cascade, "_level1_jina", lambda u: ("", "", ""))

    rendered = []

    def fake_browser(url, delay, **kw):
        rendered.append(url)
        if url == "https://z.ru/price/":      # индекс: только меню, без цен
            return ('<html><body><a href="/price/derma/">Дерматология</a>'
                    '<a href="/price/trih/">Трихология</a>'
                    '<a href="/o-nas/">О нас</a></body></html>')
        return _category_html(len(rendered))

    monkeypatch.setattr(prices, "browser_render", fake_browser)
    res = prices.run_company(db, "111", "z.ru")

    assert rendered == ["https://z.ru/price/", "https://z.ru/price/derma/",
                        "https://z.ru/price/trih/"]   # «О нас» не ветка
    assert res["status"] == "прайс извлечён"
    assert res["items"] >= 8                  # обе категории, а не одна


def test_p2_sums_several_price_documents(monkeypatch):
    """Разветвлённый прайс выкладывают несколькими файлами — по одному на
    раздел. Прежнее правило «взять документ с наибольшим числом позиций»
    оставляло от такого прайса один раздел из нескольких."""
    import sqlite3

    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"

    files = ["https://f.ru/derma.pdf", "https://f.ru/trih.pdf"]
    monkeypatch.setattr(prices, "p0_passport_files", lambda db, inn: files)
    monkeypatch.setattr(prices, "polite_get", lambda u, d: _Resp("x"))

    def fake_parse(content, ext):
        n = fake_parse.calls = getattr(fake_parse, "calls", 0) + 1
        names = ([f"Дерма {i}" for i in range(1, 5)] if n == 1
                 else ["Дерма 4", "Трихология 1", "Трихология 2"])
        return [{"section": "", "code": "", "name": nm, "price_raw": "100 ₽",
                 "price_value": 100.0, "currency": "RUB"} for nm in names]

    monkeypatch.setattr(prices, "parse_price_file", fake_parse)
    res = prices.run_company(db, "222", "f.ru")

    assert res["level"] == "P2:документ"
    assert res["items"] == 6           # 4 + 3 − 1 дубль, а не «толще из двух»
    urls = {r[0] for r in db.execute("SELECT url FROM price_items")}
    assert urls == set(files)          # у позиции — свой документ-источник


def test_rerun_picks_branchy_and_failed(monkeypatch):
    """Перепрогон берёт неудачные домены и успешные с разветвлённым меню
    (две и более прайс-страницы в маршруте): прежний код суммировал
    максимум шесть страниц и такой прайс собирал частично."""
    import sqlite3

    from src.prices import ensure_price_tables, rerun_branch
    db = sqlite3.connect(":memory:")
    ensure_price_tables(db)
    route2 = ('[{"url":"https://a.ru/price/derma"},'
              ' {"url":"https://a.ru/price/trih"}]')
    route1 = '[{"url":"https://b.ru/price"}]'
    db.execute("INSERT INTO price_recipes VALUES ('a.ru','1','P3:статика',"
               "'прайс извлечён','u','[]',?,3,40,'','2026-09-05')", (route2,))
    db.execute("INSERT INTO price_recipes VALUES ('b.ru','2','P3:статика',"
               "'прайс извлечён','u','[]',?,1,12,'','2026-09-05')", (route1,))
    db.execute("INSERT INTO price_recipes VALUES ('c.ru','3','P5',"
               "'прайс не найден на дату проверки','','[]','[]',0,0,'','d')")

    res = rerun_branch(db, "all")
    assert res["на перепрогон: разветвлённые"] == 1     # a.ru
    assert res["на перепрогон: неудачные"] == 1         # c.ru
    st = dict(db.execute("SELECT domain, status FROM price_recipes"))
    assert st["a.ru"] == "в работе" and st["c.ru"] == "в работе"
    assert st["b.ru"] == "прайс извлечён"               # одностраничный не тронут
    prev = db.execute("SELECT prev_items FROM price_rerun "
                      "WHERE domain='a.ru'").fetchone()
    assert prev[0] == 40                                # прежний итог записан


def test_rerun_never_makes_result_worse(monkeypatch):
    """Если сайт с тех пор лёг, повторный проход не затирает прежний
    разбор своим нулём (CLAUDE.md: «прозрачная неполнота ценнее выдуманной
    полноты», но и терять доказанное нельзя)."""
    import sqlite3

    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"
    db.execute("INSERT INTO price_rerun VALUES ('old.ru','прайс извлечён',"
               "'P3:статика',40,'https://old.ru/price','2026-09-07')")
    db.execute("INSERT INTO price_recipes VALUES ('old.ru','7','P3:статика',"
               "'в работе','https://old.ru/price','[]','[]',3,40,'','2026-09-05')")
    db.execute("INSERT INTO price_items (inn, domain, url, section, code, "
               "name_raw, price_raw, price_value, currency, checked_at) "
               "VALUES ('7','old.ru','https://old.ru/price','','',"
               "'Приём дерматолога','900 ₽',900.0,'RUB','2026-09-05')")

    monkeypatch.setattr(prices, "p0_passport_files", lambda db, inn: [])
    monkeypatch.setattr(prices, "navigate", lambda db, d, delay, **k: {
        "files": [], "price_pages": [], "route": [], "pages_seen": 0,
        "branch_pages": 0, "reachable": False})
    monkeypatch.setattr(prices, "polite_get", lambda u, d: None)
    monkeypatch.setattr(prices, "browser_render", lambda u, d, **k: "")

    res = prices.run_company(db, "7", "old.ru")
    assert res["kept"] is True
    assert res["status"] == "прайс извлечён"
    assert db.execute("SELECT COUNT(*) FROM price_items "
                      "WHERE domain='old.ru'").fetchone()[0] == 1
    note = db.execute("SELECT note FROM price_recipes "
                      "WHERE domain='old.ru'").fetchone()[0]
    assert "прежний разбор сохранён" in note


# --- САНАЦИЯ РАЗМЕТКИ (заказчик, 2026-09-07, разбор «Выбросов») -------------

def test_sanitize_repairs_split_thousands():
    """Кейс leface.ru: имя «Введение Stylage S;16», цена «000 ₽; А11.01.013»
    — на странице стояло «16 000 ₽», разметка с «;» разорвала число."""
    from src.prices import sanitize_items
    clean, dropped = sanitize_items([
        {"section": "", "code": "", "name": "Введение Stylage S;16",
         "price_raw": "000 ₽; А11.01.013", "price_value": 0.0,
         "currency": "RUB"}])
    assert dropped == 0
    assert clean[0]["name"] == "Введение Stylage S"
    assert clean[0]["price_value"] == 16000.0
    assert clean[0]["price_raw"] == "16 000 ₽"


def test_sanitize_lead_time_is_not_a_price():
    """Кейс nmclinika.ru: колонки лаборатории «название · цена · срок».
    «1 р.д.» — срок готовности, не цена; настоящая цена прилипла к
    названию («…количеств. 390»)."""
    from src.prices import sanitize_items
    clean, dropped = sanitize_items([
        {"section": "", "code": "", "price_raw": "1 р.д.", "price_value": 1.0,
         "name": "Общий анализ крови количеств. 390", "currency": "RUB"},
        {"section": "", "code": "", "price_raw": "2 р.д.", "price_value": 2.0,
         "name": "Анализ без цены в разметке", "currency": "RUB"}])
    assert clean[0]["price_value"] == 390.0
    assert clean[0]["price_raw"] == "390"
    assert "390" not in clean[0]["name"]
    assert dropped == 1                    # без цены — брак, не «1 ₽»


def test_sanitize_drops_garbled_pdf_columns():
    """Кейс радугаздоровья.рф: PDF с посимвольной перемешкой колонок —
    «B01.003.00Т4.о0т0а9льная внутривенная анестезия». Название утеряно,
    ремонту не подлежит — позиция отбрасывается, а не пишется в таблицу."""
    from src.prices import sanitize_items
    clean, dropped = sanitize_items([
        {"section": "", "code": "", "price_value": 8500.0, "currency": "RUB",
         "name": "B01.003.00Т4.о0т0а9льная внутривенная анестезия",
         "price_raw": "8500,00 руб"},
        {"section": "", "code": "", "price_value": 2500.0, "currency": "RUB",
         "name": "Прием врача акушера-гинеколога первичный",
         "price_raw": "2500,00 руб"},
        # легитимные цифры в названии не считаются перемешкой
        {"section": "", "code": "", "price_value": 900.0, "currency": "RUB",
         "name": "УЗИ 2 зоны, витамин D3 и В12",
         "price_raw": "900 ₽"}])
    assert dropped == 1
    names = [c["name"] for c in clean]
    assert "Прием врача акушера-гинеколога первичный" in names
    assert "УЗИ 2 зоны, витамин D3 и В12" in names


def test_damaged_p2_document_falls_through_to_pages(monkeypatch):
    """Документ, где брака больше, чем чистого (перемешанный PDF), не
    глушит каскад: уцелевшее сохраняется, но за полным прайсом конвейер
    идёт на страницы сайта."""
    import sqlite3

    from src import fetch_cascade, prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"
    monkeypatch.setattr(prices, "p0_passport_files",
                        lambda db, inn: ["https://g.ru/price.pdf"])
    monkeypatch.setattr(prices, "parse_price_file", lambda content, ext: [
        {"section": "", "code": "", "name": "B01.00П2.р0и2ем в8р0ача",
         "price_raw": "0,00 руб", "price_value": 0.0, "currency": "RUB"},
        {"section": "", "code": "", "name": "A16.У0д7аление н5о0в0о",
         "price_raw": "00 руб", "price_value": 0.0, "currency": "RUB"},
        {"section": "", "code": "", "name": "Приём дерматолога",
         "price_raw": "1500 руб", "price_value": 1500.0, "currency": "RUB"}])
    pages = {"https://g.ru/price/": _category_html(1),
             "https://g.ru/price.pdf": "pdf-байты"}

    class _R(_Resp):
        pass
    monkeypatch.setattr(prices, "navigate", lambda db, d, delay, **k: {
        "files": [], "price_pages": ["https://g.ru/price/"], "dry_branch": [],
        "route": [{"url": "https://g.ru/price/", "label": "Цены", "depth": 1}],
        "pages_seen": 2, "branch_pages": 1, "reachable": True})
    monkeypatch.setattr(prices, "polite_get",
                        lambda u, d: _R(pages[u]) if u in pages else None)
    monkeypatch.setattr(fetch_cascade, "_level1_jina", lambda u: ("", "", ""))
    monkeypatch.setattr(prices, "browser_render", lambda u, d, **k: "")

    res = prices.run_company(db, "55", "g.ru")
    assert res["status"] == "прайс извлечён"
    names = [r[0] for r in db.execute("SELECT name_raw FROM price_items")]
    assert "Приём дерматолога" in names            # уцелевшее из документа
    assert any("Дерматоскопия" in n for n in names)  # добрано со страниц
    assert not any("У0д7" in n for n in names)     # брак не в таблице
    note = db.execute("SELECT note FROM price_recipes WHERE domain='g.ru'"
                      ).fetchone()[0]
    assert "брак разметки: 2" in note


def _mk_recipe(db, dom, status, items=0):
    db.execute("INSERT INTO price_recipes VALUES (?,?,?,?,'','[]','[]',1,?,"
               "'','2026-09-07')", (dom, "1", "P3", status, items))


def test_rerun_outliers_resets_fully(tmp_path):
    """Полный пересбор доменов с выбросами: рецепт, позиции, журнал
    навигатора и гвард удаляются — прогон идёт с чистого листа (заказчик:
    «новый прогон, а не обращение к уже полученным данным»)."""
    import sqlite3

    from src.prices import ensure_price_tables, rerun_branch
    db = sqlite3.connect(":memory:")
    ensure_price_tables(db)
    _mk_recipe(db, "bad.ru", "прайс извлечён", 2)
    _mk_recipe(db, "good.ru", "прайс извлечён", 1)
    for dom, val in (("bad.ru", 0.0), ("bad.ru", 900.0), ("good.ru", 900.0)):
        db.execute("INSERT INTO price_items (inn, domain, url, section, code,"
                   " name_raw, price_raw, price_value, currency, checked_at)"
                   " VALUES ('1',?,'u','','','Приём','x',?,'RUB','d')",
                   (dom, val))
    db.execute("INSERT INTO price_nav_log VALUES ('bad.ru','u',1,90,'ok','t')")
    db.execute("INSERT INTO price_rerun VALUES ('bad.ru','прайс извлечён',"
               "'P3',2,'','t')")
    res = rerun_branch(db, "outliers")
    assert res["на полный пересбор (выбросы)"] == 1
    doms = {r[0] for r in db.execute("SELECT domain FROM price_recipes")}
    assert doms == {"good.ru"}
    for t in ("price_items", "price_nav_log", "price_rerun"):
        assert db.execute(f"SELECT COUNT(*) FROM {t} WHERE domain='bad.ru'"
                          ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM price_items "
                      "WHERE domain='good.ru'").fetchone()[0] == 1


def test_sanitize_serial_numbers_are_not_prices():
    """Кейс altermedplus.ru: колонка «№» таблицы принята за цену —
    значения идут подряд 10, 11, 12… Номер строки ценой не записывается;
    настоящая цена в такой разметке утеряна → брак."""
    from src.prices import sanitize_items
    items = [{"section": "", "code": "", "name": f"Услуга номер {i}",
              "price_raw": str(v), "price_value": float(v), "currency": "RUB"}
             for i, v in enumerate([10, 11, 12, 13, 14, 15, 16])]
    items.append({"section": "", "code": "", "name": "Приём дерматолога",
                  "price_raw": "1800 ₽", "price_value": 1800.0,
                  "currency": "RUB"})
    clean, dropped = sanitize_items(items)
    assert dropped == 7
    assert [c["name"] for c in clean] == ["Приём дерматолога"]
    # короткая случайная пара (999, 1000) серией не считается
    clean2, dropped2 = sanitize_items([
        {"section": "", "code": "", "name": "А", "price_raw": "999",
         "price_value": 999.0, "currency": "RUB"},
        {"section": "", "code": "", "name": "Б", "price_raw": "1000",
         "price_value": 1000.0, "currency": "RUB"}])
    assert dropped2 == 0 and len(clean2) == 2


def test_sanitize_variant_glued_to_price():
    """Кейс euromednsk.ru: «ФДТ молочной железы, вариант» + «1 112 000 ₽»
    — это вариант 1 за 112 000 ₽, а не миллион."""
    from src.prices import sanitize_items
    clean, dropped = sanitize_items([
        {"section": "", "code": "оон.фдт.001", "currency": "RUB",
         "name": "ФДТ молочной железы, вариант",
         "price_raw": "1 112 000 ₽", "price_value": 1112000.0}])
    assert dropped == 0
    assert clean[0]["name"] == "ФДТ молочной железы, вариант 1"
    assert clean[0]["price_value"] == 112000.0
    assert clean[0]["price_raw"] == "112 000 ₽"


def test_sanitize_article_number_is_not_a_service():
    """Кейс samara.medguard.ru: «Артикул: 13468» — товарная карточка,
    название услуги осталось в другой ячейке. Такая позиция — брак."""
    from src.prices import sanitize_items
    clean, dropped = sanitize_items([
        {"section": "", "code": "", "name": "Артикул: 13468",
         "price_raw": "49 ₽", "price_value": 49.0, "currency": "RUB"},
        {"section": "", "code": "", "name": "Крем защитный",
         "price_raw": "490 ₽", "price_value": 490.0, "currency": "RUB"}])
    assert dropped == 1
    assert [c["name"] for c in clean] == ["Крем защитный"]


def test_damaged_p0_document_triggers_navigator(monkeypatch):
    """Кейс радугаздоровья.рф: файлы пришли из паспорта (P0), навигатор не
    запускался; документ оказался перемешанным PDF — добор со страниц шёл
    по слепым догадкам и давал 0. Теперь повреждённый документ запускает
    навигатор."""
    import sqlite3

    from src import fetch_cascade, prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("""CREATE TABLE t40_companies (inn TEXT, found_site TEXT,
                  row_no INTEGER, name TEXT)""")
    prices.T40 = "t40_companies"
    monkeypatch.setattr(prices, "p0_passport_files",
                        lambda db, inn: ["https://r.ru/p.pdf"])
    monkeypatch.setattr(prices, "parse_price_file", lambda content, ext: [
        {"section": "", "code": "", "name": "B01.У0д7аление н5о0в0о",
         "price_raw": "00 руб", "price_value": 0.0, "currency": "RUB"},
        {"section": "", "code": "", "name": "A16.П2р0и2ём в8р0ача",
         "price_raw": "0,00 руб", "price_value": 0.0, "currency": "RUB"}])
    nav_calls = []

    def fake_nav(db, d, delay, **k):
        nav_calls.append(d)
        return {"files": [], "price_pages": ["https://r.ru/ceny/"],
                "dry_branch": [], "route": [{"url": "https://r.ru/ceny/",
                                             "label": "Цены", "depth": 1}],
                "pages_seen": 3, "branch_pages": 1, "reachable": True}

    monkeypatch.setattr(prices, "navigate", fake_nav)
    pages = {"https://r.ru/p.pdf": "pdf", "https://r.ru/ceny/": _category_html(1)}
    monkeypatch.setattr(prices, "polite_get",
                        lambda u, d: _Resp(pages[u]) if u in pages else None)
    monkeypatch.setattr(fetch_cascade, "_level1_jina", lambda u: ("", "", ""))
    monkeypatch.setattr(prices, "browser_render", lambda u, d, **k: "")

    res = prices.run_company(db, "66", "r.ru")
    assert nav_calls == ["r.ru"]                  # навигатор запущен
    assert res["status"] == "прайс извлечён"
    names = [r[0] for r in db.execute("SELECT name_raw FROM price_items")]
    assert any("Дерматоскопия" in n for n in names)
