"""Пересборка листов 1–4 выгрузки «ЧК_олигопрофильные_анализ».

Единица анализа — юрлицо (ИНН) с прайсом. Направление позиции берётся из
data/directions.db по ключу norm(strip_ui(name_raw)); позиции с флагом
«Брак парсинга (цена)» в ценовые метрики не идут.

Сегмент клиники считается по числу КЛИНИЧЕСКИХ направлений. Решение
заказчика 2026-09-23: НЕ считаются направлениями только УЗИ/лучевая,
Вакцинация, Функциональная диагностика, Справки/профосмотры, Лаборатория,
Анестезиология (плюс не-специализации: брак разметки, брак парсинга,
неклассифицированное, приём без указания специальности, процедурный
кабинет, спа, выездные услуги). Генетика и репродуктология — клинические.
ПОРОГА ПРЕДСТАВЛЕННОСТИ НЕТ: любая позиция клинического направления
засчитывает его клинике (прежний порог ≥3 позиций / ≥2% отменён
заказчиком 2026-09-23 — из-за него специализации с 1–2 позициями
выпадали из комбинации).

Комплементарность: lift = (доля клиник среза с направлением) /
(доля клиник базы с ним), значимость — точный тест Фишера (через lgamma,
иначе переполнение). Вывод не делается при <10 клиник в базе и <5 в срезе.
"""

import collections
import math
import sqlite3
import statistics

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from src.direction_map import norm, strip_ui

XLSX = "output/ЧК_олигопрофильные_анализ_2026-09-22.xlsx"
# сервисные — закрытый список заказчика (2026-09-23): «УЗИ, вакцинации,
# функциональную диагностику, справки, лаборатории, анестезиологию не выносим»
SERVICE = {
    "Лаборатория", "УЗИ/лучевая", "Функциональная диагностика",
    "Справки/профосмотры", "Вакцинация", "Анестезиология",
}
# не специализации вовсе: качество данных и форматы обслуживания
NONSPEC = {
    "Брак разметки (UI-текст)", "Брак разметки (обрывок)", "Брак парсинга (цена)",
    "Приём без указания специальности", "Приём смежного специалиста",
    "Процедурный кабинет", "Спа", "Выездные услуги",
}
SERVICE = SERVICE | NONSPEC
UNMAPPED = "— НЕ КЛАССИФИЦИРОВАНО —"
H = Font(bold=True)
FILL = PatternFill("solid", fgColor="DDEBF7")


def seg_name(k):
    return ("без клинического профиля" if k == 0 else
            "монопрофильная (1-2)" if k <= 2 else
            "олигопрофильная (3–6)" if k <= 6 else "многопрофильная (7+)")


def load():
    dd = sqlite3.connect("file:data/directions.db?mode=ro", uri=True)
    dmap = {k: v for k, v in dd.execute("select name_norm, direction from name_directions")}
    p = sqlite3.connect("file:data/prices.db?mode=ro", uri=True)
    bad = {r[0] for r in p.execute("select price_item_id from price_flags")}
    comp = {}
    for inn, name, city, region in p.execute(
            "select distinct inn, null, null, null from price_items limit 0"):
        pass
    rows = collections.defaultdict(list)          # инн -> [(направление, цена, название)]
    for pid, inn, name, price in p.execute(
            "select id, inn, name_raw, price_value from price_items"):
        d = dmap.get(norm(strip_ui(name or ""))) or UNMAPPED
        rows[inn].append((d, None if (price and pid in bad) else price, name or ""))
    return rows


def meta_from_sheet(wb):
    out = {}
    for r in wb["1_Сегменты_клиник"].iter_rows(min_row=2, values_only=True):
        if r[0]:
            out[str(r[0])] = (r[1], r[2], r[3])   # компания, город, регион
    return out


def profile(rows_of_clinic):
    tot = len(rows_of_clinic)
    cnt = collections.Counter(d for d, _, _ in rows_of_clinic)
    dirs = [d for d, n in cnt.items() if d not in SERVICE and d != UNMAPPED]
    return tot, cnt, sorted(dirs)


def fisher_p(a, b, c, d):
    """Двусторонний точный тест Фишера через логарифмы факториалов."""
    def lf(n):
        return math.lgamma(n + 1)
    n = a + b + c + d
    lp0 = (lf(a + b) + lf(c + d) + lf(a + c) + lf(b + d) - lf(n)
           - lf(a) - lf(b) - lf(c) - lf(d))
    total = 0.0
    for x in range(0, min(a + b, a + c) + 1):
        y, z, w = a + b - x, a + c - x, d - (a - x)
        if y < 0 or z < 0 or w < 0:
            continue
        lp = (lf(a + b) + lf(c + d) + lf(a + c) + lf(b + d) - lf(n)
              - lf(x) - lf(y) - lf(z) - lf(w))
        if lp <= lp0 + 1e-9:
            total += math.exp(lp)
    return min(total, 1.0)


def build():
    wb = openpyxl.load_workbook(XLSX)
    meta = meta_from_sheet(wb)
    rows = load()

    prof = {}
    for inn, rr in rows.items():
        tot, cnt, dirs = profile(rr)
        prof[inn] = (tot, cnt, dirs, seg_name(len(dirs)))

    # ---------------- лист 1
    del wb["1_Сегменты_клиник"]
    ws = wb.create_sheet("1_Сегменты_клиник", 0)
    ws.append(["ИНН", "Компания", "Город", "Регион", "Позиций в прайсе", "Покрытие маппингом",
               "Клинических направлений", "Сегмент", "Есть дерматовенерология",
               "Есть дерм-контур", "Направления (по объёму)"])
    for c in ws[1]:
        c.font = H
        c.fill = FILL
    DERM = {"Дерматовенерология", "Косметология", "Трихология",
            "Удаление новообразований (дерматохирургия)", "Онкодерматология", "Подология"}
    for inn, (tot, cnt, dirs, seg) in sorted(prof.items(),
                                             key=lambda kv: -kv[1][0]):
        comp, city, region = meta.get(inn, (None, None, None))
        cov = 1 - cnt.get(UNMAPPED, 0) / tot if tot else None
        by_vol = "; ".join(d for d, _ in sorted(((d, cnt[d]) for d in dirs),
                                                key=lambda x: -x[1]))
        ws.append([inn, comp, city, region, tot, round(cov, 3) if cov is not None else None,
                   len(dirs), seg,
                   "да" if "Дерматовенерология" in dirs else "нет",
                   "да" if DERM & set(dirs) else "нет", by_vol])
    for i, w in enumerate([13, 38, 17, 24, 16, 18, 22, 24, 22, 18, 90], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{ws.max_row}"

    # ---------------- лист 2
    oli = {i: v for i, v in prof.items() if v[3].startswith("олиго")}
    sel = {i: v for i, v in oli.items() if "Дерматовенерология" in v[2]}
    base_n, sel_n = len(oli), len(sel)
    cnt_base = collections.Counter(d for v in oli.values() for d in v[2])
    cnt_sel = collections.Counter(d for v in sel.values() for d in v[2])
    del wb["2_Комплементарность"]
    w2 = wb.create_sheet("2_Комплементарность", 1)
    for line in [
        "КАК ЧИТАТЬ ЭТОТ ЛИСТ",
        "Lift = (доля клиник среза, у которых есть направление) ÷ (доля таких клиник во всей базе сравнения).",
        "lift > 1 — направление тяготеет к дерматовенерологии; lift < 1 — избегает её.",
        "p — точный тест Фишера: вероятность увидеть такую связь случайно.",
        "Вывод НЕ делается, если направление встречается менее чем у 10 клиник базы и менее чем у 5 в срезе.",
        "Отрицательный вывод при малом n в срезе допустим, если направление часто встречается в базе.",
        "",
    ]:
        w2.append([line])
    w2.append([f"ОЛИГОПРОФИЛЬНЫЕ (3–6) С ДЕРМАТОВЕНЕРОЛОГИЕЙ", f"клиник в срезе: {sel_n}",
               f"база сравнения: {base_n}"])
    w2.cell(w2.max_row, 1).font = H
    w2.append(["Направление", "Клиник в срезе", "Доля в срезе", "Клиник в базе", "Доля в базе",
               "Lift", "p (тест Фишера)", "Вывод"])
    for c in w2[w2.max_row]:
        c.font = H
        c.fill = FILL
    res = []
    for d, nb in cnt_base.items():
        if d == "Дерматовенерология":
            continue
        ns = cnt_sel.get(d, 0)
        ps, pb = ns / sel_n, nb / base_n
        lift = round(ps / pb, 2) if pb else None
        p = fisher_p(ns, sel_n - ns, nb - ns, base_n - sel_n - (nb - ns))
        if nb < 10 and ns < 5:
            verdict = "вывод не делается: слишком мало клиник"
        elif p >= 0.05:
            verdict = "связь не значима"
        elif lift >= 1.5:
            verdict = "сильно комплементарно"
        elif lift > 1:
            verdict = "комплементарно"
        elif lift == 0:
            verdict = "не встречается вместе"
        else:
            verdict = "антикомплементарно"
        res.append((d, ns, round(ps, 3), nb, round(pb, 3), lift, round(p, 4), verdict))
    for row in sorted(res, key=lambda x: (-(x[5] or 0), -x[1])):
        w2.append(list(row))
    for i, w in enumerate([40, 16, 14, 14, 14, 10, 16, 34], 1):
        w2.column_dimensions[get_column_letter(i)].width = w

    # ---------------- лист 3
    del wb["3_Медианы_клиника_направление"]
    w3 = wb.create_sheet("3_Медианы_клиника_направление", 2)
    w3.append(["ИНН", "Компания", "Город", "Сегмент", "Направление", "Позиций",
               "С ценой", "Медиана ₽", "p25 ₽", "p75 ₽", "Максимум ₽"])
    for c in w3[1]:
        c.font = H
        c.fill = FILL
    for inn, (tot, cnt, dirs, seg) in sorted(prof.items(), key=lambda kv: kv[1][3]):
        if not seg.startswith("олиго"):
            continue
        comp, city, _ = meta.get(inn, (None, None, None))
        per = collections.defaultdict(list)
        for d, price, _ in rows[inn]:
            per[d].append(price)
        for d, prices in sorted(per.items(), key=lambda kv: -len(kv[1])):
            vals = sorted(p for p in prices if p)
            q = (lambda f: vals[min(int(len(vals) * f), len(vals) - 1)]) if vals else None
            w3.append([inn, comp, city, seg, d, len(prices), len(vals),
                       round(statistics.median(vals)) if vals else None,
                       q(0.25) if vals else None, q(0.75) if vals else None,
                       max(vals) if vals else None])
    for i, w in enumerate([13, 38, 17, 24, 36, 10, 10, 12, 12, 12, 14], 1):
        w3.column_dimensions[get_column_letter(i)].width = w
    w3.freeze_panes = "A2"
    w3.auto_filter.ref = f"A1:K{w3.max_row}"

    # ---------------- лист 4
    del wb["4_Дорогие_услуги"]
    w4 = wb.create_sheet("4_Дорогие_услуги", 3)
    w4.append(["ИНН", "Компания", "Город", "Направление", "Услуга (дословно)", "Цена ₽",
               "Профиль клиники"])
    for c in w4[1]:
        c.font = H
        c.fill = FILL
    pool = []
    for inn, (tot, cnt, dirs, seg) in prof.items():
        if not seg.startswith("олиго"):
            continue
        comp, city, _ = meta.get(inn, (None, None, None))
        for d, price, name in rows[inn]:
            if price:
                pool.append((price, inn, comp, city, d, name, "; ".join(dirs)))
    pool.sort(key=lambda x: -x[0])
    for price, inn, comp, city, d, name, dirs_s in pool[:600]:
        w4.append([inn, comp, city, d, name[:180], price, dirs_s])
    for i, w in enumerate([13, 34, 16, 32, 80, 12, 60], 1):
        w4.column_dimensions[get_column_letter(i)].width = w
    w4.freeze_panes = "A2"

    order = ["1_Сегменты_клиник", "2_Комплементарность", "3_Медианы_клиника_направление",
             "4_Дорогие_услуги", "6_Удержание_вся_база", "6.1_Удержание_все_позиции",
             "7_Сочетания_направлений", "8_Олигопрофили_финансы"]
    wb._sheets.sort(key=lambda s: order.index(s.title) if s.title in order else 99)
    wb.save(XLSX)
    segs = collections.Counter(v[3] for v in prof.values())
    return segs, sel_n


if __name__ == "__main__":
    segs, sel_n = build()
    print("сегменты:", dict(segs))
    print("олигопрофильных с дерматовенерологией:", sel_n)
