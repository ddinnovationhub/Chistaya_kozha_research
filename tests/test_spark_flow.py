"""Новый входной слой (promt_spark_krug, 2026-08-25): импорт СПАРК,
достройка сайтов, суждения фазы 1."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.mapper import build_formulation_index  # noqa: E402
from src.site_finder import (content_matches_name, name_tokens,  # noqa: E402
                             translit_candidates)

FORM_INDEX = build_formulation_index()


def test_translit_candidates_from_name():
    cands = translit_candidates("#ЗДОРОВЬЯВСЕМ, ООО")
    assert "zdorovyavsem.ru" in cands
    # ОПФ и родовые слова не образуют доменов
    assert not any("ooo" in c for c in cands)


def test_content_match_requires_name_not_just_200():
    assert content_matches_name("Клиника Здоровьявсем рада вам", "#ЗДОРОВЬЯВСЕМ, ООО")
    assert content_matches_name("zdorovyavsem — запись", "#ЗДОРОВЬЯВСЕМ, ООО")
    assert not content_matches_name("Купите этот домен", "#ЗДОРОВЬЯВСЕМ, ООО")
    assert name_tokens("МЦ, ООО") == []   # только ОПФ — токенов нет


def test_spark_import_dedup_and_networks(tmp_path):
    import openpyxl

    from src.spark_import import ensure_companies_table, import_spark
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "report"
    for _ in range(3):
        ws.append([])
    ws.append(["№", "Наименование", "Регистрационный номер",
               "Сайт в сети Интернет", "Код налогоплательщика",
               "Регион регистрации", "Вид деятельности/отрасль", "2025, Выручка, RUB"])
    ws.append([1, "КЛИНИКА А, ООО", "1190280011514", "https://www.setka.ru/about",
               "5405270517", "Новосибирская область", "Общая врачебная практика", 25000000])
    ws.append([2, "КЛИНИКА Б, ООО", "1105905005128", "setka.ru",
               "5405270518", "Новосибирская область", "Общая врачебная практика", 30000000])
    ws.append([3, "БИТЫЙ ИНН, ООО", "123", "x.ru", "12345",
               "Новосибирская область", "прочее", 1])
    f = tmp_path / "spark.xlsx"
    wb.save(f)
    db = sqlite3.connect(":memory:")
    ensure_companies_table(db)
    st = import_spark(str(f), db)
    assert st["inserted"] == 2 and st["bad_inn"] == 1
    # ОГРН «123» — невалиден, не записан; ИНН — ключ
    a = db.execute("SELECT site_spark, shared_domain_with, city FROM companies "
                   "WHERE inn='5405270517'").fetchone()
    assert a[0] == "setka.ru"            # нормализация www/https/путь
    assert a[1] == "5405270518"          # сеть: домен связан со вторым ИНН
    assert a[2] == "Новосибирск"         # регион → центральный город


def test_phase1_judgments_med_and_profile():
    from src.classify import load_contours
    from src.phase1 import judge_company
    ck_index = {"прием дерматолога": "Приём дерматолога",
                "дерматоскопия": "Дерматоскопия",
                "удаление папиллом": "Удаление папиллом"}
    pages = {"https://x.ru": """<html><body><p>Лицензия ЛО-54-01-000001</p>
        <p>Приём дерматолога — 1 500 ₽</p><p>Дерматоскопия — 1 200 ₽</p>
        <p>Удаление папиллом — 900 ₽</p></body></html>"""}
    j = judge_company(pages, FORM_INDEX, load_contours(), ck_index)
    assert j["med"] == "медорганизация"
    assert "Лицензия" in j["med_basis"] or "лицензия" in j["med_basis"]
    assert j["profile"] == "похож"        # 3 из 3 = 100% ≥ 30%
    assert j["matches_n"] >= 3 and "Дерматоскопия" in j["matches"]
    assert j["services"]                  # позиции возвращаются для персиста


def test_phase1_anchor_counts_like_gate():
    """Такт 3 (кейс alfa-clinic): «Удаление невуса лазером» не совпадает
    дословно ни с ЧК, ни со словарём — но это профильная позиция (якорь);
    приём профильного врача делает профиль похожим по формуле ворот."""
    from src.classify import load_contours
    from src.phase1 import judge_profile
    services = [{"name": "Удаление невуса лазером", "price": "900", "page_url": "u"},
                {"name": "Приём врача-дерматовенеролога", "price": "1700", "page_url": "u"},
                {"name": "Общий массаж спины", "price": "1500", "page_url": "u"},
                {"name": "Приём терапевта", "price": "1500", "page_url": "u"}]
    p = judge_profile(services, FORM_INDEX, load_contours(), {})
    assert p["matches_n"] >= 2            # невус (якорь) + приём (словарь)
    assert p["profile"] == "похож"        # 50% ≥ 30%, плюс профильный врач
    assert "невуса" in p["matches"].lower()


def test_phase1_judgments_nonmed_and_dissimilar():
    from src.classify import load_contours
    from src.phase1 import judge_company
    pages = {"https://y.ru": """<html><body><p>Продажа стройматериалов оптом</p>
        <p>Цемент М500 — 400 ₽</p><p>Доставка по городу</p></body></html>"""}
    j = judge_company(pages, FORM_INDEX, load_contours(), {})
    assert j["med"] == "не медорганизация"
    assert j["profile"] in ("не похож", "не определено")
    assert j["matches_n"] == 0


# ─── Разбор дефектов фазы 1 (2026-08-26): тройная проверка, мед-контекст ───

def test_triple_check_inn_strongest():
    """ИНН на сайте — сильнейший признак, подтверждает при любом контенте."""
    from src.site_finder import triple_check
    pages = ["<html>ООО Ромашка, ИНН 5405270517, г. Москва, ул. Ленина 1</html>"]
    chk = triple_check("x.ru", "5405270517", "Новосибирск", pages_hint=pages)
    assert chk["verdict"] == "ИНН"


def test_triple_check_city_mention_is_not_address():
    """Корректировка №1: УПОМИНАНИЕ города в тексте — не признак; признак —
    адрес организации (город в связке с адресными маркерами)."""
    from src.site_finder import triple_check
    blog = ["<html>Наши статьи читают в городах: Новосибирск, доставка по России</html>"]
    chk = triple_check("x.ru", "0000000000", "Новосибирск", pages_hint=blog)
    assert chk["verdict"] is None
    addr = ["<html>Контакты: г. Новосибирск, ул. Ленина, д. 1, офис 5</html>"]
    chk2 = triple_check("x.ru", "0000000000", "Новосибирск", pages_hint=addr)
    assert chk2["verdict"] == "адрес"


def test_triple_check_federal_network_needs_inn():
    """>3 городов на сайте = федеральная сеть: город не различает,
    подтверждение только по ИНН."""
    from src.site_finder import triple_check
    fed = ["<html>Клиники: г. Москва, г. Казань, г. Самара, г. Уфа, "
           "г. Новосибирск, ул. Ленина 1 — адреса филиалов</html>"]
    chk = triple_check("net.ru", "0000000000", "Новосибирск", pages_hint=fed)
    assert chk["verdict"] is None and chk["fed_network"] is True
    chk2 = triple_check("net.ru", "5405270517",
                        "Новосибирск", pages_hint=[fed[0] + " ИНН 5405270517"])
    assert chk2["verdict"] == "ИНН"


def test_med_license_requires_context():
    """Корректировка №2 (кейс АВТОКОМБИНАТ): «гоночные лицензии» — не
    медорганизация; лицензия с ЛО-номером/мед-словами — медорганизация."""
    from src.classify import load_contours
    from src.phase1 import judge_company
    race = {"https://a.ru": "<html><p>Гоночные лицензии: какие бывают и как их получить — 500 ₽</p></html>"}
    j = judge_company(race, FORM_INDEX, load_contours(), {})
    assert j["med"] != "медорганизация"
    med = {"https://b.ru": """<html><p>Лицензия ЛО-54-01-000001 на осуществление
        медицинской деятельности</p><p>Чистка лица — 2 000 ₽</p></html>"""}
    j2 = judge_company(med, FORM_INDEX, load_contours(), {})
    assert j2["med"] == "медорганизация"
    assert "ЛО-54" in j2["med_basis"]


def test_reaudit_downgrades_by_saved_basis():
    """Пересчёт всех суждений А по сохранённым цитатам — без обхода."""
    import sqlite3 as sq
    from src.phase1 import reaudit_med_judgments
    from src.spark_import import ensure_companies_table
    db = sq.connect(":memory:")
    ensure_companies_table(db)
    rows = [
        ("1000000001", "лицензия: «Гоночные лицензии: как получить» (url)", "не определено"),
        ("1000000002", "лицензия: «Лицензия ЛО-54-01-000001 Минздрава» (url)", "медорганизация"),
        ("1000000003", "заявлены врачебные специальности: гинеколог", "не определено"),
        ("1000000004", "приём врача в прайсе: «Приём терапевта»", "медорганизация"),
    ]
    for inn, basis, _exp in rows:
        db.execute("INSERT INTO companies (inn, med_judgment, med_basis) "
                   "VALUES (?,?,?)", (inn, "медорганизация", basis))
    st = reaudit_med_judgments(db)
    assert st["license_downgraded"] == 1 and st["license_kept"] == 1
    assert st["specialties_downgraded"] == 1 and st["visit_kept"] == 1
    for inn, _b, expected in rows:
        got = db.execute("SELECT med_judgment FROM companies WHERE inn=?",
                         (inn,)).fetchone()[0]
        assert got == expected, (inn, got)


def test_a_markup_only_for_similar_or_undefined_profile():
    """Порядок заказчика №3: разметка А только у «похож»/«не определено» Б."""
    import sqlite3 as sq
    from src.phase1 import ensure_phase1_tables, export_a_markup
    from src.spark_import import ensure_companies_table
    import json, pathlib
    import os, tempfile
    cwd = os.getcwd()
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "output"))
    os.chdir(tmp)   # тест не должен трогать боевой output/ (такт 3)
    db = sq.connect(":memory:")
    ensure_companies_table(db)
    ensure_phase1_tables(db)
    data = [
        ("2000000001", "А", "похож", "не определено"),      # в батч
        ("2000000002", "Б", "не определено", None),          # в батч
        ("2000000003", "В", "не похож", "не определено"),    # НЕ в батч
        ("2000000004", "Г", "похож", "медорганизация"),      # НЕ в батч (А сильное)
    ]
    for inn, nm, profj, medj in data:
        db.execute("INSERT INTO companies (inn, name, city, site, fetch_status, "
                   "profile_judgment, med_judgment) VALUES (?,?,?,?,?,?,?)",
                   (inn, nm, "Тест", f"{inn}.ru", "ok", profj, medj))
    out = export_a_markup(db)
    payload = json.loads(pathlib.Path(out).read_text(encoding="utf-8"))
    inns = {c["inn"] for c in payload["companies"]}
    assert inns == {"2000000001", "2000000002"}, inns
    os.chdir(cwd)
