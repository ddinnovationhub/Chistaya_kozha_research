"""Лист «Олигопрофили: финансы и структура прайса» (заказчик, 2026-09-23).

Единица анализа — юрлицо (ИНН) из числа 242 олигопрофильных клиник, то есть
ровно те компании, что учтены в блоке «ПОЛНЫЕ КОМБИНАЦИИ» листа
7_Сочетания_направлений.

Источники (каждое значение — из источника, ничего не достраивается):
  · комбинация направлений и позиции прайса — data/prices.db + data/directions.db
    (ключ направления: norm(strip_ui(name_raw)), как в src/direction_map);
    позиции с флагом «Брак парсинга (цена)» из price_flags в медиану не идут;
  · число точек — data/osint.db, rzn_licenses: уникальные адреса приложений
    действующих (не аннулированных, не прекращённых) медицинских лицензий;
  · выручка — выгрузки заказчика (СПАРК) в data/*.xlsx и, независимо,
    ГИР БО (bo.nalog.gov.ru, строка 2110);
  · активы (1600), чистая прибыль (2400), прибыль до налогообложения (2300),
    проценты к уплате (2330), дата внесения в ЕГРЮЛ — ГИР БО (src/girbo).

Производные показатели считаются по явным формулам, и метод пишется в строку:
  · Рентабельность EBIT = (2300 + 2330) / 2110. Строк 2300/2330 нет в
    упрощённой форме МСП — тогда берётся EBITM из выгрузки заказчика, а если
    и его нет, ставится «Не найдено», НЕ оценка.
  · ROA = 2400 / среднегодовые активы ((1600 на начало + 1600 на конец) / 2);
    если активы на начало года не опубликованы — по активам на конец,
    и это отражено в колонке метода.
  · Выручка на точку = выручка / число точек по лицензиям.
"""

import re
import statistics
import sqlite3
import zlib
import json

import openpyxl
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from src.direction_map import norm, strip_ui

XLSX = "output/ЧК_олигопрофильные_анализ_2026-09-22.xlsx"
SHEET = "8_Олигопрофили_финансы"
YEAR = "2025"


def inn_norm(x):
    s = re.sub(r"\D", "", str(x)) if x else ""
    return s.zfill(10) if len(s) == 9 else (s if len(s) in (10, 12) else None)


# ------------------------------------------------------------ источники

def load_segments(wb):
    """ИНН → (компания, город, комбинация, набор направлений) для олигопрофилей."""
    out = {}
    for r in wb["1_Сегменты_клиник"].iter_rows(min_row=2, values_only=True):
        if r[7] and "олиго" in str(r[7]):
            dirs = sorted(x.strip() for x in str(r[10]).split(";") if x.strip())
            out[inn_norm(r[0])] = (r[1], r[2], " + ".join(dirs), dirs)
    return out


def load_prices(inns):
    """ИНН → (всего позиций, медиана цены, Counter направлений по позициям)."""
    dmap = {}
    dd = sqlite3.connect("file:data/directions.db?mode=ro", uri=True)
    for k, d in dd.execute("select name_norm, direction from name_directions"):
        dmap[k] = d
    p = sqlite3.connect("file:data/prices.db?mode=ro", uri=True)
    bad = {r[0] for r in p.execute("select price_item_id from price_flags")}
    res = {}
    q = "select id, inn, name_raw, price_value from price_items"
    for pid, inn, name, price in p.execute(q):
        i = inn_norm(inn)
        if i not in inns:
            continue
        a = res.setdefault(i, [0, [], {}])
        a[0] += 1
        d = dmap.get(norm(strip_ui(name or "")))
        if d:
            a[2][d] = a[2].get(d, 0) + 1
        if price and pid not in bad:
            a[1].append(price)
    return res


def load_points():
    """ИНН → число уникальных адресов действующих мед-лицензий."""
    db = sqlite3.connect("file:data/osint.db?mode=ro", uri=True)
    out = {}
    for inn, objs, raw in db.execute(
            "select inn, objects_n, raw_gz from rzn_licenses "
            "where is_med=1 and annulled is null and terminated is null"):
        i = inn_norm(inn)
        if not i:
            continue
        addrs = out.setdefault(i, set())
        if raw:
            try:
                d = json.loads(zlib.decompress(raw).decode())
                for o in d.get("objects") or []:
                    a = (o.get("address") or o.get("addr") or "").strip()
                    if a:
                        addrs.add(re.sub(r"\s+", " ", a.lower()))
            except Exception:                            # noqa: BLE001
                pass
    return {k: len(v) for k, v in out.items() if v}


def load_spark():
    """ИНН → (выручка 2025 ₽, EBITM 2025 %) из выгрузок заказчика."""
    rev, ebit = {}, {}

    def sheet_rows(path, sheet, key):
        w = openpyxl.load_workbook(path, read_only=True)
        rs = list(w[sheet].iter_rows(values_only=True))
        hi = next(i for i, r in enumerate(rs)
                  if r and key in [str(x) for x in r if x])
        return [str(x) if x else "" for x in rs[hi]], rs[hi + 1:]

    h, rows = sheet_rows("data/Ванина выгрузка V2.xlsx", "Компании мед отрасли",
                         "ИНН Код налогоплательщика")
    ii = h.index("ИНН Код налогоплательщика")
    ir = h.index(f"{YEAR}, Выручка, RUB")
    ie = h.index(f"{YEAR}, Рентабельность прибыли до налогообложения "
                 f"и процентов - (EBITM), %")
    for r in rows:
        i = inn_norm(r[ii])
        if not i:
            continue
        if r[ir] not in (None, ""):
            rev.setdefault(i, r[ir])
        if r[ie] not in (None, ""):
            ebit.setdefault(i, r[ie])

    for path, sheet in [("data/Выборка_компаний_V2.xlsx", "Sheet1"),
                        ("data/Выборка_компаний_V1.xlsx", "report"),
                        ("data/Выборка_для_проверки.xlsx", "Sheet1")]:
        h, rows = sheet_rows(path, sheet, "Код налогоплательщика")
        ii = h.index("Код налогоплательщика")
        ir = h.index(f"{YEAR}, Выручка, RUB")
        for r in rows:
            i = inn_norm(r[ii])
            if i and r[ir] not in (None, ""):
                rev.setdefault(i, r[ir])
    return rev, ebit


def load_girbo():
    """ИНН → (карточка, отчётность за YEAR). Суммы форм — тыс. ₽."""
    db = sqlite3.connect("file:data/osint.db?mode=ro", uri=True)
    org = {}
    try:
        for r in db.execute("select inn, short_name, status_code, status_date "
                            "from girbo_org"):
            org[inn_norm(r[0])] = r[1:]
    except sqlite3.OperationalError:
        return {}, {}
    fin = {}
    for r in db.execute(
            "select inn, simplified, line1600, line1600_prev, line2110, "
            "line2120, line2200, line2300, line2330, line2400 from girbo_fin "
            "where period=?", (YEAR,)):
        fin[inn_norm(r[0])] = dict(zip(
            ("simplified", "a1600", "a1600p", "r2110", "c2120", "p2200",
             "p2300", "i2330", "p2400"), r[1:]))
    return org, fin


# ------------------------------------------------------------ сборка

def build():
    wb = openpyxl.load_workbook(XLSX)
    seg = load_segments(wb)
    inns = set(seg)
    prices = load_prices(inns)
    points = load_points()
    spark_rev, spark_ebit = load_spark()
    org, fin = load_girbo()
    K = 1000.0                                   # формы БО — в тыс. ₽

    if SHEET in wb.sheetnames:
        del wb[SHEET]
    ws = wb.create_sheet(SHEET)
    H = Font(bold=True)
    FILL = PatternFill("solid", fgColor="DDEBF7")
    ws.append([f"Все {len(seg)} компаний из блока «ПОЛНЫЕ КОМБИНАЦИИ» листа "
               f"7_Сочетания_направлений. Финансы за {YEAR} год."])
    ws.append(["Доля профиля = позиций этого направления / всех позиций прайса компании; доли комбинации и "
               "остального прайса в сумме дают 100%. "
               "Медиана прайса — по позициям с ценой, без помеченных «Брак парсинга (цена)»."])
    ws.append(["EBIT = стр. 2300 + стр. 2330 формы 0710002; ROA = стр. 2400 / среднегодовые активы (стр. 1600). "
               "Где строк нет (упрощённая отчётность МСП) — «Не найдено», без оценок: реконструкция EBIT из "
               "упрощённой формы проверена на 256 полных формах и расходится с фактом более чем на 1% в трети "
               "случаев, поэтому не применяется. Сопоставимый по покрытию показатель — рентабельность продаж."])
    ws.append([])
    cols = ["ИНН", "Компания", "Город", "Комбинация", "Направлений",
            "Комбинация с долей в прайсе", "Доля комбинации в прайсе",
            "Остальной прайс (все направления с долями)",
            "Доля лаборатории в прайсе", "Признак лабораторного профиля",
            "Позиций в прайсе", "Медиана прайса ₽",
            "Выручка 2025 ₽ (выгрузка заказчика)", "Выручка 2025 ₽ (ГИР БО)",
            "Чистая прибыль 2025 ₽", "Активы на конец 2025 ₽",
            "Прибыль от продаж 2025 ₽", "Рентабельность продаж 2025 %",
            "Метод рентабельности продаж",
            "Рентабельность EBIT 2025 %", "Метод EBIT",
            "ROA 2025 %", "Метод ROA",
            "Точек по мед. лицензиям", "Выручка на точку ₽",
            "Дата внесения в ЕГРЮЛ", "Возраст юрлица, лет", "Статус юрлица"]
    ws.append(cols)
    for c in ws[5]:
        c.font = H
        c.fill = FILL

    import datetime
    today = datetime.date.today()
    for inn, (comp, city, combo, dirs) in sorted(seg.items(), key=lambda kv: kv[1][0] or ""):
        tot, prs, cnt = prices.get(inn, (0, [], {}))
        shares = [(d, cnt.get(d, 0) / tot) for d in dirs] if tot else []
        shares.sort(key=lambda x: -x[1])
        combo_sh = "; ".join(f"{d} {s:.0%}" for d, s in shares)
        share_sum = sum(s for _, s in shares) if shares else None
        median = round(statistics.median(prs)) if prs else None
        # остальной прайс: всё, что не вошло в комбинацию, включая сервисные
        # направления и неклассифицированное — сумма всех долей даёт 100%
        rest = {d: n for d, n in cnt.items() if d not in dirs}
        unmapped = tot - sum(cnt.values()) if tot else 0
        if unmapped > 0:
            rest["— не классифицировано —"] = rest.get("— не классифицировано —", 0) + unmapped
        rest_s = "; ".join(f"{d} {n / tot:.0%}" for d, n in
                           sorted(rest.items(), key=lambda kv: -kv[1])) if tot else None
        lab = (cnt.get("Лаборатория", 0) / tot) if tot else None
        lab_flag = ("да" if lab is not None and lab > 0.5 else "нет")

        rev_s = spark_rev.get(inn)
        f = fin.get(inn) or {}
        rev_g = f.get("r2110") * K if f.get("r2110") is not None else None
        prof = f.get("p2400") * K if f.get("p2400") is not None else None
        act = f.get("a1600") * K if f.get("a1600") is not None else None
        actp = f.get("a1600p") * K if f.get("a1600p") is not None else None

        # прибыль от продаж: стр. 2200 полной формы; в упрощённой форме
        # строки 2200 нет, и она по определению равна 2110 - 2120
        if f.get("p2200") is not None:
            sales = f["p2200"] * K
            sales_src = "ГИР БО: стр. 2200"
        elif f.get("r2110") is not None and f.get("c2120") is not None:
            sales = (f["r2110"] - f["c2120"]) * K
            sales_src = "ГИР БО, упрощённая форма: 2110 − 2120"
        else:
            sales, sales_src = None, "Не найдено: отчётности нет"
        sales_m = (round(sales / (f["r2110"] * K) * 100, 1)
                   if sales is not None and f.get("r2110") else None)

        if f.get("p2300") is not None and f.get("r2110"):
            ebit = f["p2300"] + (f.get("i2330") or 0)
            ebit_m = round(ebit / f["r2110"] * 100, 1)
            ebit_src = "ГИР БО: (2300+2330)/2110"
        elif spark_ebit.get(inn) is not None:
            ebit_m = round(float(spark_ebit[inn]), 1)
            ebit_src = "выгрузка заказчика: EBITM"
        else:
            ebit_m, ebit_src = None, ("Не найдено: упрощённая отчётность"
                                      if f else "Не найдено: отчётности нет")
        if prof is not None and act:
            base = (act + actp) / 2 if actp else act
            roa = round(prof / base * 100, 1)
            roa_src = ("2400 / среднегодовые активы" if actp
                       else "2400 / активы на конец года")
        else:
            roa, roa_src = None, "Не найдено: нет активов или прибыли в ГИР БО"

        pts = points.get(inn)
        rev = rev_s if rev_s not in (None, "") else rev_g
        per_point = round(rev / pts) if (rev and pts) else None

        o = org.get(inn) or (None, None, None)
        reg = o[2]
        age = None
        if reg:
            try:
                d0 = datetime.date.fromisoformat(str(reg)[:10])
                age = round((today - d0).days / 365.25, 1)
            except ValueError:
                age = None

        ws.append([inn, comp, city, combo, len(dirs), combo_sh,
                   round(share_sum, 3) if share_sum is not None else None,
                   rest_s, round(lab, 3) if lab is not None else None, lab_flag,
                   tot or None, median,
                   rev_s if rev_s not in (None, "") else None, rev_g,
                   prof, act, sales, sales_m, sales_src,
                   ebit_m, ebit_src, roa, roa_src,
                   pts, per_point, reg, age, o[1]])

    for i, w in enumerate([13, 38, 17, 60, 12, 70, 16, 90, 16, 24, 14, 15, 22, 20, 20, 20,
                           20, 18, 34, 16, 30, 12, 34, 14, 18, 18, 14, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A6"
    ws.auto_filter.ref = f"A5:{get_column_letter(len(cols))}{ws.max_row}"
    wb._sheets.sort(key=lambda s: s.title)
    wb.save(XLSX)
    return ws.max_row - 5


if __name__ == "__main__":
    print("строк:", build())
