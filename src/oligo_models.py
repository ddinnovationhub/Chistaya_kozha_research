"""Типологические модели клиник с дерматовенерологией (заказчик, 2026-09-23).

Срез: клиники листа 8 (олигопрофильные), у которых в комбинации есть
«Дерматовенерология» — 48 организаций. Остальные строки листа 8 помечаются
«вне среза (нет дерматовенерологии)».

Метрики от структуры прайса:
  · клин.прайс — доля позиций клинических направлений во всём прайсе;
  · доли блоков ВНУТРИ клинического прайса:
      ДЕРМ-МЕД = дерматовенерология + трихология + удаление новообразований
                 + подология + онкодерматология (медицинский дерм-контур);
      КОСМ = косметология; Ж+М = гинекология + репродуктология + маммология
      + урология; ВОССТ = травматология + неврология + физиотерапия +
      остеопатия + реабилитация + нейрохирургия.

Модели (первое подходящее правило):
  1. «сервисно-диагностическая с дерма-примесью» — клин.прайс < 20%
     (организация живёт лабораторией/УЗИ/справками);
  2. «медицинская дерматология (ядро)» — ДЕРМ-МЕД ≥ 50%;
  3. «дерма при женском/мужском здоровье» — Ж+М ≥ 40%;
  4. «дермато-косметическая сбалансированная (аналог ЧК)» —
     ДЕРМ-МЕД 15–50% (дерм-контур — самостоятельная бизнес-линия
     при косметологическом объёме);
  5. «дерма при восстановительной медицине» — ВОССТ ≥ 40%;
  6. «косметологическая фабрика с дерма-приёмом» — КОСМ ≥ 50%
     при ДЕРМ-МЕД < 15% (дерматовенеролог — входная точка при
     эстетическом конвейере);
  7. «дерма в смешанном профиле» — всё остальное.
Пороги (20/50/15/40/50) — параметры, двигаются в одном месте.
"""

import collections
import statistics

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from src.oligo_analysis import load, SERVICE, UNMAPPED

XLSX = "output/ЧК_олигопрофильные_анализ_2026-09-24.xlsx"
DERM_MED = {"Дерматовенерология", "Трихология",
            "Удаление новообразований (дерматохирургия)", "Подология",
            "Онкология (онкодерматология)"}
WM = {"Гинекология", "Репродуктология", "Маммология", "Урология"}
MSK = {"Травматология/ортопедия", "Неврология", "Физиотерапия/массаж",
       "Остеопатия/мануальная/рефлексотерапия", "Реабилитация/ЛФК", "Нейрохирургия"}
OUT = "вне среза (нет дерматовенерологии)"


def classify(clinic_rows):
    tot = len(clinic_rows)
    cnt = collections.Counter(d for d, _, _ in clinic_rows)
    clin = {d: n for d, n in cnt.items() if d not in SERVICE and d != UNMAPPED}
    ctot = sum(clin.values())
    cs = ctot / tot if tot else 0
    sh = {d: n / ctot for d, n in clin.items()} if ctot else {}
    dom = max(sh.items(), key=lambda kv: kv[1]) if sh else None

    def g(S):
        return sum(v for d, v in sh.items() if d in S)

    derm, kosm = g(DERM_MED), sh.get("Косметология", 0)
    if cs < 0.20:
        return "сервисно-диагностическая с дерма-примесью", dom, cs, derm
    if derm >= 0.50:
        return "медицинская дерматология (ядро)", dom, cs, derm
    if g(WM) >= 0.40:
        return "дерма при женском/мужском здоровье", dom, cs, derm
    if derm >= 0.15:
        return "дермато-косметическая сбалансированная (аналог ЧК)", dom, cs, derm
    if g(MSK) >= 0.40:
        return "дерма при восстановительной медицине", dom, cs, derm
    if kosm >= 0.50:
        return "косметологическая фабрика с дерма-приёмом", dom, cs, derm
    return "дерма в смешанном профиле", dom, cs, derm


def build():
    rows = load()
    wb = openpyxl.load_workbook(XLSX)
    ws = wb["8_Олигопрофили_финансы"]
    head = [c.value for c in ws[5]]
    for name in ["Модель (группа)", "Доминанта клинического прайса",
                 "Доля дерм-мед в клин. прайсе"]:
        if name not in head:
            j = len(head) + 1
            ws.cell(5, j, name).font = Font(bold=True)
            ws.cell(5, j).fill = PatternFill("solid", fgColor="DDEBF7")
            ws.column_dimensions[get_column_letter(j)].width = 40
            head = [c.value for c in ws[5]]
    jm = head.index("Модель (группа)") + 1
    jc = head.index("Комбинация") + 1
    per = {}
    for i in range(6, ws.max_row + 1):
        inn = str(ws.cell(i, 1).value)
        if "Дерматовенерология" in str(ws.cell(i, jc).value or ""):
            model, dom, cs, derm = classify(rows.get(inn, []))
            per[inn] = (model, dom, cs, derm)
            ws.cell(i, jm, model)
            ws.cell(i, jm + 1, f"{dom[0]} {dom[1]:.0%} клин. прайса" if dom else "—")
            ws.cell(i, jm + 2, round(derm, 3))
        else:
            ws.cell(i, jm, OUT)
            ws.cell(i, jm + 1, None)
            ws.cell(i, jm + 2, None)
    ws.auto_filter.ref = f"A5:{get_column_letter(len(head))}{ws.max_row}"

    fin = {}
    for i in range(6, ws.max_row + 1):
        d = {head[j - 1]: ws.cell(i, j).value for j in range(1, len(head) + 1)}
        fin[str(d["ИНН"])] = d
    if "9_Группы_моделей" in wb.sheetnames:
        del wb["9_Группы_моделей"]
    w9 = wb.create_sheet("9_Группы_моделей")
    w9.append([f"Типология среза «олигопрофильные с дерматовенерологией в комбинации» — {len(per)} клиник."])
    w9.append(["Правила и пороги — src/oligo_models.py. Клиники листа 8 без дерматовенерологии в типологию не входят."])
    w9.append([])
    w9.append(["Модель", "Клиник", "Медиана выручки 2025 ₽", "Медиана рент. продаж %",
               "Медиана ROA %", "Медиана прайса ₽", "Медиана выручки на точку ₽",
               "Медиана доли клин. прайса", "Медиана доли дерм-мед"])
    for c in w9[4]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")

    def med(vals, r=0):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(statistics.median(vals), r) if vals else None

    groups = collections.defaultdict(list)
    for inn, (model, *_ ) in per.items():
        groups[model].append(inn)
    for model, inns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rs = [fin[i] for i in inns]
        w9.append([model, len(inns),
                   med([r["Выручка 2025 ₽ (ГИР БО)"] for r in rs]),
                   med([r["Рентабельность продаж 2025 %"] for r in rs], 1),
                   med([r["ROA 2025 %"] for r in rs], 1),
                   med([r["Медиана прайса ₽"] for r in rs]),
                   med([r["Выручка на точку ₽"] for r in rs]),
                   med([per[i][2] for i in inns], 2),
                   med([per[i][3] for i in inns], 2)])
    w9.append([])
    w9.append(["СОСТАВ ГРУПП"])
    w9.cell(w9.max_row, 1).font = Font(bold=True)
    w9.append(["Модель", "ИНН", "Компания", "Город", "Доминанта", "Дерм-мед",
               "Клин. прайс", "Комбинация"])
    for c in w9[w9.max_row]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")
    for model, inns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        for i in sorted(inns, key=lambda x: str(fin[x]["Компания"])):
            d = fin[i]
            _, dom, cs, derm = per[i]
            w9.append([model, i, d["Компания"], d["Город"],
                       f"{dom[0]} {dom[1]:.0%}" if dom else "—",
                       round(derm, 2), round(cs, 2), str(d["Комбинация"])[:120]])
    for i, w in enumerate([48, 14, 38, 17, 30, 10, 12, 80], 1):
        w9.column_dimensions[get_column_letter(i)].width = w
    wb.save(XLSX)
    return {m: len(v) for m, v in groups.items()}


if __name__ == "__main__":
    for m, n in sorted(build().items(), key=lambda kv: -kv[1]):
        print(f"{n:4d}  {m}")
