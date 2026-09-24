"""Состав направлений клиники — правило трёх источников (такт 1 команды,
утверждается заказчиком, 2026-09-24).

Роли и критерии:
  Методолог: состав = прайс (подтверждение деньгами) × сайт (заявление
    клиники) × лицензия (только вето — указание заказчика).
  Врач-методолог: направление «работает», если за ним есть позиции прайса;
    сайт без прайса — заявление, не факт.
  Инженер качества: направление вне лицензии юрлица не включается — это
    ошибка маппинга или юридически чужая деятельность (указание заказчика).
  Статистик: пропорции считаются по КЛИНИЧЕСКОМУ прайсу (сервисные и брак
    вынесены), состав — по правилу ниже.

Правило включения направления D в состав клиники:
  СТАТУС «подтверждено»: D заявлен на сайте (site_dirs) И ≥1 позиция прайса.
  СТАТУС «по прайсу»:    D не найден на сайте, но ≥3 позиций И ≥1% клин. прайса.
  СТАТУС «только сайт»:  D на сайте, позиций 0 — в состав НЕ входит, виден.
  ВЕТО ЛИЦЕНЗИИ: клиническое проверяемое D без покрытия лицензией → в состав
    НЕ входит, статус «вне лицензии» (кроме NO_LIC_CHECK и клиник без
    расшифровки лицензии).

Выход: таблица clinic_directions в data/directions_v2.db:
  inn · direction · status · positions · share_clinical · sources
"""

import sqlite3
import sys
from collections import Counter, defaultdict

from src.map_v2 import NO_LIC_CHECK, LIC_KEYS, lic_text, lic_covers

DIR_DB = "data/directions_v2.db"
OSINT_DB = "data/osint.db"

# сервисные направления (закрытый список заказчика) и брак — не «клинические»
SERVICE = {
    "Лаборатория", "УЗИ/лучевая", "Функциональная диагностика",
    "Справки/профосмотры", "Вакцинация", "Анестезиология",
    "Процедурный кабинет", "Приём без указания специальности",
    "Приём смежного специалиста", "Спа", "Выездные услуги",
    "Брак разметки (UI-текст)", "Брак разметки (обрывок)", "Брак парсинга",
}


def build():
    dd = sqlite3.connect(DIR_DB, timeout=60)
    o = sqlite3.connect(f"file:{OSINT_DB}?mode=ro", uri=True)
    dd.execute("""CREATE TABLE IF NOT EXISTS clinic_directions(
        inn TEXT, direction TEXT, status TEXT, positions INTEGER,
        share_clinical REAL, sources TEXT)""")
    dd.execute("delete from clinic_directions")

    price = defaultdict(Counter)      # inn -> direction -> позиций
    for inn, d, k in dd.execute(
            """select inn, direction, count(*) from directions_v2
               where direction != '' group by inn, direction"""):
        price[inn][d] = k
    site = defaultdict(set)
    for inn, d in dd.execute("select distinct inn, direction from site_dirs"):
        site[inn].add(d)

    out = []
    inns = set(price) | set(site)
    lic_cache = {}
    for inn in inns:
        cnt = price.get(inn, Counter())
        clinical_total = sum(k for d, k in cnt.items() if d not in SERVICE) or 1
        if inn not in lic_cache:
            lic_cache[inn] = lic_text(inn, o)
        lic = lic_cache[inn]
        dirs = set(d for d in cnt if not d.startswith("Брак")) | site.get(inn, set())
        for d in sorted(dirs):
            k = cnt.get(d, 0)
            share = (k / clinical_total) if d not in SERVICE else None
            on_site = d in site.get(inn, set())
            srcs = "+".join(s for s, y in (("прайс", k > 0), ("сайт", on_site)) if y)
            # вето лицензии — только для клинических проверяемых направлений
            if (d not in NO_LIC_CHECK and d in LIC_KEYS and lic is not None
                    and not lic_covers(d, lic)):
                status = "вне лицензии (исключено)"
            elif on_site and k >= 1:
                status = "подтверждено (сайт+прайс)"
            elif not on_site and (k >= 3 or (k >= 2 and share is not None
                                             and share >= 0.05)):
                # порог заказчика (2026-09-24): ≥3 позиций ИЛИ ≥2 позиций
                # при ≥5% клинического прайса
                status = "по прайсу"
            elif on_site and k == 0:
                status = "только сайт (не подтверждено прайсом)"
            else:
                status = "след в прайсе (<порога)"
            out.append((inn, d, status, k,
                        round(share, 4) if share is not None else None, srcs))
    dd.executemany("insert into clinic_directions values (?,?,?,?,?,?)", out)
    dd.commit()
    st = Counter(r[2] for r in out)
    print("клиник:", len(inns), "| строк:", len(out))
    for s, k in st.most_common():
        print(f"  {k:6} {s}")


if __name__ == "__main__":
    build()
