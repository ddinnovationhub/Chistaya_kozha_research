"""Направления прайс-позиций: нормализация, UI-мусор, гарды правил,
канон словаря, спорные строки голда."""
import yaml

from src.direction_map import (DIRECTIONS_YAML, classify, is_ui_garbage,
                               load_gold, norm, strip_ui)


def cats():
    return set(yaml.safe_load(open(DIRECTIONS_YAML, encoding="utf-8"))
               ["categories"])


def test_norm_and_strip_ui():
    assert norm("Ёлки_x000D_  Палки!") == "елки палки"
    assert strip_ui("Удаление гемангиом, Записаться") == "Удаление гемангиом"
    assert strip_ui("Акция! Пилинг, бонусов, Записаться") == "Пилинг"


def test_ui_garbage_and_names_row():
    assert is_ui_garbage("Записаться")
    assert is_ui_garbage("Цена")
    assert is_ui_garbage("Ольга Игнатенко Лилия Киселева")
    # чётное число слов — НЕ мусор (регресс бага шаблона имён)
    assert not is_ui_garbage("Удаление доброкачественных новообразований кожи")


def test_false_keys_guarded():
    # R4: ложные ключи из проверки заказчика
    d, *_ = classify("Береза бородавчатая t3 Betula verrucosa")
    assert d == "Аллергология"
    d, *_ = classify("перхоть лошади, клещ Dermatophagoides")
    assert d == "Аллергология"
    d, *_ = classify("Барий (Ba) в волосах")
    assert d == "Лаборатория"
    # хвост «без учёта анестезии» не тянет в Анестезиологию
    d, *_ = classify("Внутрикожный шов (без учета стоимости анестезии)")
    assert d != "Анестезиология"


def test_essence_over_disease():
    # R1: серология/ПЦР/гистология — Лаборатория
    assert classify("Антитела класса IgM к Borrelia burgdorferi")[0] == \
        "Лаборатория"
    assert classify("Гистологическое исследование операционного материала")[0] \
        == "Лаборатория"
    # R3: генетика отдельно
    assert classify("Определение мутации в гене протромбина")[0] == "Генетика"
    # приём по специальности (R7)
    assert classify("Прием (осмотр, консультация) врача-кардиолога")[0] == \
        "Приём смежного специалиста"
    assert classify("Прием врача-дерматовенеролога первичный")[0] == \
        "Дерматовенерология"


def test_localization_beats_method():
    # R6
    assert classify("Удаление новообразования вульвы (кондиломы)")[0] == \
        "Гинекология"
    assert classify("Удаление папилломы радиоволновым методом")[0] == \
        "Удаление новообразований (дерматохирургия)"


def test_all_outputs_canonical():
    c = cats()
    for name in ("УЗИ брюшной полости", "Ботулинотерапия лба",
                 "Общий массаж медицинский", "Вакцинация против гриппа",
                 "ФГДС с седацией", "Кал на скрытую кровь"):
        d, *_ = classify(name)
        assert d is None or d in c, (name, d)


def test_gold_loads_with_disputed():
    g = load_gold()
    assert len(g) > 900
    assert sum(1 for v in g.values() if v[2]) >= 8  # спорные исключены
