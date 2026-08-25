"""Промежуточная выгрузка этапа 6 (п.8 промпта 2026-08-26):
лист 02_Услуги по обработанным клиникам, Спорные маппинги (средняя/низкая
уверенность), Услуги без тега, Сводка распределения, По клиникам.

Плюс файл «на разметку» (п.6 промпта исправления 2026-08-26): всё, что
ступень 1 не закрыла, уходит в output/{город}_на_разметку_{дата}.json —
заказчик размечает батчи в Claude Code по prompts/06_markup_batch.md."""

import datetime
import json
import pathlib
import sqlite3

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

from src.mapper import tags_reference_text

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
    base = "WHERE mapping_tier != 'вне профиля'"   # второй уровень фильтра
    if where:
        base += " AND " + where.replace("WHERE ", "")
    return list(db.execute(
        "SELECT clinic_id, clinic_title, name_raw, description_raw, page_url, price, "
        "COALESCE(tag, 'тега нет'), COALESCE(code_804n, 'код не определён'), "
        "mapping_basis, mapping_tier, confidence, client_has "
        f"FROM services_found {base} ORDER BY clinic_id, name_raw"))


# Профильные контуры для смысловой метрики (разбор заказчика 2026-08-26, п.4)
_PROFILE_CONTOURS = ("derm", "oncoderm", "trich", "dermsurg")


def profile_share(db: sqlite3.Connection) -> tuple[float, int, int]:
    """Доля профильных строк в 02_Услуги: (доля, профильных, всего).
    Профильная строка = тег профильного контура, ИЛИ якорная строка на
    разметке (второй фильтр пропускает только якорные), ИЛИ строка-агрегат."""
    import yaml as _yaml

    contours = {t["tag"]: t["contour"] for t in _yaml.safe_load(
        pathlib.Path("dictionaries/services.yaml").read_text(encoding="utf-8"))["tags"]}
    rows = list(db.execute(
        "SELECT tag, mapping_tier, mapping_basis FROM services_found "
        "WHERE mapping_tier != 'вне профиля'"))
    total = len(rows)
    good = sum(1 for tag, tier, basis in rows
               if contours.get(tag) in _PROFILE_CONTOURS
               or tier == 'на разметке'
               or 'агрегат' in (basis or ''))
    return (good / total if total else 1.0), good, total


def export_intermediate(city: str, db: sqlite3.Connection) -> pathlib.Path:
    # ── СМЫСЛОВОЙ ГЕЙТ (разбор заказчика 2026-08-26, п.4): доля профильных
    # строк ниже порога → таблица НЕ выпускается, разбираться дальше ──
    import yaml as _yaml
    qcfg = _yaml.safe_load(pathlib.Path("config/thresholds.yaml")
                           .read_text(encoding="utf-8")).get("quality", {})
    share_min = float(qcfg.get("profile_share_min", 0.70))
    share, good, total = profile_share(db)
    if total and share < share_min:
        raise RuntimeError(
            f"⛔ ТАБЛИЦА НЕ ВЫПУЩЕНА: доля профильных строк {share:.0%} "
            f"({good}/{total}) ниже порога {share_min:.0%} "
            f"(quality.profile_share_min) — разбор продолжается")

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
    r = _sheet(ws, "Услуги, для которых тега не нашлось (после разметки) или разметка ещё не выполнена — "
                   "прямой ответ «что оказывают конкуренты и не оказывает клиент».",
               SERVICE_COLS, [16, 26, 40, 36, 32, 10, 20, 14, 30, 12, 12, 10])
    for row in _svc_rows(db, "WHERE tag IS NULL"):
        _put(ws, r, row); r += 1

    ws = wb.create_sheet("Сводка")
    r = _sheet(ws, "Распределение маппинга по обработанным клиникам.", ["Показатель", "Значение"], [56, 40])
    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    total = q("SELECT COUNT(*) FROM services_found WHERE mapping_tier != 'вне профиля'")
    stats = [
        ("ДОЛЯ ПРОФИЛЬНЫХ СТРОК (гейт ≥70%)", f"{share:.0%} ({good}/{total})"),
        ("Всего строк услуг (без «вне профиля»)", total),
        ("Отсечено вторым фильтром (нет профильного якоря)",
         q("SELECT COUNT(*) FROM services_found WHERE mapping_tier='вне профиля'")),
        ("Смаплено кодом (ступень 1, точное совпадение)", q("SELECT COUNT(*) FROM services_found WHERE mapping_tier='код'")),
        ("Размечено вручную (Claude Code, ступень 2)", q("SELECT COUNT(*) FROM services_found WHERE mapping_tier='разметка'")),
        ("Ожидает разметки", q("SELECT COUNT(*) FROM services_found WHERE mapping_tier='на разметке'")),
        ("Без кода 804н", q("SELECT COUNT(*) FROM services_found WHERE code_804n IS NULL")),
        ("Без тега (вне справочника, после разметки)", q("SELECT COUNT(*) FROM services_found WHERE tag IS NULL AND mapping_tier='разметка'")),
        ("Уверенность высокая / средняя / низкая",
         f"{q('SELECT COUNT(*) FROM services_found WHERE confidence=' + chr(39) + 'высокая' + chr(39))}"
         f" / {q('SELECT COUNT(*) FROM services_found WHERE confidence=' + chr(39) + 'средняя' + chr(39))}"
         f" / {q('SELECT COUNT(*) FROM services_found WHERE confidence=' + chr(39) + 'низкая' + chr(39))}"),
    ]
    for k, v in stats:
        _put(ws, r, [k, v]); r += 1

    ws = wb.create_sheet("По_клиникам")
    r = _sheet(ws, "Итог по каждой обработанной клинике: ворота, тип, флаги, грейд. "
                   "Тип до слияния разметки — ПРЕДВАРИТЕЛЬНЫЙ (см. «Статус типа»).",
               ["ИД", "Клиника", "Форма собственности", "Домен", "Ворота", "Причина", "Тип", "Статус типа",
                "Правило", "Грейд", "Эстетические маркеры", "Несмежные",
                "Флаг: единств. несмежное", "Флаг: удаление вне дерм-контура",
                "Флаг: сайт недоступен", "Примечание доступности", "Уровень каскада",
                "Пакеты", "ИНН", "Статус ИНН", "Разделы"],
               [14, 26, 18, 20, 14, 24, 14, 22, 10, 8, 24, 24, 12, 14, 12, 30, 10, 8, 14, 24, 26])
    for row in db.execute(
            "SELECT clinic_id, title, ownership_form, domain, gate, gate_reason, "
            "type, type_status, "
            "rule, grade, esthetic_markers, nonadjacent, flag_single_nonadjacent, "
            "flag_removal_outside_derm, flag_site_unreachable, unreachable_note, "
            "fetch_level, has_packages, inn, inn_status, sections_found "
            "FROM clinics ORDER BY clinic_id"):
        _put(ws, r, list(row)); r += 1

    ws = wb.create_sheet("Отсечено_фильтром_позиции")
    r = _sheet(ws, "Строки, отсечённые ВТОРЫМ уровнем фильтра (нет профильного якоря в названии) — "
                   "на выборочную проверку: профильная услуга здесь = дефект якорного словаря.",
               ["ИД клиники", "Клиника", "Название с сайта", "Цена", "URL"],
               [16, 26, 50, 12, 34])
    for row in db.execute("SELECT clinic_id, clinic_title, name_raw, price, page_url "
                          "FROM services_found WHERE mapping_tier='вне профиля' "
                          "ORDER BY clinic_id, name_raw"):
        _put(ws, r, list(row)); r += 1

    ws = wb.create_sheet("07_Качество")
    r = _sheet(ws, "Каскад доступа к сайтам (заказчик, 2026-08-26): кто каким уровнем взят, "
                   "кто не взят ничем. Телеметрия попыток — таблица fetch_attempts в osint.db.",
               ["Показатель", "Значение"], [56, 60])
    q = lambda sql: db.execute(sql).fetchone()[0]  # noqa: E731
    total_clinics = q("SELECT COUNT(*) FROM clinics")
    unreachable = [row[0] for row in db.execute(
        "SELECT domain FROM clinics WHERE flag_site_unreachable=1 ORDER BY domain")]
    rows = [("Клиник обработано", total_clinics)]
    for lv, label in ((1, "Jina Reader"), (2, "прямой HTTP"),
                      (3, "Playwright headless"), (4, "Playwright эмуляция")):
        rows.append((f"Взято уровнем {lv} ({label})",
                     q(f"SELECT COUNT(*) FROM clinics WHERE fetch_level={lv}")))
    rows += [
        ("Не взято ни одним уровнем (Сайт недоступен)", len(unreachable)),
        ("Доля недоступных", f"{len(unreachable) / total_clinics:.0%}" if total_clinics else "—"),
        ("Домены недоступных", "; ".join(unreachable) or "—"),
        ("Страниц-попыток всего (fetch_attempts)", q("SELECT COUNT(*) FROM fetch_attempts")),
        ("Попыток «suspicious_zero страницы» (200 без контента)",
         q("SELECT COUNT(*) FROM fetch_attempts WHERE note LIKE 'suspicious_zero%'")),
        ("Заблокировано robots.txt (обход запрещён)",
         q("SELECT COUNT(*) FROM fetch_attempts WHERE status='robots_disallow'")),
        ("Непрофильных строк отбито при сборе (вакцинация, ЭКГ, несмежные и т.п.)",
         q("SELECT COALESCE(SUM(nonprofile_excluded),0) FROM clinics")),
        ("Обход: страниц найдено / обойдено (адаптивный, потолок 40)",
         f"{q('SELECT COALESCE(SUM(crawl_pages_found),0) FROM clinics')}"
         f" / {q('SELECT COALESCE(SUM(crawl_pages_fetched),0) FROM clinics')}"),
        ("Клиник, упёршихся в потолок страниц",
         q("SELECT COUNT(*) FROM clinics WHERE crawl_cap_hit=1")),
        ("Слой L1 (каталоги)", "выполняется отдельно с локальной машины "
                               "(python -m src.run_l1; решение заказчика 2026-08-26)"),
    ]
    for k, v in rows:
        _put(ws, r, [k, v]); r += 1

    ws = wb.create_sheet("03_Доказательства")
    r = _sheet(ws, "Каждый факт вне списка услуг — с цитатой и URL (лицензия, несмежные "
                   "направления, эстетика, реквизиты). Такт 3, Верификатор: вывод без цитаты запрещён.",
               ["ИД клиники", "Факт", "Деталь", "Цитата", "URL"],
               [16, 22, 22, 60, 34])
    for row in db.execute("SELECT clinic_id, kind, detail, quote, url "
                          "FROM clinic_evidence ORDER BY clinic_id, kind"):
        _put(ws, r, list(row)); r += 1

    out = pathlib.Path("output") / f"{city}_этап6_промежуточная_{day}.xlsx"
    wb.save(out)
    return out


def export_markup(city: str, db: sqlite3.Connection) -> pathlib.Path | None:
    """Файл «на разметку» (п.6, 2026-08-26): все строки mapping_tier='на разметке'
    батчами по клиникам, с row_id для обратной записи merge_markup.
    Возвращает None, если размечать нечего."""
    rows = list(db.execute(
        "SELECT id, clinic_id, clinic_title, name_raw, description_raw, page_url, price "
        "FROM services_found WHERE mapping_tier='на разметке' "
        "ORDER BY clinic_id, name_raw"))
    if not rows:
        return None
    day = datetime.date.today().isoformat()
    clinics: dict[str, dict] = {}
    for rid, cid, ctitle, name, desc, purl, price in rows:
        c = clinics.setdefault(cid, {"clinic_id": cid, "clinic_title": ctitle,
                                     "services": []})
        c["services"].append({"row_id": rid, "name": name, "description": desc,
                              "price": price, "page_url": purl})
    payload = {
        "city": city, "date": day,
        "instructions": ("Разметка в Claude Code по prompts/06_markup_batch.md. "
                         "Результат — output/{city}_разметка_{date}.json, "
                         "подхватывается: python -m src.merge_markup "
                         f"--city '{city}' --file output/{city}_разметка_{day}.json"),
        "tags_reference": tags_reference_text(),
        "clinics": sorted(clinics.values(), key=lambda c: c["clinic_id"]),
    }
    out = pathlib.Path("output") / f"{city}_на_разметку_{day}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return out
