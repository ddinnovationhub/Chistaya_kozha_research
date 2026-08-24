"""Промежуточная выгрузка этапа 6 (п.8 промпта 2026-08-26):
лист 02_Услуги по обработанным клиникам, Спорные маппинги (средняя/низкая
уверенность), Услуги без тега, Сводка распределения, По клиникам."""

import datetime
import pathlib
import sqlite3

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ARIAL = Font(name="Arial", size=10)
BOLD = Font(name="Arial", size=10, bold=True)
NOTE = Font(name="Arial", size=9, italic=True, color="555555")
HDR = PatternFill("solid", fgColor="DDE7F3")

SERVICE_COLS = ["ИД клиники", "Клиника", "Название с сайта (дословно)",
                "Описание с сайта", "URL страницы", "Цена", "Наш тег",
                "Код 804н", "Основание маппинга", "Ступень маппинга",
                "Уверенность", "Есть у ЧК"]


def _sheet(ws, note, headers, widths):
    ws.cell(1, 1, note).font = NOTE
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    for i, h in enumerate(headers, 1):
        c = ws.cell(2, i, h); c.font = BOLD; c.fill = HDR
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    ws.freeze_panes = "A3"
    return 3


def _put(ws, r, vals):
    for i, v in enumerate(vals, 1):
        c = ws.cell(r, i, v if v not in (None, "") else "Не найдено")
        c.font = ARIAL
        c.alignment = Alignment(vertical="top", wrap_text=True)


def _svc_rows(db, where=""):
    return list(db.execute(
        "SELECT clinic_id, clinic_title, name_raw, description_raw, page_url, price, "
        "COALESCE(tag, 'тега нет'), COALESCE(code_804n, 'код не определён'), "
        "mapping_basis, mapping_tier, confidence, client_has "
        f"FROM services_found {where} ORDER BY clinic_id, name_raw"))


def export_intermediate(city: str, db: sqlite3.Connection) -> pathlib.Path:
    wb = openpyxl.Workbook()
    day = datetime.date.today().isoformat()

    ws = wb.active
    ws.title = "02_Услуги"
    r = _sheet(ws, f"Все услуги первых обработанных клиник ({city}, {day}). Сбор ОТКРЫТЫЙ: "
                   "включая позиции вне справочника. Основание и ступень маппинга — для выборочной проверки решений.",
               SERVICE_COLS, [16, 26, 40, 36, 32, 10, 20, 14, 30, 12, 12, 10])
    for row in _svc_rows(db):
        _put(ws, r, row); r += 1

    ws = wb.create_sheet("Спорные_маппинги")
    r = _sheet(ws, "Все строки с уверенностью «средняя» или «низкая» — на выборочную проверку заказчиком.",
               SERVICE_COLS, [16, 26, 40, 36, 32, 10, 20, 14, 30, 12, 12, 10])
    for row in _svc_rows(db, "WHERE confidence IN ('средняя','низкая')"):
        _put(ws, r, row); r += 1

    ws = wb.create_sheet("Услуги_без_тега")
    r = _sheet(ws, "Услуги, для которых тега не нашлось — прямой ответ «что оказывают конкуренты и не оказывает клиент».",
               SERVICE_COLS, [16, 26, 40, 36, 32, 10, 20, 14, 30, 12, 12, 10])
    for row in _svc_rows(db, "WHERE tag IS NULL"):
        _put(ws, r, row); r += 1

    ws = wb.create_sheet("Сводка")
    r = _sheet(ws, "Распределение маппинга по обработанным клиникам.", ["Показатель", "Значение"], [56, 40])
    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    total = q("SELECT COUNT(*) FROM services_found")
    stats = [
        ("Всего строк услуг", total),
        ("Смаплено кодом (ступень 1, точное совпадение)", q("SELECT COUNT(*) FROM services_found WHERE mapping_tier='код'")),
        ("Смаплено моделью (ступень 2)", q("SELECT COUNT(*) FROM services_found WHERE mapping_tier='модель'")),
        ("По одному названию без описания", q("SELECT COUNT(*) FROM services_found WHERE mapping_basis LIKE '%описание отсутствует%'")),
        ("Без кода 804н", q("SELECT COUNT(*) FROM services_found WHERE code_804n IS NULL")),
        ("Без тега (вне справочника)", q("SELECT COUNT(*) FROM services_found WHERE tag IS NULL")),
        ("Уверенность высокая / средняя / низкая",
         f"{q('SELECT COUNT(*) FROM services_found WHERE confidence=' + chr(39) + 'высокая' + chr(39))}"
         f" / {q('SELECT COUNT(*) FROM services_found WHERE confidence=' + chr(39) + 'средняя' + chr(39))}"
         f" / {q('SELECT COUNT(*) FROM services_found WHERE confidence=' + chr(39) + 'низкая' + chr(39))}"),
    ]
    for k, v in stats:
        _put(ws, r, [k, v]); r += 1

    ws = wb.create_sheet("По_клиникам")
    r = _sheet(ws, "Итог по каждой обработанной клинике: ворота, тип, флаги, грейд.",
               ["ИД", "Клиника", "Домен", "Ворота", "Причина", "Тип", "Правило", "Грейд",
                "Эстетические маркеры", "Несмежные", "Флаг: единств. несмежное",
                "Флаг: удаление вне дерм-контура", "Пакеты", "ИНН", "Статус ИНН", "Разделы"],
               [14, 26, 20, 14, 24, 14, 10, 8, 24, 24, 12, 14, 8, 14, 24, 26])
    for row in db.execute(
            "SELECT clinic_id, title, domain, gate, gate_reason, type, rule, grade, "
            "esthetic_markers, nonadjacent, flag_single_nonadjacent, "
            "flag_removal_outside_derm, has_packages, inn, inn_status, sections_found "
            "FROM clinics ORDER BY clinic_id"):
        _put(ws, r, list(row)); r += 1

    out = pathlib.Path("output") / f"{city}_этап6_промежуточная_{day}.xlsx"
    wb.save(out)
    return out
