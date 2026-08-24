"""Подмешивание ручного сбора для недоступных сайтов (п.6 второго промпта
исправления, 2026-08-26): клиники, не взятые уровнем 4 каскада, заказчик
открывает вручную; содержимое передаётся файлом и вливается в общую таблицу
с пометкой источника «ручной сбор».

Формат входа — output/{город}_ручной_сбор_{дата}.json:
    {"clinics": [{"domain": "example.ru",
                  "services": [{"name": "...", "description": null,
                                "price": "1 200 ₽"}, ...]}]}

Правила:
- принимаются только клиники с flag_site_unreachable=1 (для остальных данные
  уже собраны каскадом — подмена запрещена);
- услуги проходят ту же ступень 1, несопоставленное — «на разметке»
  (общий поток prompts/06_markup_batch.md);
- источник каждой строки: page_url = «ручной сбор заказчика {дата}» —
  происхождение видно в 02_Услуги;
- статус клиники: «Требует проверки / Сайт недоступен» снимается на
  «Включён / ручной сбор заказчика», грейд не выше B (сайт агент не видел),
  флаг flag_site_unreachable остаётся — история доступности не стирается.

Запуск: python -m src.manual_import --city 'Новосибирск' --file output/..._ручной_сбор_....json
"""

import argparse
import datetime
import json
import pathlib
import sqlite3

import yaml

from src.mapper import build_formulation_index, map_tier1


def import_manual(city: str, path: pathlib.Path, db: sqlite3.Connection) -> dict:
    form_index = build_formulation_index()
    client_tags = set(yaml.safe_load(
        pathlib.Path("data/client_profile.yaml").read_text(encoding="utf-8"))["tags"])
    data = json.loads(path.read_text(encoding="utf-8"))
    day = datetime.date.today().isoformat()
    source_mark = f"ручной сбор заказчика {day}"

    imported, skipped = 0, []
    for c in data["clinics"]:
        dom = c["domain"]
        row = db.execute("SELECT clinic_id, title, flag_site_unreachable "
                         "FROM clinics WHERE domain=?", (dom,)).fetchone()
        if row is None:
            skipped.append((dom, "домена нет в clinics — сначала прогон этапа 6"))
            continue
        clinic_id, title, unreachable = row
        if not unreachable:
            skipped.append((dom, "сайт был взят каскадом — ручная подмена запрещена"))
            continue
        for s in c["services"]:
            m1 = map_tier1(s["name"], form_index)
            db.execute(
                "INSERT INTO services_found (clinic_id, clinic_title, name_raw, "
                "description_raw, page_url, price, tag, code_804n, mapping_basis, "
                "mapping_tier, confidence, client_has) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (clinic_id, title, s["name"], s.get("description"), source_mark,
                 s.get("price"),
                 m1["tag"] if m1 else None, None,
                 m1["basis"] if m1 else "ступень 1: точного совпадения со справочником нет",
                 m1["tier"] if m1 else "на разметке",
                 m1["confidence"] if m1 else None,
                 ("Да" if m1 and m1["tag"] in client_tags else "Нет") if m1 else None))
        db.execute("UPDATE clinics SET gate='Включён', "
                   "gate_reason='ручной сбор заказчика', grade='B', "
                   "type_status='ожидает разметки (ручной сбор)' WHERE clinic_id=?",
                   (clinic_id,))
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--db", default="data/osint.db")
    args = ap.parse_args()
    db = sqlite3.connect(args.db)
    res = import_manual(args.city, pathlib.Path(args.file), db)
    print(f"Ручной сбор влит: клиник {res['imported']} · пропущено {len(res['skipped'])}")
    for dom, why in res["skipped"]:
        print(f"  ✗ {dom}: {why}")
    from src.export_stage6 import export_intermediate, export_markup
    out = export_intermediate(args.city, db)
    markup = export_markup(args.city, db)
    print(f"Выгрузка перевыпущена: {out}")
    if markup:
        print(f"Файл «на разметку» обновлён: {markup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
