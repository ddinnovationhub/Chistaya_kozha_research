# -*- coding: utf-8 -*-
"""Листы 6 / 6.1 / 7 на базе v2 (структура файла 2026-09-22 сохранена).

6_Удержание_вся_база — механизмы удержания по всем клиникам с прайсами:
  сводка (клиник, доля, позиций, медиана цены) + разрез по направлениям.
6.1_Удержание_все_позиции — ВСЕ позиции с механизмом (без отсечений,
  решение заказчика 2026-09-22).
7_Сочетания_направлений — полные комбинации / пары / тройки направлений
  олигопрофильных клиник; состав направлений — по правилу трёх источников
  (clinic_directions) + перечень ИНН по комбинациям.
"""

import collections
import itertools
import re
import statistics

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from src.oligo_analysis import XLSX, SERVICE, UNMAPPED, load, profile, seg_name
from src.v2_adapter import companies

H = Font(bold=True)
FILL = PatternFill("solid", fgColor="DDEBF7")

MECHS = [
    ("Годовое ведение / прикрепление",
     re.compile(r"годов(ое|ая|ой)|прикреплен|ведение (беременност|ребенка|"
                r"пациент)|наблюдение и ведение|программа ведения", re.I)),
    ("Подписка", re.compile(r"подписк", re.I)),
    ("Абонемент", re.compile(r"абонемент", re.I)),
    ("Курс процедур (≥2)",
     re.compile(r"курс\w*\s+(из\s+)?([2-9]|1[0-9])\s*(процедур|сеанс|посещен)|"
                r"([2-9]|1[0-9])\s*(процедур|сеанс)[а-я]*\s+курс|"
                r"курсом\s+([2-9]|1[0-9])", re.I)),
    ("Пакет / комплексная программа",
     re.compile(r"пакет|комплексн(ая|ое|ый) (программ|обследован|уход|чистк)|"
                r"программа [«\"']?", re.I)),
    ("Чек-ап / программа обследования",
     re.compile(r"чек[- ]?ап|check[- ]?up|скрининг|диспансеризац", re.I)),
    ("Депозит / клубная карта / бонусы / сертификат",
     re.compile(r"депозит|клубн(ая|ой) карт|бонус|сертификат", re.I)),
    ("Рассрочка / кредит", re.compile(r"рассрочк|кредит", re.I)),
]


def mech_of(name):
    for m, rx in MECHS:
        if rx.search(name):
            return m
    return None


def build():
    comp_db = companies()
    rows = load()
    composition = load.composition
    n_clinics = len(rows)

    # позиции с механизмами
    per_mech = collections.defaultdict(list)   # mech -> [(inn, d, name, price)]
    for inn, rr in rows.items():
        for d, price, name in rr:
            m = mech_of(name)
            if m:
                per_mech[m].append((inn, d, name, price))

    wb = openpyxl.load_workbook(XLSX)
    for sh in ("6_Удержание_вся_база", "6.1_Удержание_все_позиции",
               "7_Сочетания_направлений"):
        if sh in wb.sheetnames:
            del wb[sh]

    # ---------------- лист 6
    ws = wb.create_sheet("6_Удержание_вся_база")
    ws.append([f"БАЗА: все {n_clinics} клиник с прайсами (v2, 2026-09-24). "
               f"Механизмы удержания ищутся по названию позиции."])
    ws.append(["Курс = 2 и более процедур (одиночное «1 процедура» не механизм)."])
    ws.append(["Полные формулировки всех позиций — на листе 6.1, без отсечений."])
    ws.append([])
    ws.append(["Механизм", "Клиник", "Доля клиник", "Позиций", "Медиана цены ₽"])
    for c in ws[5]:
        c.font = H
        c.fill = FILL
    for m, _ in MECHS:
        items = per_mech.get(m, [])
        clinics = {i for i, *_ in items}
        prices = [p for *_, p in items if p]
        ws.append([m, len(clinics), round(len(clinics) / n_clinics, 3),
                   len(items),
                   round(statistics.median(prices)) if prices else None])
    ws.append([])
    ws.append(["РАЗРЕЗ ПО НАПРАВЛЕНИЯМ — ПОЛНЫЙ, без отсечения"])
    ws.append(["Механизм", "Направление", "Позиций", "Медиана цены ₽"])
    for c in ws[ws.max_row]:
        c.font = H
    for m, _ in MECHS:
        by_dir = collections.defaultdict(list)
        for inn, d, name, price in per_mech.get(m, []):
            by_dir[d].append(price)
        for d, ps in sorted(by_dir.items(), key=lambda x: -len(x[1])):
            good = [p for p in ps if p]
            ws.append([m, d, len(ps),
                       round(statistics.median(good)) if good else None])
    for i, w in enumerate([44, 10, 12, 10, 16], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # ---------------- лист 6.1
    w61 = wb.create_sheet("6.1_Удержание_все_позиции")
    w61.append(["Механизм", "ИНН", "Компания", "Город", "Направление",
                "Название услуги (дословно)", "Цена ₽", "Флаг качества"])
    for c in w61[1]:
        c.font = H
        c.fill = FILL
    CLEAN = re.compile(r"[\x00-\x1f]")
    n61 = 0
    for m, _ in MECHS:
        for inn, d, name, price in sorted(per_mech.get(m, [])):
            nm, city, _ = comp_db.get(inn, ("", "", ""))
            w61.append([m, inn, nm, city, d, CLEAN.sub("", name)[:250],
                        price, "—"])
            n61 += 1
    w61.freeze_panes = "A2"
    w61.auto_filter.ref = f"A1:H{w61.max_row}"
    for i, w in enumerate([40, 13, 34, 15, 26, 80, 10, 12], 1):
        w61.column_dimensions[get_column_letter(i)].width = w

    # ---------------- лист 7
    prof = {}
    for inn, rr in rows.items():
        tot, cnt, dirs = profile(rr, composition.get(inn, set()))
        prof[inn] = (tot, cnt, dirs, seg_name(len(dirs)))
    oli = {i: v for i, v in prof.items() if v[3].startswith("олиго")}
    combos = collections.Counter()
    combo_inns = collections.defaultdict(list)
    pairs = collections.Counter()
    triples = collections.Counter()
    for inn, (tot, cnt, dirs, seg) in oli.items():
        key = " + ".join(dirs)
        combos[key] += 1
        combo_inns[key].append(inn)
        for a, b in itertools.combinations(dirs, 2):
            pairs[f"{a} + {b}"] += 1
        for t in itertools.combinations(dirs, 3):
            triples[" + ".join(t)] += 1

    w7 = wb.create_sheet("7_Сочетания_направлений")
    w7.append([f"Сочетания направлений у {len(oli)} олигопрофильных клиник "
               f"(состав — по правилу трёх источников: сайт+прайс, вето лицензии)"])
    w7.append([f"Уникальных полных комбинаций: {len(combos)}; "
               f"пар: {len(pairs)}; троек: {len(triples)} — все, без отсечений"])
    w7.append([])
    w7.append([f"ПОЛНЫЕ КОМБИНАЦИИ (весь профиль клиники) — все {len(combos)}"])
    w7.append(["Комбинация", "Направлений", "Клиник", "Доля"])
    for c in w7[w7.max_row]:
        c.font = H
        c.fill = FILL
    for k, n in combos.most_common():
        w7.append([k, k.count(" + ") + 1, n, round(n / len(oli), 3)])
    w7.append([])
    w7.append([f"ПАРЫ НАПРАВЛЕНИЙ — все {len(pairs)}"])
    w7.append(["Пара", "", "Клиник", "Доля"])
    for c in w7[w7.max_row]:
        c.font = H
    for k, n in pairs.most_common():
        w7.append([k, "", n, round(n / len(oli), 3)])
    w7.append([])
    w7.append([f"ТРОЙКИ НАПРАВЛЕНИЙ — все {len(triples)}"])
    w7.append(["Тройка", "", "Клиник", "Доля"])
    for c in w7[w7.max_row]:
        c.font = H
    for k, n in triples.most_common():
        w7.append([k, "", n, round(n / len(oli), 3)])
    w7.append([])
    w7.append(["ИНН по полным комбинациям"])
    w7.append(["Комбинация", "ИНН (через ;)"])
    for c in w7[w7.max_row]:
        c.font = H
    for k, n in combos.most_common():
        w7.append([k, "; ".join(combo_inns[k])])
    for i, w in enumerate([100, 13, 10, 10], 1):
        w7.column_dimensions[get_column_letter(i)].width = w

    # порядок листов
    order = ["1_Сегменты_клиник", "2_Комплементарность",
             "3_Медианы_клиника_направление", "4_Дорогие_услуги",
             "6_Удержание_вся_база", "6.1_Удержание_все_позиции",
             "7_Сочетания_направлений", "8_Олигопрофили_финансы",
             "9_Группы_моделей"]
    wb._sheets = ([wb[s] for s in order if s in wb.sheetnames]
                  + [ws for ws in wb._sheets if ws.title not in order])
    wb.save(XLSX)
    print(f"листы 6/6.1/7: механизмов-позиций {n61}, олиго {len(oli)}, "
          f"комбинаций {len(combos)}, пар {len(pairs)}, троек {len(triples)}")


if __name__ == "__main__":
    build()
