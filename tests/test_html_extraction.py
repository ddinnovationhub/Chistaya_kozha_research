"""Третий промпт исправления (2026-08-26): HTML → чистый текст через DOM,
валидация названий, ворота ДО записи услуг, robots-матчер."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extract_site import extract_pages, is_clean_name  # noqa: E402
from src.fetch_cascade import _parse_robots, _robots_decision  # noqa: E402
from src.html_text import html_to_text, site_name_from_html  # noqa: E402
from src.mapper import build_formulation_index  # noqa: E402

FORM_INDEX = build_formulation_index()

HTML = """<!DOCTYPE html><html><head><title>Аллергологи в Новосибирске — МЦ</title>
<meta property="og:site_name" content="Клиника Пример"></head><body>
<header><a href="/"><img src="l.png" alt="Клиника Пример"></a></header>
<script>var x = "Мусор из скрипта 999 ₽";</script>
<div class="tabAccordionContent"><p><span class="">Иммунизация вакциной &#8211; Превенар</span> — 3 500 ₽</p></div>
<table><tr><td>Дерматоскопия</td><td>1 200 ₽</td></tr>
<tr><td>Удаление папилломы лазером</td><td>900 ₽</td></tr></table>
<p>Приём дерматолога</p>
</body></html>"""


def test_html_to_text_strips_markup_and_decodes_entities():
    text = html_to_text(HTML)
    assert "<" not in text and "class=" not in text and "&#" not in text
    assert "Иммунизация вакциной – Превенар" in text   # &#8211; → –
    assert "Мусор из скрипта" not in text              # script выброшен
    assert "Дерматоскопия | 1 200 ₽" in text           # таблица → «a | b»


def test_extracted_names_are_clean():
    """ТЕСТ заказчика (п.2): ни одно название не содержит < > class= span div &#."""
    d = extract_pages({"https://x.ru/": HTML}, FORM_INDEX)
    assert d["services"], "экстракция не дала ни одной услуги из живого HTML"
    for s in d["services"]:
        assert is_clean_name(s["name"]), s["name"]
    names = [s["name"] for s in d["services"]]
    assert "Дерматоскопия" in names
    assert any("Иммунизация вакциной" in n for n in names)


def test_dirty_name_is_rejected_not_written():
    assert not is_clean_name('<span class="" Иммунизация')
    assert not is_clean_name("текст с &#8211; сущностью")
    assert is_clean_name("Иммунизация вакциной – Превенар")


def test_site_name_priority():
    """п.4: og:site_name → шапка; title — не берётся автоматически."""
    name, src = site_name_from_html(HTML)
    assert name == "Клиника Пример" and src == "og:site_name"
    no_og = HTML.replace('<meta property="og:site_name" content="Клиника Пример">', "")
    name, src = site_name_from_html(no_og)
    assert name == "Клиника Пример" and "шапка" in src


def test_gates_before_service_write():
    """ТЕСТ заказчика (п.1): клиника без профильных маркеров → Исключён,
    ноль строк в services_found."""
    from src.site_checker import ensure_stage6_tables, process_clinic
    import src.site_checker as sc
    from src.classify import load_contours

    allergo = """<html><head><title>Аллерго+</title></head><body>
    <p>Приём аллерголога-иммунолога</p><p>Лицензия ЛО-54-01-000001</p>
    <p>Иммунизация вакциной Превенар — 3 500 ₽</p>
    <p>Спирография — 800 ₽</p><p>ЭКГ — 500 ₽</p>
    <p>Справка в бассейн — 400 ₽</p></body></html>"""

    db = sqlite3.connect(":memory:")
    ensure_stage6_tables(db)
    orig = sc.crawl_site
    sc.crawl_site = lambda *a, **k: ({"https://allergo.test/": allergo},
                                     {"level": 2, "last_level": 2,
                                      "last_status": "200", "blocked_by_robots": False})
    try:
        r = process_clinic({"title": "Аллерго+", "url": "https://allergo.test",
                            "domain": "allergo.test"}, db, load_contours(),
                           FORM_INDEX, set(), "Тест")
    finally:
        sc.crawl_site = orig
    assert r["gate"] == "Исключён"
    row = db.execute("SELECT gate_reason FROM clinics").fetchone()
    assert "нет релевантного профиля" in row[0]
    assert "аллерголог" in row[0]   # фактический профиль указан
    assert db.execute("SELECT COUNT(*) FROM services_found").fetchone()[0] == 0


def test_robots_wildcard_rules_do_not_block_root():
    """Ложная блокировка urllib.robotparser (alleya-nsk.ru, akriderm.com):
    «Disallow: /?» и «Disallow: */?*» запрещают query-string, не главную."""
    rules = _parse_robots("User-agent: *\nDisallow: /?\nDisallow: */?*\nDisallow: /wp-\n")
    assert _robots_decision(rules, "/") is True
    assert _robots_decision(rules, "/uslugi/") is True
    assert _robots_decision(rules, "/?s=query") is False
    assert _robots_decision(rules, "/page/?utm=1") is False
    assert _robots_decision(rules, "/wp-admin") is False


def test_robots_full_disallow_still_blocks():
    rules = _parse_robots("User-agent: *\nDisallow: /\n")
    assert _robots_decision(rules, "/") is False
    rules_allow = _parse_robots("User-agent: *\nDisallow: /\nAllow: /uslugi\n")
    assert _robots_decision(rules_allow, "/uslugi/price") is True


def _gate_for(page_html, domain="x.test", title="X"):
    from src.site_checker import ensure_stage6_tables, process_clinic
    import src.site_checker as sc
    from src.classify import load_contours
    db = sqlite3.connect(":memory:")
    ensure_stage6_tables(db)
    orig = sc.crawl_site
    sc.crawl_site = lambda *a, **k: ({f"https://{domain}/": page_html},
                                     {"level": 2, "last_level": 2,
                                      "last_status": "200", "blocked_by_robots": False})
    try:
        r = process_clinic({"title": title, "url": f"https://{domain}",
                            "domain": domain}, db, load_contours(), FORM_INDEX,
                           set(), "Тест")
    finally:
        sc.crawl_site = orig
    rows = db.execute("SELECT COUNT(*) FROM services_found").fetchone()[0]
    return r, rows


def test_stoplist_pharma_site_excluded():
    """п.1: сайт мази — производитель препарата, не клиника (кейс akriderm)."""
    page = """<html><body><p>Акридерм — инструкция по применению</p>
    <p>Действующее вещество: бетаметазон</p><p>Дерматолог рекомендует</p>
    <p>Дерматит: лечение — 500 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Исключён" and "производитель" in r["reason"]
    assert rows == 0


def test_stoplist_salon_excluded():
    """п.1: салон красоты — «косметолог» в тексте не делает его клиникой (кейс albane)."""
    page = """<html><body><p>Салон: стрижка, укладка, маникюр, педикюр</p>
    <p>Наш косметолог ждёт вас</p><p>Чистка лица — 2 000 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Исключён" and "салон" in r["reason"]
    assert rows == 0


def test_lab_without_doctor_excluded():
    """п.1: лаборатория без приёма врача (кейс alab54)."""
    page = """<html><body><p>Лицензия ЛО-54-01-000001</p>
    <p>Анализ волос на микроэлементы — 3 000 ₽</p>
    <p>Микроскопическое исследование соскоба — 900 ₽</p>
    <p>ПЦР на дерматофиты — 1 200 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Исключён" and "лаборатория" in r["reason"]
    assert rows == 0


def test_doctor_visit_in_price_lifts_stoplist():
    """Медцентр с парикмахерской зоной: приём врача в прайсе снимает стоп-лист."""
    page = """<html><body><p>Стрижка и укладка</p>
    <p>Приём врача-дерматолога первичный — 1 800 ₽</p>
    <p>Дерматоскопия — 1 200 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Включён"
    assert rows >= 2


def test_g1_word_kosmetolog_not_enough():
    """п.1: слова «косметолог» в тексте недостаточно — нужен приём в прайсе или лицензия."""
    page = """<html><body><p>Наши косметологи лучшие в городе</p>
    <p>Пилинг — 1 500 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Исключён"
    assert rows == 0


def test_nonprofile_services_not_collected():
    """п.6: вакцинация, ЭКГ, справки, несмежные — в таблицу не попадают."""
    page = """<html><body><p>Лицензия ЛО-54-01-000001</p>
    <p>Приём врача-дерматолога — 1 500 ₽</p>
    <p>Вакцинация от гриппа — 900 ₽</p><p>ЭКГ с расшифровкой — 700 ₽</p>
    <p>Справка в бассейн — 400 ₽</p><p>Приём гинеколога — 1 800 ₽</p>
    <p>Дерматоскопия — 1 200 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Включён"
    assert r["skipped_nonprofile"] >= 4
    assert rows == r["services"] == 2   # только дерматологические строки


def test_esthetic_collected_as_aggregate_not_rows():
    """Эстетика — ОДНОЙ агрегатной строкой на клинику (мера 2, заказчик
    2026-08-26: свернуть, не выбросить), не построчно."""
    page = """<html><body><p>Приём врача-дерматолога — 1 500 ₽</p>
    <p>Ботулинотерапия — 4 500 ₽</p><p>Биоревитализация — 5 000 ₽</p>
    <p>Дерматоскопия — 1 200 ₽</p></body></html>"""
    r, rows = _gate_for(page)
    assert r["gate"] == "Включён"
    assert r["skipped_esthetic"] >= 2
    assert rows == 3   # 2 дерматологические + 1 агрегат эстетики


def test_filler_brands_collapse_to_one_row():
    """Мера 2: бренды латиницей с мл → одна строка-агрегат с перечнем и
    диапазоном цен; данные не теряются."""
    page = """<html><body><p>Приём врача-дерматолога — 1 500 ₽</p>
    <p>Juvederm Ultra 3 1 мл — 18 000 ₽</p>
    <p>Stylage M 1 мл — 15 000 ₽</p>
    <p>Belotero Balance 1 мл — 16 500 ₽</p></body></html>"""
    from src.site_checker import ensure_stage6_tables  # noqa: F401
    r, rows = _gate_for(page)
    assert r["gate"] == "Включён"
    assert rows == 2   # приём + агрегат брендов
    db_check_done = False
    # содержимое агрегата проверяем через повторный прогон с доступом к БД
    import src.site_checker as sc
    from src.classify import load_contours
    db = sqlite3.connect(":memory:")
    sc.ensure_stage6_tables(db)
    orig = sc.crawl_site
    sc.crawl_site = lambda *a, **k: ({"https://f.test/": page},
                                     {"level": 2, "last_level": 2, "last_status": "200",
                                      "blocked_by_robots": False})
    try:
        sc.process_clinic({"title": "F", "url": "https://f.test", "domain": "f.test"},
                          db, load_contours(), FORM_INDEX, set(), "Тест")
    finally:
        sc.crawl_site = orig
    row = db.execute("SELECT name_raw, description_raw, price, tag FROM services_found "
                     "WHERE name_raw LIKE 'Инъекционная эстетика%'").fetchone()
    assert row is not None
    assert "3 позиций" in row[0]
    assert "Juvederm" in row[1] and "Stylage" in row[1]
    assert "от" in row[2] and "до" in row[2]
    assert row[3] == "contour_filler"


def test_fuzzy_092_maps_close_but_not_antonyms():
    """Мера 3: порог 0.92; добро/злокачественные не сближаются никогда."""
    from src.mapper import map_tier1
    m = map_tier1("Приём детского дерматолога.", FORM_INDEX, fuzzy_cutoff=0.92)
    assert m and m["tag"] == "derm_consult_child"
    m2 = map_tier1("Удаление доброкачественных новообразований кожи",
                   FORM_INDEX, fuzzy_cutoff=0.92)
    assert m2 is None or "злокачествен" not in str(m2)
