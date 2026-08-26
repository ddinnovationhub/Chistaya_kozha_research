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


def test_phase1_judgments_nonmed_and_dissimilar():
    from src.classify import load_contours
    from src.phase1 import judge_company
    pages = {"https://y.ru": """<html><body><p>Продажа стройматериалов оптом</p>
        <p>Цемент М500 — 400 ₽</p><p>Доставка по городу</p></body></html>"""}
    j = judge_company(pages, FORM_INDEX, load_contours(), {})
    assert j["med"] == "не медорганизация"
    assert j["profile"] in ("не похож", "не определено")
    assert j["matches_n"] == 0
