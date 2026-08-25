"""Пересчитываемый шаг СУЖДЕНИЙ (заказчик, 2026-08-25, часть 2: сбор и
суждения разделены).

Этап 6 собирает только сырое: позиция (название/описание/цена/URL),
«Тип строки», телеметрия доступа. Всё интерпретируемое считает ЭТОТ шаг —
по базе, без повторного обхода сайтов:

- строкам услуг: наш тег, код 804н, основание и ступень маппинга,
  уверенность (ступень 1 — код; несопоставленное — «на разметке»);
- свёртка однотипной эстетики в строки-агрегаты (группирует, не теряет:
  перечень свёрнутых позиций в описании обязателен; члены агрегата
  помечаются collapsed_into и в 02_Услуги не выводятся);
- клиникам: тип (Тип 1/2/3), статус типа, правило, грейд, эстетические
  маркеры, флаги (единственное несмежное; удаление вне дерм-контура).

Правка справочника → повторный запуск этого шага за секунды.
Колонка «Профиль» НЕ заполняется до эталона заказчика (часть 3).
«Есть у ЧК» не считается вовсе — последний шаг, после разметки.

Запуск: python -m src.judgments
Шаг идемпотентен: прежние суждения и агрегаты сбрасываются и строятся заново.
"""

import pathlib
import re
import sqlite3

import yaml

from src.classify import classify, load_contours
from src.extract_site import (BRAND_INJECTABLE_RE, _esthetic_keywords,
                              is_esthetic_line)
from src.mapper import build_formulation_index, map_tier1, normalize_service_name

TYPE_STATUS_PRELIM = "предварительный (ступень 1, до разметки)"

_DERM = ("derm", "oncoderm", "trich", "dermsurg")


def _family_key(nm: str) -> str:
    """Семейство свёртки: позиции, различающиеся только препаратом/зоной/
    объёмом/длительностью, дают один ключ (первые 4 слова без латиницы/цифр)."""
    base = re.sub(r"[A-Za-z0-9+]+", " ", normalize_service_name(nm))
    return " ".join(base.split()[:4])


def _agg_price(rows) -> str | None:
    prices = []
    for r in rows:
        digits = re.sub(r"\D", "", r["price"] or "")
        if digits:
            prices.append(int(digits))
    return f"от {min(prices)} до {max(prices)} ₽" if prices else None


def _insert_agg(db, clinic_id, title, rows, family_name, tag, basis,
                descr_prefix: str = "") -> int:
    """Строка-агрегат + пометка членов collapsed_into. Возвращает размер."""
    names = []
    for r in rows:
        if r["name_raw"] not in names:
            names.append(r["name_raw"])
    cur = db.execute(
        "INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
        "description_raw, page_url, price, row_type, tag, code_804n, "
        "mapping_basis, mapping_tier, confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (clinic_id, title,
         f"{family_name} — {len(rows)} позиций (агрегат)",
         # перечень свёрнутых позиций в описании ОБЯЗАТЕЛЕН (заказчик 2026-08-25)
         descr_prefix + "; ".join(names),
         rows[0]["page_url"], _agg_price(rows), "агрегат", tag, None,
         basis, "код", "высокая"))
    agg_id = cur.lastrowid
    db.executemany("UPDATE services_found SET collapsed_into=? WHERE id=?",
                   [(agg_id, r["id"]) for r in rows])
    return len(rows)


def recompute(db: sqlite3.Connection, verbose: bool = False) -> dict:
    db.row_factory = sqlite3.Row
    contours = load_contours()
    form_index = build_formulation_index()
    esth_kws = _esthetic_keywords()
    cfg = yaml.safe_load(pathlib.Path("config/thresholds.yaml")
                         .read_text(encoding="utf-8"))
    fuzzy_cutoff = float(cfg.get("mapping", {}).get("fuzzy_threshold", 0)) or None

    # ── Сброс прежних суждений (идемпотентность) ──
    db.execute("DELETE FROM services_found WHERE row_type='агрегат'")
    db.execute("UPDATE services_found SET collapsed_into=NULL, tag=NULL, "
               "code_804n=NULL, mapping_basis=NULL, mapping_tier=NULL, "
               "confidence=NULL WHERE mapping_tier IS NULL "
               "OR mapping_tier != 'разметка'")   # ручную разметку не трогаем

    clinics = list(db.execute(
        "SELECT clinic_id, title, gate, sections_found, nonadjacent "
        "FROM clinics WHERE gate='Включён'"))
    stats = {"clinics": 0, "rows": 0, "tier1": 0, "to_markup": 0,
             "aggregates": 0, "collapsed": 0}
    for cl in clinics:
        cid, title = cl["clinic_id"], cl["title"]
        rows = list(db.execute(
            "SELECT id, name_raw, description_raw, page_url, price, row_type, "
            "mapping_tier FROM services_found WHERE clinic_id=? "
            "AND row_type != 'агрегат'", (cid,)))
        found_tags, esth_present = set(), False
        esth_plain, brand_rows = [], []
        for r in rows:
            if r["row_type"] in ("расходник", "служебное"):
                db.execute("UPDATE services_found SET mapping_basis=? WHERE id=?",
                           (f"тип строки: {r['row_type']} — не услуга, "
                            f"маппингу не подлежит", r["id"]))
                continue
            if r["mapping_tier"] == "разметка":
                continue   # ручная разметка сохраняется
            name = r["name_raw"]
            m1 = map_tier1(name, form_index, fuzzy_cutoff=fuzzy_cutoff)
            if m1:
                c = contours.get(m1["tag"])
                if c in ("cosm_est", "cosm_med"):
                    esth_present = True
                    found_tags.add(m1["tag"])
                    esth_plain.append(r)   # эстетика — кандидат свёртки
                    continue
                if is_esthetic_line(name, esth_kws):
                    # конфликт: словарный мед-тег при эстетическом маркере —
                    # слепой маппинг запрещён, спорное решает разметчик
                    esth_present = True
                    db.execute(
                        "UPDATE services_found SET mapping_tier='на разметке', "
                        "mapping_basis=? WHERE id=?",
                        ("конфликт: словарное совпадение при эстетическом "
                         "маркере в названии — решает разметчик", r["id"]))
                    stats["to_markup"] += 1
                    continue
                db.execute("UPDATE services_found SET tag=?, code_804n=?, "
                           "mapping_basis=?, mapping_tier=?, confidence=? "
                           "WHERE id=?",
                           (m1["tag"], m1["code_804n"], m1["basis"], m1["tier"],
                            m1["confidence"], r["id"]))
                found_tags.add(m1["tag"])
                stats["tier1"] += 1
                continue
            if BRAND_INJECTABLE_RE.search(name) and "волос" not in name.lower():
                esth_present = True
                brand_rows.append(r)
                continue
            if is_esthetic_line(name, esth_kws):
                esth_present = True
                esth_plain.append(r)
                continue
            db.execute("UPDATE services_found SET mapping_tier='на разметке', "
                       "mapping_basis=? WHERE id=?",
                       ("ступень 1: точного совпадения со справочником нет",
                        r["id"]))
            stats["to_markup"] += 1

        # ── Свёртка КАК КЛАСС (сохранена по указанию заказчика) ──
        if brand_rows:
            brands = []
            for b in brand_rows:
                m = re.search(r"[A-Za-z][A-Za-z0-9+\- ]{2,}", b["name_raw"])
                if m and m.group(0).strip() not in brands:
                    brands.append(m.group(0).strip())
            found_tags.add("contour_filler")
            stats["collapsed"] += _insert_agg(
                db, cid, title, brand_rows,
                "Инъекционная эстетика (филлеры/биоревитализанты)",
                "contour_filler",
                "агрегат брендов инъекционной эстетики (мера 2, 2026-08-26)",
                descr_prefix=f"бренды: {', '.join(brands)} | позиции: ")
            stats["aggregates"] += 1
        if esth_plain:
            groups: dict[str, list] = {}
            for r in esth_plain:
                groups.setdefault(_family_key(r["name_raw"]), []).append(r)
            rest = []
            for fam, grp in sorted(groups.items()):
                if len(grp) >= 3:
                    stats["collapsed"] += _insert_agg(
                        db, cid, title, grp,
                        grp[0]["name_raw"][:60].rstrip(" ,;:-"),
                        "hardware_rejuvenation",
                        "агрегат однотипного эстетического семейства "
                        "(свёртка как класс, 2026-08-26)")
                    stats["aggregates"] += 1
                else:
                    rest.extend(grp)
            if rest:
                stats["collapsed"] += _insert_agg(
                    db, cid, title, rest, "Эстетическая косметология — прочее",
                    "hardware_rejuvenation",
                    "агрегат эстетических позиций (мера 2)")
                stats["aggregates"] += 1
        if esth_present:
            found_tags.add("hardware_rejuvenation")

        # ── Клиника: тип/правило/флаги/грейд — из размеченного состава ──
        nonadj = [x for x in (cl["nonadjacent"] or "").split("; ") if x]
        prof_tags = found_tags & set(contours)
        if prof_tags:
            cls = classify(prof_tags, nonadjacent_found=nonadj, contours=contours)
            ctype, type_status = cls["type"], TYPE_STATUS_PRELIM
        else:
            cls = {"type": "Не классифицировано", "rule": None,
                   "esthetic_markers_found": [],
                   "flag_single_nonadjacent": False,
                   "flag_removal_outside_derm": False}
            ctype, type_status = ("Не классифицировано",
                                  "ожидает разметки (ступень 1 тегов не дала)")
        sections = set((cl["sections_found"] or "").split("; "))
        grade = "A" if {"услуги", "врачи", "прайс", "направления"} <= sections else "B"
        n_collapsed = db.execute(
            "SELECT COUNT(*) FROM services_found WHERE clinic_id=? "
            "AND collapsed_into IS NOT NULL", (cid,)).fetchone()[0]
        db.execute("UPDATE clinics SET type=?, type_status=?, rule=?, grade=?, "
                   "esthetic_markers=?, flag_single_nonadjacent=?, "
                   "flag_removal_outside_derm=?, agg_collapsed=? WHERE clinic_id=?",
                   (ctype, type_status, cls.get("rule"), grade,
                    "; ".join(cls["esthetic_markers_found"]) or None,
                    int(cls["flag_single_nonadjacent"]),
                    int(cls["flag_removal_outside_derm"]), n_collapsed, cid))
        stats["clinics"] += 1
        stats["rows"] += len(rows)
        if verbose:
            print(f"  {cid:30} тип={ctype:28} строк={len(rows)}")
    db.commit()
    return stats


if __name__ == "__main__":
    con = sqlite3.connect("data/osint.db")
    con.execute("PRAGMA busy_timeout=15000")
    st = recompute(con, verbose=True)
    print(f"пересчёт суждений: клиник {st['clinics']}, строк {st['rows']}, "
          f"кодом {st['tier1']}, на разметке {st['to_markup']}, "
          f"агрегатов {st['aggregates']} (свёрнуто {st['collapsed']})")
