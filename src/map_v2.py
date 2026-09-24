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

Выход: таблица directions_v2 в data/directions_v2.db + промежуточная выгрузка
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
DIR_DB = "data/directions_v2.db"

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
    "Кардиология": ["кардиолог"],
    "Гастроэнтерология": ["гастроэнтеролог", "терапи"],
    "Эндокринология": ["эндокринолог"],
    "Гематология": ["гематолог", "лабораторн"],
    "Ревматология": ["ревматолог", "терапи"],
    "Нефрология": ["нефролог", "уролог", "терапи"],
    "Пульмонология": ["пульмонолог", "терапи"],
    "Инфекционные болезни": ["инфекц", "терапи"],
    "Педиатрия": ["педиатр"],
    "Терапия": ["терапи"],
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


# --- слой «раздел сайта = направление» (заказчик, 2026-09-24: «будет раздел,
# а внутри него услуги — так тебе и надо промаппить») + доборные словари
# для несмаппленного остатка. Слои НЕ трогают движок direction_map (голд).

from src.site_dirs import map_nav  # словарь названий направлений

LAB_SEC = re.compile(
    r"фермент|обмен (белк|углевод|липид|железа|пигмент)|гормон|биохими|"
    r"коагул|гемостаз|витамин|микроэлемент|онкомаркер|маркер|аллерген|"
    r"диагностика анеми|пренатальн|иммунологические|серологи|гематологи|"
    r"общеклиническ|анализ|исследовани[ея] (крови|мочи|кала)|лаборатор|"
    r"инфекции|torch|специфические белки", re.I)
LAB_NAME = re.compile(
    r"белок|белков|альбумин|глобулин|фосфатаза|трансфераза|трансферрин|"
    r"липопротеин|аполипопротеин|триглицерид|холинэстераза|амилаза|липаза|"
    r"лактат|молочная кислота|мочевая кислота|билирубин|креатинин|мочевина|"
    r"ферритин|жсс\b|железосвязывающ|гаптоглобин|церулоплазмин|гомоцистеин|"
    r"прокальцитонин|с[- ]реактивн|срб\b|фибриноген|протромбин|мно\b|ачтв|"
    r"д[- ]димер|антитромбин|калий|натрий|кальций|магний|фосфор|хлориды|"
    r"цинк|медь|селен|литий|эстриол|эстрадиол|кортизол|тестостерон|"
    r"прогестерон|пролактин|альдостерон|паратгормон|кальцитонин|"
    r"соматотропн|андростендион|дгэа|17[- ]он|игф|инсулин|с[- ]пептид|"
    r"тиреоглобулин|антистрептолизин|ревматоидный фактор|иммуноглобулин|"
    r"комплемент[а]? c\d|криоглобулин|электрофорез|осадок мочи|"
    r"копрограмм|кальпротектин|эластаза|скрытую кровь|фракции|"
    r"лактальбумин|казеин|овальбумин|миоглобин|тропонин|кфк|лдг\b|"
    r"щелочная фосфатаза|ггт\b|алт\b|аст\b|железо в сыворотке|"
    r"фолиевая кислота|цианокобаламин|25[- ]он|1,25|"
    r"антитрипсин|гликопротеин|протеин|зонулин|енолаза|фно[- ]альфа|"
    r"некроза опухоли|интерлейкин|липидограмм|коагулограмм|иммунограмм|"
    r"спермограмм|papp[- ]a|p1np|кальпротектин|остеокальцин|"
    r"исследовани[ея] (кала|крови|мочи|мокроты|ликвора)|"
    r"получение (мазк|соскоб)|взятие (мазк|соскоб|биоматериал|крови)|"
    r"лекарственный мониторинг|ламотриджин|вальпроев|карбамазепин|"
    r"такролимус|циклоспорин|бактерицидная активность|фагоцит|"
    r"иммунный статус|клостриди|difficile|токсин[ыа]?\b", re.I)
EXTRA_RULES = [
    ("Дерматовенерология", re.compile(
        r"(соскоб|биопси[яи]).{0,15}кож|кожи и слизистых|дерматоскоп", re.I)),
    ("Маммология", re.compile(r"молочн(ой|ых) желез", re.I)),
    ("Гинекология", re.compile(
        r"синехи|девственной плевы|пессари|кольпоскоп|цервикометр|"
        r"интравагинальн|влагалищ|вульв|половых губ|лабиопластик|гистероскоп|гистеросальпинго|"
        r"эндометри|яичник|маточн", re.I)),
    ("Урология", re.compile(
        r"цистостом|нефростом|уретральн|уретры|мочевого пузыря|простаты|"
        r"мочевыводящих|крайней плоти|семенного канатика|мошонк|"
        r"полового члена|оболочек яичка", re.I)),
    ("Травматология/ортопедия", re.compile(
        r"(пункция|аспирация|блокада).{0,15}сустав|паравертебральн", re.I)),
    ("УЗИ/лучевая", re.compile(
        r"нейросонограф|соногра|эластометр|эластограф|денситометр|"
        r"допплерометр|цдк\b|скт\b", re.I)),
    ("Офтальмология/пластика век", re.compile(
        r"очков|линз|рефрактометр|тонометрия глаз|глазного дна|"
        r"остроты зрения|биомикроскоп|офтальмоскоп", re.I)),
    ("ЛОР", re.compile(
        r"слизистой носа|носовых пазух|внутриносов|септопластик|миндалин|"
        r"аденоид|барабанн|слухов|серных пробок|тимпано|гайморит|параценте",
        re.I)),
    ("Хирургия", re.compile(
        r"вторичных швов|снятие швов|хирургическ|иссечение|вскрытие "
        r"(абсцесс|фурункул|панариц|гематом)|некрэктоми|гигром", re.I)),
    ("Процедурный кабинет", re.compile(
        r"аутогемотерап|внутримышечн|внутривенн|подкожн|инъекци|"
        r"капельниц|забор крови", re.I)),
    ("Функциональная диагностика", re.compile(
        r"экг\b|электрокардиогр|спирометр|спирогр|холтер|смад\b|"
        r"велоэргометр|ээг\b|электроэнцефалогр|энмг|электронейромиогр", re.I)),
    ("Стоматология", re.compile(
        r"зуба|зубов|кариес|пломб|коронк|эндодонт|пародонт", re.I)),
]


def fallback_direction(name: str, section: str):
    """Доборные слои для остатка после движка."""
    if section:
        d = map_nav(section)
        if d:
            return d, "раздел = направление сайта", "выс", f"раздел «{section[:40]}»"
        if LAB_SEC.search(section):
            return ("Лаборатория", "код: лаб-раздел", "сред",
                    f"раздел «{section[:40]}»")
    if LAB_NAME.search(name):
        return "Лаборатория", "код: лаб-аналит", "сред", "аналит в названии"
    for d, rx in EXTRA_RULES:
        m = rx.search(name)
        if m:
            return d, "код: доборный ключ", "сред", f"ключ «{m.group(0)[:30]}»"
    d = map_nav(name)
    if d:
        return d, "код: название направления", "сред", "имя = направление"
    return None


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
    p = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    o = sqlite3.connect(f"file:{OSINT_DB}?mode=ro", uri=True)
    dd = sqlite3.connect(DIR_DB, timeout=60)
    dd.execute("""CREATE TABLE IF NOT EXISTS directions_v2(
        domain TEXT, inn TEXT, name TEXT, section TEXT, direction TEXT,
        method TEXT, confidence TEXT, evidence TEXT, flags TEXT)""")
    dd.execute("delete from directions_v2")

    rows = p.execute(
        "select domain, inn, name, coalesce(section,''), count(*) "
        "from items_v2 group by domain, inn, name, section").fetchall()
    # кэш classify по (имя, раздел)
    cache = {}
    licenses = {}
    # первый проход: базовый маппинг.
    # Порядок (заказчик, 2026-09-24): раздел сайта, называющий направление,
    # бьёт всё, кроме UI-мусора («сайт сам промаппил»); конфликт с движком
    # по имени — во флаг. Затем движок, затем доборные слои (лаборатория,
    # процедурные ключи).
    def full_classify(name, sec):
        d, m, c, e = classify(name, sec)
        conflict = ""
        sec_d = map_nav(sec) if sec else None
        if d and d.startswith("Брак"):
            # раздел спасает непонятное имя (заказчик, 2026-09-24:
            # «Первичный прием» в разделе «услуги дерматовенеролога»)
            if sec_d:
                d, m, c, e = (sec_d, "раздел спасает имя", "сред",
                              f"имя «{name[:30]}», раздел «{sec[:40]}»")
        elif sec_d:
            if d and d != sec_d:
                conflict = f"раздел≠имя (движок: {d})"
            d, m, c, e = (sec_d, "раздел = направление сайта", "выс",
                          f"раздел «{sec[:40]}»")
        elif not d:
            fb = fallback_direction(name, sec)
            if fb:
                d, m, c, e = fb
        return d, m, c, e, conflict

    base = []
    for domain, inn, name, sec, k in rows:
        key = (norm(name), norm(sec))
        if key not in cache:
            cache[key] = full_classify(name, sec)
        base.append((domain, inn, name, sec, cache[key]))
    # наследование по большинству раздела (заказчик, 2026-09-24: «зачастую
    # понятно всё из раздела прайса»): blank в разделе, где ≥60% уже
    # смаппленных соседей (и их ≥5) дают одно направление, наследует его
    sec_major = defaultdict(Counter)
    for domain, inn, name, sec, (d, m, c, e, cf) in base:
        if d and not d.startswith("Брак") and sec:
            sec_major[(domain, sec)][d] += 1
    inherited = 0
    for i, (domain, inn, name, sec, (d, m, c, e, cf)) in enumerate(base):
        if d or not sec:
            continue
        cnt = sec_major.get((domain, sec))
        if not cnt:
            continue
        top, k2 = cnt.most_common(1)[0]
        if k2 >= 5 and k2 / sum(cnt.values()) >= 0.6:
            base[i] = (domain, inn, name, sec,
                       (top, "по большинству раздела", "сред",
                        f"{k2}/{sum(cnt.values())} соседей раздела", cf))
            inherited += 1
    print(f"унаследовано по большинству раздела: {inherited}")
    # профиль клиники: доли направлений по ИНН (по уникальным позициям)
    prof = defaultdict(Counter)
    for domain, inn, name, sec, (d, m, c, e, cf) in base:
        if d:
            prof[inn][d] += 1
    # второй проход: контекстная валидация
    out = []
    flag_cnt = Counter()
    for domain, inn, name, sec, (d, m, c, e, cf) in base:
        flags = [cf] if cf else []
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
    dd.executemany("insert into directions_v2 values (?,?,?,?,?,?,?,?,?)", out)
    dd.commit()
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
    dd2 = sqlite3.connect(f"file:{DIR_DB}?mode=ro", uri=True)
    cands = dd2.execute("""
        select inn, count(*) k, count(distinct domain) from directions_v2
        where inn != '' group by inn order by k desc""").fetchall()
    chosen, seen_kind = [], Counter()
    for inn, k, nd in cands:
        top = dd2.execute("""select direction, count(*) c from directions_v2
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
        for dom, name, sec, d, m, conf, ev, fl in dd2.execute(
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
        rows = dd2.execute("select direction, flags from directions_v2 where inn=?",
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

    # --- сверка трёх источников: сайт × прайс × лицензия
    dd = sqlite3.connect(f"file:{DIR_DB}?mode=ro", uri=True) \
        if __import__("os").path.exists(DIR_DB) else None
    w4 = wb.create_sheet("Сайт_vs_прайс")
    w4.append(["ИНН", "Компания", "Направления с сайта (навигация/страница)",
               "На сайте ЕСТЬ, в прайсе НЕТ (<2%)", "В прайсе ЕСТЬ (≥2%), на сайте НЕТ"])
    for c_ in w4[1]: c_.font = H; c_.fill = FILL
    if dd is not None:
        for inn in chosen:
            site = {r[0] for r in dd.execute(
                "select distinct direction from site_dirs where inn=?", (inn,))}
            rows = dd2.execute("""select direction, count(*) from directions_v2
                where inn=? and direction!='' and direction not like 'Брак%'
                group by direction""", (inn,)).fetchall()
            tot = sum(k for _, k in rows) or 1
            price_big = {d for d, k in rows if k / tot >= 0.02}
            price_all = {d for d, k in rows}
            w4.append([inn, comp.get(inn, ("", ""))[0],
                       ", ".join(sorted(site)) or "не найдены",
                       ", ".join(sorted(site - price_all)) or "—",
                       ", ".join(sorted(price_big - site)) or "—"])
    for i, w in enumerate([12, 30, 70, 50, 50], 1):
        w4.column_dimensions[get_column_letter(i)].width = w

    w3 = wb.create_sheet("Спорные")
    w3.append(["ИНН", "Компания", "Раздел", "Название", "Направление",
               "Уверенность", "Флаги", "Улика"])
    for c_ in w3[1]: c_.font = H; c_.fill = FILL
    for inn in chosen:
        for name, sec, d, conf, ev, fl in dd2.execute(
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
    dd2 = sqlite3.connect(f"file:{DIR_DB}?mode=ro", uri=True)
    n = dd2.execute("select count(*) from directions_v2").fetchone()[0]
    for d, k in dd2.execute("""select direction, count(*) from directions_v2
            group by direction order by 2 desc limit 25"""):
        print(f"{k:7} {d or '— не смапплено —'}")
    print("всего:", n)


if __name__ == "__main__":
    {"run": run, "pilot10": pilot10, "stats": stats}[sys.argv[1]]()
