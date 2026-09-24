# -*- coding: utf-8 -*-
"""Классификация клиник по моделям М0–М6 (документ заказчика
«Модели эволюции бизнес-модели "Чистой кожи"», 2026-09-24).

Методика документа, формализованная на нашей базе:
  · Дерматологическое ядро: есть/нет/неясно — по составу направлений
    (дерм-контур) и позициям прайса.
  · На каждую модель — 5 признаков (правило «существенно ≥3 из 5»):
      1) раздел/посадочная на сайте  → направление модели в навигации
         (site_dirs) или профильный раздел прайса;
      2) профильный врач             → приём профильного специалиста в прайсе;
      3) диагностика                 → диагностические услуги модели в прайсе;
      4) несколько услуг/этапов      → ≥5 позиций модели И (диагностика+лечение);
      5) позиционирование            → доля модели ≥5% клинического прайса.
  · Уровень зрелости: 0 — позиции есть, признаков <2; 1 — 2 признака;
    2 — ≥3 признаков; 3 — ≥4 признаков и доля ≥20% (второе ядро).
  · Ведущая модель — максимальная по (уровень, доля); М0 — базовая, ведущая
    когда есть дерм-ядро и ни одна из М1–М6 не достигла уровня 2.
  · Доказательства — дословные формулировки позиций + URL страницы.
"""

import re
import sqlite3
from collections import defaultdict

PRICE_DB = "data/price_v2.db"
DIR_DB = "data/directions_v2.db"
OSINT_DB = "data/osint.db"

# --- ключи моделей: (врач, диагностика, лечение/процедуры) — по названию
# позиции ИЛИ разделу прайса; site — направления навигации, дающие признак 1
M = {
 "М1 — превентивная дерматоонкология": dict(
   site={"Онкология (онкодерматология)", "Удаление новообразований (дерматохирургия)"},
   doctor=r"при[её]м.*(онколог|онкодерматолог)|врач[а-я\- ]*онколог",
   diag=r"дерматоскоп|картирован|fotofinder|цифров\w+ (карта|мониторинг) (кожи|невус)|биопси.{0,20}кож|гистолог",
   treat=r"удален\w+ (новообразован|невус|родин|папиллом|кератом|базалиом)|иссечен\w+ (новообразован|опухол)",
   any_=r"дерматоскоп|невус|меланом|базалиом|онкодерматолог|новообразован"),
 "М2 — трихология и здоровье волос": dict(
   site={"Трихология"},
   doctor=r"при[её]м.*трихолог|врач[а-я\- ]*трихолог|консультац\w+ трихолог",
   diag=r"трихоскоп|фототрихограмм|трихограмм|(анализ|диагностик)\w*.{0,25}волос|минералограмм",
   treat=r"(мезотерап|плазмотерап|плазмолифтинг|prp|дарсонвал|озонотерап|пилинг).{0,30}(головы|волосист)|лечен\w+ (выпаден|волос|алопец)",
   any_=r"трихолог|волосист|алопец|выпаден\w+ волос"),
 "М3 — Clinical Skin Health": dict(
   site={"Косметология"},
   doctor=r"при[её]м.*(косметолог|дерматокосметолог)|врач[а-я\- ]*косметолог",
   diag=r"диагностик\w+ кожи|дерматоскоп|себуметр|корнеометр|visia|скан\w+ кожи",
   treat=r"лечен\w+ (акне|угр|постакне|розаце|купероз)|(удален|коррекц|шлифовк)\w+.{0,15}(рубц|шрам|постакне)|пигментац|фотоомоложен|лазерн\w+ (шлифовк|лечен)|bbl|ipl|чистк\w+ лица|пилинг",
   any_=r"акне|постакне|рубц|пигментац|купероз|розаце|косметолог"),
 "М4 — дерматовенерология и интимное здоровье": dict(
   site={"Дерматовенерология", "Урология", "Гинекология"},
   doctor=r"при[её]м.*(дерматовенеролог|венеролог)|врач[а-я\- ]*(дерматовенеролог|венеролог)",
   diag=r"пцр.{0,25}(иппп|инфекц|хламид|микоплазм|уреаплазм|впч)|мазок|соскоб.{0,20}(урогенитал|уретр|цервикал)|фемофлор|андрофлор|сифилис|rpr|рпга",
   treat=r"удален\w+ кондилом|кондилом|лечен\w+ (иппп|впч|герпес|хламидиоз|уреаплазмоз|микоплазмоз|трихомониаз|гонор)",
   any_=r"иппп|зппп|впч|кондилом|венеролог|интимн"),
 "М5 — подология и здоровье стоп": dict(
   site={"Подология"},
   doctor=r"при[её]м.*подолог|врач[а-я\- ]*подолог|консультац\w+ подолог",
   diag=r"(диагностик|микроскоп|посев|соскоб)\w*.{0,25}(ногт|стоп|онихомикоз|гриб)",
   treat=r"вросш\w+ ногт|коррекц\w+ ногтев|скоб\w+ (комбипед|3то|ziplock)|титанов\w+ нить|обработк\w+ (стоп|ногт)|онихомикоз|подологическ",
   any_=r"подолог|вросш|онихомикоз|ногтев"),
 "М6 — аллергодерматология и хронические дерматозы": dict(
   site={"Аллергология"},
   doctor=r"при[её]м.*(аллерголог|иммунолог)|врач[а-я\- ]*аллерголог",
   diag=r"аллергопроб|прик[- ]тест|скарификацион|аллергопанел|иммуноглобулин\w* e|ige|аллерген",
   treat=r"лечен\w+ (атопическ|экзем|крапивниц|псориаз|дерматит)|фототерап|пува|асит|аллерген[- ]специфическ",
   any_=r"атопическ|экзем|крапивниц|псориаз|нейродермит|себорейн\\w+ дерматит|хроническ\\w+ дерматоз"),
}
M0 = "М0 — As-is+: стандартизированная экспертная дерматология"
DERM_CORE_DIRS = {"Дерматовенерология", "Трихология", "Подология",
                  "Удаление новообразований (дерматохирургия)",
                  "Онкология (онкодерматология)"}
SERVICE = {"Лаборатория", "УЗИ/лучевая", "Функциональная диагностика",
           "Справки/профосмотры", "Вакцинация", "Анестезиология",
           "Процедурный кабинет", "Приём без указания специальности",
           "Приём смежного специалиста", "Спа", "Выездные услуги"}


def load_positions():
    p = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    p.execute("ATTACH ? AS d", (f"file:{DIR_DB}?mode=ro",))
    rows = defaultdict(list)   # inn -> [(name, section, url, direction)]
    for inn, name, sec, url, d in p.execute("""
        SELECT i.inn, i.name, coalesce(i.section,''), i.url,
               coalesce(x.direction,'')
        FROM items_v2 i LEFT JOIN d.directions_v2 x
          ON x.domain=i.domain AND x.name=i.name
         AND x.section IS coalesce(i.section, x.section)"""):
        rows[inn].append((name, sec, url, d))
    return rows


def classify():
    dd = sqlite3.connect(f"file:{DIR_DB}?mode=ro", uri=True)
    site = defaultdict(set)
    for inn, d in dd.execute("select distinct inn, direction from site_dirs"):
        site[inn].add(d)
    comp_dirs = defaultdict(set)
    for inn, d in dd.execute("""select inn, direction from clinic_directions
        where status in ('подтверждено (сайт+прайс)', 'по прайсу')"""):
        comp_dirs[inn].add(d)
    rows = load_positions()

    out = {}
    for inn, items in rows.items():
        clin_total = sum(1 for n, s, u, d in items
                         if d and d not in SERVICE and not d.startswith("Брак")) or 1
        derm_pos = sum(1 for n, s, u, d in items if d in DERM_CORE_DIRS)
        core = ("есть" if (comp_dirs[inn] & DERM_CORE_DIRS and derm_pos >= 3)
                else "неясно" if derm_pos >= 1 else "нет")
        models = {}
        for mname, k in M.items():
            txt = [(n + " || " + s, n, u) for n, s, u, d in items]
            hits = [(n, u) for t, n, u in txt if re.search(k["any_"], t, re.I)]
            n_hits = len(hits)
            if n_hits == 0 and not (site[inn] & k["site"]):
                continue
            f_site = bool(site[inn] & k["site"])
            f_doc = any(re.search(k["doctor"], t, re.I) for t, n, u in txt)
            f_diag = any(re.search(k["diag"], t, re.I) for t, n, u in txt)
            f_treat = any(re.search(k["treat"], t, re.I) for t, n, u in txt)
            share = n_hits / clin_total
            f_multi = n_hits >= 5 and f_diag and f_treat
            f_pos = share >= 0.05 and n_hits >= 3
            feats = [f_site, f_doc, f_diag, f_multi, f_pos]
            nf = sum(feats)
            if nf >= 4 and share >= 0.20 and n_hits >= 5:
                lvl = 3
            elif nf >= 3 and n_hits >= 3:
                lvl = 2
            elif nf == 2:
                lvl = 1
            elif n_hits >= 1:
                lvl = 0
            else:
                continue
            ev = []
            for pat in (k["doctor"], k["diag"], k["treat"], k["any_"]):
                for t, n, u in txt:
                    if re.search(pat, t, re.I) and (n, u) not in ev:
                        ev.append((n, u))
                        break
            models[mname] = dict(lvl=lvl, nf=nf, share=share, hits=n_hits,
                                 feats=feats, ev=ev[:3])
        # ведущая модель: М-типология осмысленна только при дерм-ядре
        # (документ: «текущая дерматологическая платформа + вертикаль»)
        strong = {m: v for m, v in models.items() if v["lvl"] >= 2}
        if r_core_no := (core == "нет"):
            lead = "— (нет дерм-ядра)"
        elif strong:
            lead = max(strong, key=lambda m: (strong[m]["lvl"], strong[m]["share"]))
        elif core == "есть":
            lead = M0
        elif models:
            lead = max(models, key=lambda m: (models[m]["lvl"], models[m]["share"]))
        else:
            lead = "—"
        mods = [m for m, v in sorted(models.items(),
                                     key=lambda kv: (-kv[1]["lvl"], -kv[1]["share"]))
                if m != lead and v["lvl"] >= 1]
        out[inn] = dict(core=core, lead=lead, models=models, mods=mods,
                        clin_total=clin_total, derm_pos=derm_pos)
    return out

def export(only_inns=None):
    import openpyxl, datetime, re as _re
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    o = sqlite3.connect(f"file:{OSINT_DB}?mode=ro", uri=True)
    comp = {i: (n, c) for i, n, c in o.execute("select inn, name, city from t40_companies")}
    doms = {}
    p = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    for inn, d in p.execute("select distinct inn, domain from items_v2"):
        doms.setdefault(inn, []).append(d)
    res = classify()
    if only_inns is not None:
        res = {i: r for i, r in res.items() if i in only_inns}
    H = Font(bold=True); FILL = PatternFill("solid", fgColor="DDEBF7")
    CLEAN = _re.compile(r"[\x00-\x1f]")
    wb = openpyxl.Workbook()
    ws = wb.active; ws.title = "Классификация_М0-М6"
    mnames = list(M.keys())
    hdr = (["ИНН", "Компания", "Город", "Домен(ы)", "Дерматологическое ядро",
            "Основная модель", "Дополнительные модули",
            "Уровень зрелости основной модели"]
           + [f"{m.split(' — ')[0]}: уровень" for m in mnames]
           + ["Признаки основной модели (из 5)", "Доказательства (дословно | URL)"])
    ws.append(hdr)
    for c in ws[1]: c.font = H; c.fill = FILL
    FEAT = ["раздел на сайте", "профильный врач", "диагностика",
            "несколько услуг/этапов", "доля ≥5%"]
    for inn, r in sorted(res.items(), key=lambda kv: (kv[1]["lead"] == "—", kv[1]["lead"])):
        nm, city = comp.get(inn, ("", ""))
        lead = r["lead"]
        lv = r["models"].get(lead, {})
        feats = lv.get("feats")
        ftxt = ("; ".join(f for f, on in zip(FEAT, feats) if on)
                if feats else ("дерм-ядро, база" if lead == M0 else ""))
        ev = "\n".join(f"«{CLEAN.sub('', n)[:120]}» | {u[:120]}"
                       for n, u in lv.get("ev", []))
        ws.append([inn, nm, city, ", ".join(doms.get(inn, [])), r["core"],
                   lead, "; ".join(m.split(" — ")[0] + f" ({r['models'][m]['lvl']})"
                                   for m in r["mods"]) or "—",
                   lv.get("lvl", 0 if lead == M0 else None)]
                  + [r["models"].get(m, {}).get("lvl", "") for m in mnames]
                  + [ftxt, ev])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(hdr))}{ws.max_row}"
    for i, w in enumerate([12, 32, 14, 22, 14, 44, 40, 12] + [10]*len(mnames) + [45, 90], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    w2 = wb.create_sheet("Сводка_моделей")
    w2.append(["Модель", "Ведущая у клиник", "Уровень 3 (второе ядро)",
               "Уровень 2", "Уровень 1", "Уровень 0"])
    for c in w2[1]: c.font = H; c.fill = FILL
    from collections import Counter
    leads = Counter(r["lead"] for r in res.values())
    for m in [M0] + mnames:
        lvls = Counter(r["models"].get(m, {}).get("lvl") for r in res.values()
                       if m in r["models"])
        w2.append([m, leads.get(m, 0), lvls.get(3, 0), lvls.get(2, 0),
                   lvls.get(1, 0), lvls.get(0, 0)])
    w2.append(["— (без дерм-ядра и без моделей)", leads.get("—", 0), "", "", "", ""])
    for i, w in enumerate([55, 16, 20, 10, 10, 10], 1):
        w2.column_dimensions[get_column_letter(i)].width = w

    w3 = wb.create_sheet("Методика")
    for line in [
        "Классификация по документу заказчика «Модели эволюции бизнес-модели "
        "'Чистой кожи'» (2026-09-24).",
        "Признаки модели (правило существенности — минимум 3 из 5):",
        "  1. Самостоятельный раздел на сайте — направление модели в навигации сайта (site_dirs).",
        "  2. Профильный врач — приём профильного специалиста в прайсе.",
        "  3. Диагностика — диагностические услуги модели в прайсе.",
        "  4. Несколько услуг/этапов маршрута — ≥5 позиций модели и наличие и диагностики, и лечения.",
        "  5. Позиционирование — доля позиций модели ≥5% клинического прайса.",
        "Уровни зрелости: 0 — единичная услуга (позиции есть, признаков <2); "
        "1 — дополнительное направление (2 признака); 2 — усиленный профиль (≥3); "
        "3 — второе ядро (≥4 признаков и доля ≥20%).",
        "Ведущая модель — максимальная по (уровень, доля); М0 — когда есть "
        "дерм-ядро и ни одна из М1–М6 не достигла уровня 2.",
        "Дерм-ядро: подтверждённые направления дерм-контура + ≥3 позиции.",
        "Источники: прайсы (items_v2 + directions_v2), навигация сайтов "
        "(site_dirs), состав направлений (clinic_directions).",
    ]:
        w3.append([line])
    w3.column_dimensions["A"].width = 120

    path = f"output/ЧК_модели_М0-М6_{datetime.date.today()}.xlsx"
    wb.save(path)
    print(path, "| клиник:", len(res))
    print("ведущие:", dict(Counter(r['lead'] for r in res.values()).most_common()))


if __name__ == "__main__":
    import sys as _s
    if len(_s.argv) > 1:
        inns = {l.strip() for l in open(_s.argv[1]) if l.strip()}
        export(inns)
    else:
        export()
