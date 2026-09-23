"""Типологические модели олигопрофильных клиник (задача заказчика 2026-09-23).

Каждая из клиник листа 8 относится ровно к одной модели. Признаки механические
и воспроизводимые, считаются от структуры прайса:
  · клин.прайс — доля позиций клинических направлений во всём прайсе клиники;
  · доли блоков считаются ВНУТРИ клинического прайса:
      ДЕРМ-МЕД  = дерматовенерология + трихология + удаление новообразований
                  + подология + онкодерматология
      КОСМ      = косметология
      ПЛАСТ     = пластическая хирургия
      Ж+М       = гинекология + репродуктология + маммология + урология
      ВОССТ     = травматология + неврология + физиотерапия + остеопатия
                  + реабилитация + нейрохирургия
      СТОМ, ПСИХО — одноимённые направления.

Правила (первое подходящее):
  0. «сервисно-диагностическая» — клин.прайс < 20% (организация живёт
     лабораторией/УЗИ/справками; клинические направления — примесь);
  1. «дермато-косметическая» — ДЕРМ-МЕД+КОСМ ≥ 50% и ДЕРМ-МЕД ≥ 10%
     (формат «Чистой Кожи»); подтип по доминанте: медицинская дерма
     или косметология;
  2. «косметологическая (чистая эстетика)» — ДЕРМ-МЕД+КОСМ+ПЛАСТ ≥ 50%,
     ДЕРМ-МЕД < 10%, ПЛАСТ < 10%;
  3. «эстетика + пластика» — КОСМ+ПЛАСТ ≥ 50% и ПЛАСТ ≥ 10%;
  4. «женское и мужское здоровье» — Ж+М ≥ 40%;
  5. «восстановительная медицина» — ВОССТ ≥ 40%;
  6. «стоматология+» — СТОМ ≥ 40%;
  7. «психиатрия/психология» — ПСИХО ≥ 40%;
  8. «узкая специализированная: <направление>» — иное одиночное направление
     держит ≥ 50% клинического прайса (ЛОР, флебология, офтальмология…);
  9. «смешанная малая поликлиника» — всё остальное.
"""

import collections
import statistics

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from src.oligo_analysis import load, SERVICE, UNMAPPED

XLSX = "output/ЧК_олигопрофильные_анализ_2026-09-22.xlsx"
DERM_MED = {"Дерматовенерология", "Трихология",
            "Удаление новообразований (дерматохирургия)", "Подология",
            "Онкодерматология"}
WM = {"Гинекология", "Репродуктология", "Маммология", "Урология"}
MSK = {"Травматология/ортопедия", "Неврология", "Физиотерапия/массаж",
       "Остеопатия/мануальная/рефлексотерапия", "Реабилитация/ЛФК", "Нейрохирургия"}


def classify(clinic_rows):
    tot = len(clinic_rows)
    cnt = collections.Counter(d for d, _, _ in clinic_rows)
    clin = {d: n for d, n in cnt.items() if d not in SERVICE and d != UNMAPPED}
    ctot = sum(clin.values())
    cs = ctot / tot if tot else 0
    if not ctot:
        return "сервисно-диагностическая", None, cs
    sh = {d: n / ctot for d, n in clin.items()}
    dom = max(sh.items(), key=lambda kv: kv[1])

    def g(S):
        return sum(v for d, v in sh.items() if d in S)

    derm, kosm, plast = g(DERM_MED), sh.get("Косметология", 0), sh.get("Пластическая хирургия", 0)
    if cs < 0.20:
        return "сервисно-диагностическая", dom, cs
    if derm + kosm >= 0.5 and derm >= 0.10:
        sub = "мед. дерматология" if derm >= kosm else "косметология-доминанта"
        return f"дермато-косметическая ({sub})", dom, cs
    if derm + kosm + plast >= 0.5 and derm < 0.10 and plast < 0.10:
        return "косметологическая (чистая эстетика)", dom, cs
    if kosm + plast >= 0.5 and plast >= 0.10:
        return "эстетика + пластика", dom, cs
    if g(WM) >= 0.4:
        return "женское и мужское здоровье", dom, cs
    if g(MSK) >= 0.4:
        return "восстановительная медицина (опорно-двигательная)", dom, cs
    if sh.get("Стоматология", 0) >= 0.4:
        return "стоматология+", dom, cs
    if sh.get("Психиатрия/психология", 0) >= 0.4:
        return "психиатрия/психология", dom, cs
    if dom[1] >= 0.5:
        return f"узкая специализированная: {dom[0]}", dom, cs
    return "смешанная малая поликлиника", dom, cs


def build():
    rows = load()
    wb = openpyxl.load_workbook(XLSX)
    ws = wb["8_Олигопрофили_финансы"]
    head = [c.value for c in ws[5]]
    if "Модель (группа)" not in head:
        base = len(head)
        for j, name in enumerate(["Модель (группа)", "Доминанта клинического прайса"], 1):
            ws.cell(5, base + j, name).font = Font(bold=True)
            ws.cell(5, base + j).fill = PatternFill("solid", fgColor="DDEBF7")
        ws.column_dimensions[get_column_letter(base + 1)].width = 44
        ws.column_dimensions[get_column_letter(base + 2)].width = 34
        head = [c.value for c in ws[5]]
    jm = head.index("Модель (группа)") + 1
    per = {}
    for i in range(6, ws.max_row + 1):
        inn = str(ws.cell(i, 1).value)
        model, dom, cs = classify(rows[inn])
        per[inn] = (model, dom, cs, i)
        ws.cell(i, jm, model)
        ws.cell(i, jm + 1, f"{dom[0]} {dom[1]:.0%} клин. прайса" if dom else "—")
        ws.auto_filter.ref = f"A5:{get_column_letter(len(head))}{ws.max_row}"

    # лист 9 — сводка по моделям с финансами
    fin = {}
    for i in range(6, ws.max_row + 1):
        d = {head[j - 1]: ws.cell(i, j).value for j in range(1, len(head) + 1)}
        fin[str(d["ИНН"])] = d
    if "9_Группы_моделей" in wb.sheetnames:
        del wb["9_Группы_моделей"]
    w9 = wb.create_sheet("9_Группы_моделей")
    w9.append(["Типология олигопрофильных клиник (158). Правила — в src/oligo_models.py; "
               "каждая клиника ровно в одной модели, признаки считаются от структуры прайса."])
    w9.append([])
    w9.append(["Модель", "Клиник", "Медиана выручки 2025 ₽", "Медиана рент. продаж %",
               "Медиана ROA %", "Медиана прайса ₽", "Медиана выручки на точку ₽",
               "Медиана доли клин. прайса"])
    for c in w9[3]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")

    def med(vals):
        vals = [v for v in vals if isinstance(v, (int, float))]
        return round(statistics.median(vals)) if vals else None

    groups = collections.defaultdict(list)
    for inn, (model, dom, cs, _) in per.items():
        groups[model].append(inn)
    for model, inns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        rowset = [fin[i] for i in inns]
        w9.append([model, len(inns),
                   med([r["Выручка 2025 ₽ (ГИР БО)"] for r in rowset]),
                   med([r["Рентабельность продаж 2025 %"] for r in rowset]),
                   med([r["ROA 2025 %"] for r in rowset]),
                   med([r["Медиана прайса ₽"] for r in rowset]),
                   med([r["Выручка на точку ₽"] for r in rowset]),
                   round(statistics.median([per[i][2] for i in inns]), 2)])
    w9.append([])
    w9.append(["СОСТАВ ГРУПП"])
    w9.cell(w9.max_row, 1).font = Font(bold=True)
    w9.append(["Модель", "ИНН", "Компания", "Город", "Доминанта", "Комбинация"])
    for c in w9[w9.max_row]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")
    for model, inns in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        for i in sorted(inns, key=lambda x: str(fin[x]["Компания"])):
            d = fin[i]
            dom = per[i][1]
            w9.append([model, i, d["Компания"], d["Город"],
                       f"{dom[0]} {dom[1]:.0%}" if dom else "—",
                       str(d["Комбинация"])[:120]])
    for i, w in enumerate([46, 14, 38, 18, 30, 90], 1):
        w9.column_dimensions[get_column_letter(i)].width = w
    wb.save(XLSX)
    return {m: len(v) for m, v in groups.items()}


if __name__ == "__main__":
    for m, n in sorted(build().items(), key=lambda kv: -kv[1]):
        print(f"{n:4d}  {m}")
