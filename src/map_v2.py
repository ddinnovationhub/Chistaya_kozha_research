"""Задача 2 (заказчик, 2026-09-23): маппинг направлений по чистым прайсам v2
С УЧЁТОМ КОНТЕКСТА КЛИНИКИ — не «слепая строка», а строка + раздел прайса +
лицензия юрлица (РЗН) + профиль остального прайса.

Ступень 1 — код (движок src/direction_map.classify: голд заказчика v4,
правила R1–R13, коды 804н, приёмы по специальности, раздел прайса).
Поверх — контекстная валидация:
  · направление строки ∉ специальностей лицензии ИНН (лицензия непуста) →
    флаг «вне лицензии»; для клиники, чей профиль прайса тоже не содержит
    этого направления (<2% позиций), уверенность понижается до «низ» —
    кандидат в спорные (пример заказчика: стоматологическая клиника не
    получает Дерматовенерологию от одной формулировки);
  · «удаление новообразований …» с анатомией другой специальности уже
    решается правилом R6 движка (локализация бьёт метод).

Выход: таблица directions_v2 в data/price_v2.db + промежуточная выгрузка
по первым 10 клиникам (ОБЯЗАТЕЛЬНАЯ ОСТАНОВКА CLAUDE.md — ждать заказчика).

Команды:
  python -m src.map_v2 run            # смаппить все позиции items_v2
  python -m src.map_v2 pilot10        # выгрузка первых 10 клиник и стоп
  python -m src.map_v2 stats          # сводка покрытия и флагов
"""

import datetime
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict

from src.direction_map import classify, norm

PRICE_DB = "data/price_v2.db"
OSINT_DB = "data/osint.db"

# направление движка → подстроки, ЛЮБАЯ из которых в тексте лицензии РЗН
# покрывает направление (несколько специальностей могут легально оказывать
# одно направление: удаление новообразований — дерматовенеролог/хирург/
# косметолог/онколог)
LIC_KEYS = {
    "Дерматовенерология": ["дерматовенеролог"],
    "Трихология": ["дерматовенеролог", "косметолог"],
    "Удаление новообразований (дерматохирургия)":
        ["дерматовенеролог", "хирург", "косметолог", "онколог"],
    "Онкология (онкодерматология)": ["онколог", "дерматовенеролог"],
    "Гинекология": ["гинеколог", "акушерств"],
    "Репродуктология": ["репродуктив", "акушерств", "гинеколог", "эмбриолог"],
    "Урология": ["уролог"],
    "Маммология": ["маммолог", "онколог", "хирург"],
    "Стоматология": ["стоматолог"],
    "ЛОР": ["оториноларинголог"],
    "Офтальмология/пластика век": ["офтальмолог", "пластическ", "хирург"],
    "Неврология": ["невролог"],
    "Хирургия": ["хирург"],
    "Хирургия/эндоскопия": ["эндоскоп", "хирург", "гастроэнтеролог"],
    "Проктология/хирургия": ["проктолог", "хирург"],
    "Пластическая хирургия": ["пластическ", "хирург"],
    "Травматология/ортопедия": ["травматолог", "ортопед"],
    "Психиатрия/психология": ["психиатр", "психотерап", "нарколог"],
    "Анестезиология": ["анестезиолог", "реаниматолог"],
    "УЗИ/лучевая": ["ультразвук", "рентген", "лучевой диагностик", "томограф"],
    "Функциональная диагностика": ["функциональной диагностик"],
    "Аллергология": ["аллерголог", "иммунолог", "лабораторн"],
    "Генетика": ["генетик", "лабораторн"],
    "Физиотерапия/массаж": ["физиотерап", "массаж", "лечебной физкультур",
                            "сестринск"],
    "Диетология": ["диетолог"],
}

# направления, для которых лицензионная проверка не применяется
# (обеспечивающие/агрегатные категории и брак)
NO_LIC_CHECK = {
    "Лаборатория", "Процедурный кабинет", "Вакцинация", "Справки/профосмотры",
    "Приём без указания специальности", "Приём смежного специалиста",
    "Брак разметки (UI-текст)", "Брак разметки (обрывок)", "Брак парсинга",
    "Спа", "Выездные услуги", "Превентивная/anti-age медицина",
    "Косметология",  # часто под «сестринское дело»/без отдельной строки
}


def lic_text(inn: str, o) -> str | None:
    """Склеенный текст специальностей всех медлицензий ИНН;
    None = расшифровки нет (проверка неприменима, флаг не ставится)."""
    rows = o.execute(
        "select specialties from rzn_licenses where inn=? and is_med=1 "
        "and specialties is not null and length(specialties)>3", (inn,)).fetchall()
    if not rows:
        return None
    return " ; ".join(r[0].lower() for r in rows)


def lic_covers(direction: str, text: str) -> bool:
    keys = LIC_KEYS.get(direction)
    if keys is None:
        return True              # направление без лицензионной проверки
    return any(k in text for k in keys)


def run():
    p = sqlite3.connect(PRICE_DB, timeout=60)
    o = sqlite3.connect(f"file:{OSINT_DB}?mode=ro", uri=True)
    p.execute("""CREATE TABLE IF NOT EXISTS directions_v2(
        domain TEXT, inn TEXT, name TEXT, section TEXT, direction TEXT,
        method TEXT, confidence TEXT, evidence TEXT, flags TEXT)""")
    p.execute("delete from directions_v2")

    rows = p.execute(
        "select domain, inn, name, coalesce(section,''), count(*) "
        "from items_v2 group by domain, inn, name, section").fetchall()
    # кэш classify по (имя, раздел)
    cache = {}
    licenses = {}
    # первый проход: базовый маппинг
    base = []
    for domain, inn, name, sec, k in rows:
        key = (norm(name), norm(sec))
        if key not in cache:
            cache[key] = classify(name, sec)
        base.append((domain, inn, name, sec, cache[key]))
    # профиль клиники: доли направлений по ИНН (по уникальным позициям)
    prof = defaultdict(Counter)
    for domain, inn, name, sec, (d, m, c, e) in base:
        if d:
            prof[inn][d] += 1
    # второй проход: контекстная валидация
    out = []
    flag_cnt = Counter()
    for domain, inn, name, sec, (d, m, c, e) in base:
        flags = []
        if d and d not in NO_LIC_CHECK and not d.startswith("Брак"):
            if inn not in licenses:
                licenses[inn] = lic_text(inn, o)
            lic = licenses[inn]
            if lic is not None and not lic_covers(d, lic):
                flags.append("вне лицензии")
                total = sum(prof[inn].values()) or 1
                share = prof[inn][d] / total
                if share < 0.02:
                    flags.append("вне профиля клиники (<2%)")
                    c = "низ"
        out.append((domain, inn, name, sec, d or "", m, c, e,
                    ";".join(flags)))
        for f in flags:
            flag_cnt[f] += 1
    p.executemany("insert into directions_v2 values (?,?,?,?,?,?,?,?,?)", out)
    p.commit()
    mapped = sum(1 for r in out if r[4] and not r[4].startswith("Брак"))
    print(f"строк (уник. имя×раздел×клиника): {len(out)}")
    print(f"смапплено: {mapped} ({mapped/len(out):.0%})")
    print(f"брак разметки: {sum(1 for r in out if r[4].startswith('Брак'))}")
    print(f"не смапплено: {sum(1 for r in out if not r[4])}")
    print("флаги:", dict(flag_cnt))


def pilot10():
    """Промежуточная выгрузка по первым 10 клиникам — и ОСТАНОВКА."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    p = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    o = sqlite3.connect(f"file:{OSINT_DB}?mode=ro", uri=True)
    comp = {i: (n, c) for i, n, c in o.execute("select inn, name, city from t40_companies")}
    # 10 клиник: разнообразие по размеру прайса и профилю
    cands = p.execute("""
        select inn, count(*) k, count(distinct domain) from directions_v2
        where inn != '' group by inn order by k desc""").fetchall()
    chosen, seen_kind = [], Counter()
    for inn, k, nd in cands:
        top = p.execute("""select direction, count(*) c from directions_v2
            where inn=? and direction!='' and direction not like 'Брак%'
            group by direction order by c desc limit 1""", (inn,)).fetchone()
        kind = top[0] if top else "?"
        if seen_kind[kind] >= 2 or k < 100:
            continue
        seen_kind[kind] += 1
        chosen.append(inn)
        if len(chosen) == 10:
            break
    H = Font(bold=True); FILL = PatternFill("solid", fgColor="DDEBF7")
    RED = PatternFill("solid", fgColor="FCE4E4")
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Позиции_10_клиник"
    ws.append(["ИНН", "Компания", "Город", "Домен", "Раздел прайса",
               "Название позиции", "Направление", "Уверенность", "Метод",
               "Улика", "Флаги"])
    for c_ in ws[1]: c_.font = H; c_.fill = FILL
    CLEAN = re.compile(r"[\x00-\x1f]")
    for inn in chosen:
        for dom, name, sec, d, m, conf, ev, fl in p.execute(
                """select domain, name, section, direction, method, confidence,
                   evidence, flags from directions_v2 where inn=?
                   order by section, name""", (inn,)):
            ws.append([inn, comp.get(inn, ("", ""))[0], comp.get(inn, ("", ""))[1],
                       dom, CLEAN.sub("", sec)[:120], CLEAN.sub("", name)[:200],
                       d, conf, m, CLEAN.sub("", ev)[:80], fl])
            if fl:
                for c_ in ws[ws.max_row]: c_.fill = RED
    ws.freeze_panes = "A2"; ws.auto_filter.ref = f"A1:K{ws.max_row}"
    for i, w in enumerate([12, 30, 14, 22, 30, 55, 26, 10, 22, 30, 28], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    w2 = wb.create_sheet("Сводка_по_клиникам")
    w2.append(["ИНН", "Компания", "Город", "Позиций", "Смапплено %",
               "Направления (доля ≥2%)", "Флагов «вне лицензии»",
               "Лицензия (специальности из РЗН)"])
    for c_ in w2[1]: c_.font = H; c_.fill = FILL
    for inn in chosen:
        rows = p.execute("select direction, flags from directions_v2 where inn=?",
                         (inn,)).fetchall()
        n = len(rows)
        mapped = sum(1 for d, f in rows if d and not d.startswith("Брак"))
        cnt = Counter(d for d, f in rows if d and not d.startswith("Брак"))
        dirs = ", ".join(f"{d} {c/max(mapped,1):.0%}" for d, c in cnt.most_common()
                         if c / max(mapped, 1) >= 0.02)
        lt = lic_text(inn, o)
        w2.append([inn, comp.get(inn, ("", ""))[0], comp.get(inn, ("", ""))[1],
                   n, round(mapped / max(n, 1), 2), dirs,
                   sum(1 for d, f in rows if f),
                   (lt[:220] if lt else "нет расшифровки")])
    for i, w in enumerate([12, 30, 14, 9, 11, 70, 12, 50], 1):
        w2.column_dimensions[get_column_letter(i)].width = w

    w3 = wb.create_sheet("Спорные")
    w3.append(["ИНН", "Компания", "Раздел", "Название", "Направление",
               "Уверенность", "Флаги", "Улика"])
    for c_ in w3[1]: c_.font = H; c_.fill = FILL
    for inn in chosen:
        for name, sec, d, conf, ev, fl in p.execute(
                """select name, section, direction, confidence, evidence, flags
                   from directions_v2 where inn=? and (flags != '' or
                   confidence='низ' or direction='') order by flags desc""", (inn,)):
            w3.append([inn, comp.get(inn, ("", ""))[0], CLEAN.sub("", sec)[:100],
                       CLEAN.sub("", name)[:150], d, conf, fl, CLEAN.sub("", ev)[:60]])
    for i, w in enumerate([12, 28, 28, 50, 24, 10, 28, 30], 1):
        w3.column_dimensions[get_column_letter(i)].width = w

    path = f"output/Маппинг_v2_пилот10_{datetime.date.today()}.xlsx"
    wb.save(path)
    print(path)
    print("клиники:", [f"{i} {comp.get(i, ('?',))[0][:30]}" for i in chosen])


def stats():
    p = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    n = p.execute("select count(*) from directions_v2").fetchone()[0]
    for d, k in p.execute("""select direction, count(*) from directions_v2
            group by direction order by 2 desc limit 25"""):
        print(f"{k:7} {d or '— не смапплено —'}")
    print("всего:", n)


if __name__ == "__main__":
    {"run": run, "pilot10": pilot10, "stats": stats}[sys.argv[1]]()
