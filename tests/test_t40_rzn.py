"""Тест 20-40 и реестр лицензий РЗН (2026-08-27): извлечение специальностей
из перечня работ, сохранение лицензий, паспорт сайта, импорт t40."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_specialties_from_activity():
    from src.rzn_licenses import specialties_from_activity
    acts = ["При оказании первичной медико-санитарной помощи организуются и "
            "выполняются следующие работы (услуги): при оказании первичной "
            "специализированной медико-санитарной помощи в амбулаторных "
            "условиях по: дерматовенерологии, косметологии, онкологии"]
    specs = specialties_from_activity(acts)
    assert "дерматовенерологи" in specs
    assert "косметологи" in specs
    assert "онкологи" in specs
    assert "стоматологи" not in specs
    assert specialties_from_activity([]) == []


def test_save_licenses_and_negative():
    from src.rzn_licenses import ensure_tables, save_licenses
    db = sqlite3.connect(":memory:")
    ensure_tables(db)
    lic = {"number": "Л041-01170-02/00362563", "date": "25.07.2017",
           "licensee": "АО Фармленд", "authority": "МЗ РБ", "ogrn": "102",
           "inn": "0273028277", "valid_to": "Бессрочно", "annulled": None,
           "terminated": None, "is_med": True,
           "objects": [{"address": "Уфа, ул. Менделеева 128/3", "city": "Уфа",
                        "region": "РБ",
                        "activity": "…в амбулаторных условиях по: косметологии"}]}
    res = save_licenses(db, "0273028277", [lic])
    assert res == {"status": "проверен", "licenses": 1, "med": 1}
    row = db.execute("SELECT is_med, objects_n, specialties FROM rzn_licenses "
                     "WHERE inn='0273028277'").fetchone()
    assert row == (1, 1, "косметологи")
    # отрицательный результат — тоже запись
    save_licenses(db, "1234567890", [])
    assert db.execute("SELECT status, licenses_n FROM rzn_checked "
                      "WHERE inn='1234567890'").fetchone() == ("проверен", 0)
    # неудача запроса ≠ «лицензий нет»
    save_licenses(db, "1111111111", None)
    assert db.execute("SELECT status FROM rzn_checked WHERE inn='1111111111'"
                      ).fetchone() == ("запрос не удался",)


def test_med_number_detection():
    from src.rzn_licenses import _MED_NUM_RE
    assert _MED_NUM_RE.match("Л041-01170-02/00362563")
    assert _MED_NUM_RE.match("ЛО-54-01-000001")
    assert not _MED_NUM_RE.match("Л042-00110-77/00286463")   # фарма


def test_passport_build():
    from src.extract_site import extract_pages
    from src.mapper import build_formulation_index
    from src.passport import build_passport, contact_lines, menu_texts
    html = """<html><head><title>Клиника Пример — дерматология в Уфе</title>
    <meta name="description" content="Приём дерматолога и косметолога"></head>
    <body><nav><a href="/derm">Дерматология</a><a href="/stom">Стоматология</a>
    <a href="/price">Цены</a><a href="/kontakty">Контакты</a></nav>
    <h1>Медицинский центр Пример</h1>
    <p>ИНН 0273028277, ОГРН 1020202392121</p>
    <p>г. Уфа, ул. Ленина, д. 1</p>
    <p>Приём дерматолога — 1 500 ₽</p></body></html>"""
    pages = {"https://example.ru": html}
    data = extract_pages(pages, build_formulation_index())
    p = build_passport("example.ru", pages, data)
    assert "Дерматология | Стоматология" in p        # меню целиком, по порядку
    assert "ИНН 0273028277" in p                     # контакт-блок дословно
    assert "TITLE: Клиника Пример" in p
    assert "дерматолог" in p                          # специальности
    menu = menu_texts(html)
    assert menu[:2] == ["Дерматология", "Стоматология"]
    assert contact_lines("г. Уфа, ул. Ленина, д. 1\nпросто текст") == \
        ["г. Уфа, ул. Ленина, д. 1"]


def test_import_t40_first_n(tmp_path):
    import openpyxl

    from src.test40 import import_t40
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Наименование", "Регистрационный номер",
               "Сайт в сети Интернет", "Код налогоплательщика",
               "Регион регистрации", "Вид деятельности/отрасль",
               "Маркер ОКВЭД", "2025, Выручка, RUB"])
    for i in range(1, 6):
        ws.append([i, f"КОМПАНИЯ-{i}, ООО", f"10{i}", f"site{i}.ru",
                   f"027302827{i % 10}" if i != 3 else "badinn",
                   "Башкортостан (Республика)", "Деятельность", "2", 100])
    f = tmp_path / "base.xlsx"
    wb.save(f)
    db = sqlite3.connect(":memory:")
    st = import_t40(str(f), db, first_n=4)
    assert st["total"] == 4                # первые N, не весь файл
    assert st["bad_inn"] == 1              # невалидный ИНН не проходит
    assert db.execute("SELECT COUNT(*) FROM t40_companies").fetchone()[0] == 3
    assert db.execute("SELECT city FROM t40_companies LIMIT 1"
                      ).fetchone()[0] == "Уфа"


def test_parse_judge_json():
    from src.llm_judge import _parse_judge_json
    ok = _parse_judge_json('Вот ответ: {"суждение_А": "медорганизация", '
                           '"профиль": ["дерматология"], "основание": "Приём '
                           'дерматолога — 1 500 ₽"}')
    assert ok["суждение_А"] == "медорганизация"
    assert ok["профиль"] == ["дерматология"]
    # невалидный исход отбрасывается, а не пишется
    assert _parse_judge_json('{"суждение_А": "наверное клиника"}') is None
    assert _parse_judge_json("не json") is None


def test_pdf_url_extraction():
    from src.rzn_licenses import RZN_URL
    import re
    label = ('<a class="getfile" href="?downloadlic=753079&pdf=1">PDF</a>')
    m = re.search(r"downloadlic=(\d+)", label)
    assert f"{RZN_URL}?downloadlic={m.group(1)}&pdf=1" == \
        "https://roszdravnadzor.gov.ru/services/licenses?downloadlic=753079&pdf=1"


def test_import_resume_does_not_clobber(tmp_path):
    """Краш-перезапуск: повторный импорт НЕ затирает добытые результаты."""
    import openpyxl

    from src.test40 import import_t40
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Наименование", "Рег", "Сайт", "ИНН", "Регион",
               "Вид", "Маркер", "Выручка"])
    ws.append([1, "КОМПАНИЯ, ООО", "1", "a.ru", "0273028277",
               "Башкортостан (Республика)", "Мед", "1", 1])
    f = tmp_path / "b.xlsx"
    wb.save(f)
    db = sqlite3.connect(":memory:")
    import_t40(str(f), db, 40)
    db.execute("UPDATE t40_companies SET found_site='a.ru', "
               "med_judgment='медорганизация'")
    import_t40(str(f), db, 40)   # перезапуск шага импорта
    assert db.execute("SELECT found_site, med_judgment FROM t40_companies"
                      ).fetchone() == ("a.ru", "медорганизация")
