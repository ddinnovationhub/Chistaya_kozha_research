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
