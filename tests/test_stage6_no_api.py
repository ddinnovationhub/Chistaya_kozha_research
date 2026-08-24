"""Этап 6 без внешнего API (промпт исправления 2026-08-26, п.6):
кодовая экстракция, ступень 1, слияние ручной разметки."""

import pathlib
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extract_site import extract_pages  # noqa: E402
from src.mapper import build_formulation_index, map_tier1  # noqa: E402
from src.merge_markup import merge_markup  # noqa: E402
from src.site_checker import TYPE_STATUS_FINAL, ensure_stage6_tables  # noqa: E402

FORM_INDEX = build_formulation_index()

PAGE = """# Услуги и цены

| Услуга | Цена |
| Дерматоскопия | 1 200 ₽ |
| Удаление папилломы лазером | 900 ₽ |

Приём дерматолога
Лечение акне
Комплекс «Чистая кожа» под ключ — 15 000 ₽
Записаться на приём
Лицензия ЛО-54-01-006302 от 01.01.2024
ООО «Тестовая клиника», ИНН 5406123456, ОГРН 1125476000000
Приём гинеколога — 2 000 ₽
Ботулинотерапия — 4 500 ₽
"""


def _extract():
    return extract_pages({"https://clinic.test/uslugi": PAGE}, FORM_INDEX)


def test_price_and_table_candidates_extracted():
    names = [s["name"] for s in _extract()["services"]]
    assert "Дерматоскопия" in names
    assert "Удаление папилломы лазером" in names
    assert any("Комплекс" in n for n in names)
    assert not any("Записаться" in n for n in names)  # навигация отсекается


def test_dictionary_line_without_price_extracted():
    names = [s["name"] for s in _extract()["services"]]
    assert "Приём дерматолога" in names or "Лечение акне" in names


def test_signals_extracted():
    d = _extract()
    assert d["license_evidence"]["found"]
    assert d["requisites"]["inn_text"] == "5406123456"
    assert d["requisites"]["legal_name"].startswith("ООО")
    assert "дерматолог" in d["doctor_specialties"]
    assert d["esthetic_cosmetology_present"]      # ботулинотерапия
    assert d["has_packages"]                       # «Комплекс … под ключ»
    assert any(n["direction"] == "гинекология" for n in d["nonadjacent_signs"])


def test_tier1_maps_extracted_candidate():
    m = map_tier1("Дерматоскопия", FORM_INDEX)
    assert m and m["tag"] == "dermatoscopy" and m["tier"] == "код"


def test_merge_markup_validates_and_finalizes(tmp_path):
    db = sqlite3.connect(":memory:")
    ensure_stage6_tables(db)
    db.execute("INSERT INTO clinics (clinic_id, title, domain, gate, type, type_status) "
               "VALUES ('КЛН-x', 'X', 'x.ru', 'Включён', 'Не классифицировано', "
               "'ожидает разметки (ступень 1 тегов не дала)')")
    db.execute("INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
               "mapping_tier) VALUES ('КЛН-x', 'X', 'Приём у дерматолога-эксперта', 'на разметке')")
    rid = db.execute("SELECT id FROM services_found").fetchone()[0]

    markup = tmp_path / "разметка.json"
    markup.write_text(
        '{"rows": [{"row_id": %d, "tag": "derm_consult", "code_804n": null, '
        '"basis": "приём дерматолога назван дословно", "confidence": "высокая", '
        '"is_package": false}, '
        '{"row_id": 999, "tag": "no_such_tag", "code_804n": null, '
        '"basis": "x", "confidence": "высокая", "is_package": false}]}' % rid,
        encoding="utf-8")

    res = merge_markup("Тест", pathlib.Path(markup), db)
    assert res["applied"] == 1
    assert len(res["rejected"]) == 1  # несуществующий row_id + тег вне справочника
    row = db.execute("SELECT tag, mapping_tier, client_has FROM services_found "
                     "WHERE id=?", (rid,)).fetchone()
    assert row == ("derm_consult", "разметка", "Да")
    clinic = db.execute("SELECT type, type_status FROM clinics").fetchone()
    assert clinic[0] == "Тип 1"
    assert clinic[1] == TYPE_STATUS_FINAL
