"""Адаптер аналитики на базу v2 (items_v2 + directions_v2 + clinic_directions).

Даёт старым скриптам (oligo_analysis / oligo_finance / oligo_models /
retention) единый вход:
  load_items(): [(id, inn, domain, section, name, price, direction, flags)]
  companies():  {inn: (name, city, region)}
  clinic_composition(): {inn: set(direction)} — состав направлений клиники
    по правилу трёх источников (src/clinic_profile.py): статусы
    «подтверждено (сайт+прайс)» и «по прайсу»; вето лицензии уже применено.
Направление позиции, не вошедшее в состав клиники, в комбинацию/сегмент
не идёт (позиция остаётся в «прочем» прайсе клиники).
"""

import sqlite3

PRICE_DB = "data/price_v2.db"
DIR_DB = "data/directions_v2.db"
OSINT_DB = "data/osint.db"

UNMAPPED = "— НЕ КЛАССИФИЦИРОВАНО —"


def load_items():
    p = sqlite3.connect(f"file:{PRICE_DB}?mode=ro", uri=True)
    p.execute("ATTACH ? AS d", (f"file:{DIR_DB}?mode=ro",))
    rows = p.execute("""
        SELECT i.rowid, i.inn, i.domain, coalesce(i.section,''), i.name,
               i.price, coalesce(nullif(x.direction,''), ?),
               coalesce(x.flags,'')
        FROM items_v2 i
        LEFT JOIN d.directions_v2 x
          ON x.domain = i.domain AND x.name = i.name
         AND x.section IS coalesce(i.section, x.section)
    """, (UNMAPPED,)).fetchall()
    p.close()
    return rows


def companies():
    o = sqlite3.connect(f"file:{OSINT_DB}?mode=ro", uri=True)
    out = {}
    for t in ("t40_companies", "companies"):
        try:
            for inn, nm, city, *rest in o.execute(
                    f"select inn, name, city, region from {t}"):
                out.setdefault(inn, (nm, city, rest[0] if rest else ""))
        except sqlite3.OperationalError:
            try:
                for inn, nm, city in o.execute(
                        f"select inn, name, city from {t}"):
                    out.setdefault(inn, (nm, city, ""))
            except sqlite3.OperationalError:
                continue
    return out


def clinic_composition():
    dd = sqlite3.connect(f"file:{DIR_DB}?mode=ro", uri=True)
    comp = {}
    for inn, d in dd.execute(
            """select inn, direction from clinic_directions
               where status in ('подтверждено (сайт+прайс)', 'по прайсу')"""):
        comp.setdefault(inn, set()).add(d)
    return comp


def clinic_dir_statuses():
    dd = sqlite3.connect(f"file:{DIR_DB}?mode=ro", uri=True)
    out = {}
    for inn, d, st in dd.execute(
            "select inn, direction, status from clinic_directions"):
        out.setdefault(inn, {})[d] = st
    return out
