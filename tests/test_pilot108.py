"""Пилот 108 (2026-08-26): импорт, мульти-сайты, суждение А с четырьмя
исходами (включая УК сети клиник), выгрузка с исходными колонками."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.mapper import build_formulation_index  # noqa: E402
from src.pilot108 import split_site_cell  # noqa: E402

FORM_INDEX = build_formulation_index()


def test_split_multisite_cell():
    """8 строк выгрузки имеют несколько сайтов в ячейке — каждый кандидат."""
    cell = ("https://www.ufanet.ru/sterlitamak, www.ufanet.ru, "
            "https://vkomandu.ufanet.ru/vacancy/orsk, ufaman.ru")
    doms = split_site_cell(cell)
    assert "ufanet.ru" in doms and "ufaman.ru" in doms
    assert "vkomandu.ufanet.ru" in doms
    assert len(doms) == len(set(doms))
    assert split_site_cell(None) == []


def test_judge_pilot_a_four_outcomes():
    """Четыре исхода суждения А; мед-контекст обязателен; УК сети —
    находка, не мусор; каждое основание с цитатой."""
    from src.extract_site import extract_pages
    from src.pilot108 import judge_pilot_a

    def judge(html):
        pages = {"https://t.ru": html}
        return judge_pilot_a(pages, extract_pages(pages, FORM_INDEX))

    a, basis, mgmt = judge("""<html><p>Группа управляет активами.</p>
        <p>Наша сеть клиник «Здоровье+» — 12 клиник, приём врачей ежедневно</p></html>""")
    assert a == "управляющая компания сети клиник"
    assert mgmt and "сеть клиник" in mgmt.lower()
    assert "t.ru" in basis

    a2, basis2, _ = judge("""<html><p>Лицензия ЛО-54-01-000001 на осуществление
        медицинской деятельности</p><p>Приём дерматолога — 1 500 ₽</p></html>""")
    assert a2 == "медорганизация" and "ЛО-54" in basis2

    a3, _, _ = judge("<html><p>Гоночные лицензии: как получить</p></html>")
    assert a3 != "медорганизация"

    a4, basis4, _ = judge("<html><p>Производство стройполимеров, прайс на плиты</p></html>")
    assert a4 == "не медорганизация" and "признаков" in basis4


def test_pilot_import_keeps_source_columns(tmp_path):
    import openpyxl

    from src.pilot108 import ensure_pilot_tables, import_pilot
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Наименование", "Регистрационный номер",
               "Сайт в сети Интернет", "Код налогоплательщика",
               "Регион регистрации", "Вид деятельности/отрасль",
               "Маркер ОКВЭД", "2025, Выручка, RUB"])
    ws.append([2084, "ФАРМЛЕНД, АО", "1020202392121", "farmlend.ru",
               "0273028277", "Башкортостан (Республика)",
               "Исследование конъюнктуры рынка", "2", 52192153000])
    f = tmp_path / "p.xlsx"
    wb.save(f)
    db = sqlite3.connect(":memory:")
    ensure_pilot_tables(db)
    st = import_pilot(str(f), db)
    assert st["total"] == 1 and st["with_sites"] == 1
    row = db.execute("SELECT row_no, city, okved_marker, sites_raw "
                     "FROM pilot_companies").fetchone()
    assert row[0] == 2084          # исходный № сохранён (ключ сверки)
    assert row[1] == "Уфа"         # регион → центральный город
    assert row[2] == "2"           # маркер ОКВЭД сохранён (судить по нему нельзя)
    assert row[3] == "farmlend.ru"


def test_rename_legacy_idempotent():
    from src.pilot108 import rename_legacy
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE companies (inn TEXT)")
    assert rename_legacy(db) == ["companies"]
    assert rename_legacy(db) == []   # повторно — ничего
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
    assert "companies_legacy" in names and "companies" not in names
