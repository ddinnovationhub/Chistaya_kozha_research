"""Слияние ручной разметки в базу (этап 6 без внешнего API, п.6, 2026-08-26).

Вход: output/{город}_разметка_{дата}.json, созданный агентом в Claude Code
по prompts/06_markup_batch.md:
    {"rows": [{"row_id": int, "tag": str|null, "code_804n": str|null,
               "basis": str, "confidence": "высокая"|"средняя"|"низкая",
               "is_package": bool}, ...]}

Что делает:
1. Валидирует каждый row: row_id существует и ждёт разметки; tag — из
   services.yaml или null; confidence — из словаря. Невалидная строка →
   отчёт, строка НЕ пишется (никаких тихих исправлений).
2. Обновляет services_found: mapping_tier='разметка'.
3. Переклассифицирует каждую затронутую клинику по ПОЛНОМУ набору тегов
   (ступень 1 + разметка): type_status='финальный (после разметки)'.
4. Перевыпускает промежуточную выгрузку.

Запуск: python -m src.merge_markup --city 'Новосибирск' --file output/..._разметка_....json
"""

import argparse
import json
import pathlib
import sqlite3

import yaml

from src.classify import classify, load_contours
from src.site_checker import TYPE_STATUS_FINAL

_CONFIDENCE = {"высокая", "средняя", "низкая"}


def merge_markup(city: str, markup_path: pathlib.Path,
                 db: sqlite3.Connection) -> dict:
    services = yaml.safe_load(pathlib.Path("dictionaries/services.yaml").read_text(encoding="utf-8"))
    known_tags = {t["tag"] for t in services["tags"]}
    client_tags = set(yaml.safe_load(
        pathlib.Path("data/client_profile.yaml").read_text(encoding="utf-8"))["tags"])
    contours = load_contours()

    data = json.loads(markup_path.read_text(encoding="utf-8"))
    pending = {row[0] for row in db.execute(
        "SELECT id FROM services_found WHERE mapping_tier='на разметке'")}

    applied, rejected = 0, []
    touched_clinics = set()
    for r in data["rows"]:
        rid = r.get("row_id")
        problems = []
        if rid not in pending:
            problems.append("row_id не существует или уже размечен")
        if r.get("tag") is not None and r["tag"] not in known_tags:
            problems.append(f"тег «{r.get('tag')}» отсутствует в services.yaml")
        if r.get("confidence") not in _CONFIDENCE:
            problems.append(f"confidence «{r.get('confidence')}» вне словаря")
        if not r.get("basis"):
            problems.append("пустой basis — основание маппинга обязательно")
        if problems:
            rejected.append({"row_id": rid, "problems": problems})
            continue
        tag = r["tag"]
        basis = r["basis"] + (" [пакет]" if r.get("is_package") else "")
        db.execute(
            "UPDATE services_found SET tag=?, code_804n=?, mapping_basis=?, "
            "mapping_tier='разметка', confidence=?, client_has=? WHERE id=?",
            (tag, r.get("code_804n"), basis, r["confidence"],
             "Да" if tag in client_tags else "Нет", rid))
        pending.discard(rid)
        applied += 1
        cid = db.execute("SELECT clinic_id FROM services_found WHERE id=?",
                         (rid,)).fetchone()
        if cid:
            touched_clinics.add(cid[0])

    # ── Финальная классификация затронутых клиник ──
    reclassified = 0
    for cid in sorted(touched_clinics):
        left = db.execute("SELECT COUNT(*) FROM services_found "
                          "WHERE clinic_id=? AND mapping_tier='на разметке'",
                          (cid,)).fetchone()[0]
        if left:
            continue  # клиника размечена не целиком — тип остаётся предварительным
        gate = db.execute("SELECT gate, nonadjacent FROM clinics WHERE clinic_id=?",
                          (cid,)).fetchone()
        if not gate or gate[0] != "Включён":
            continue
        found_tags = {row[0] for row in db.execute(
            "SELECT DISTINCT tag FROM services_found WHERE clinic_id=? AND tag IS NOT NULL",
            (cid,))} & set(contours)
        nonadj = (gate[1] or "").split("; ") if gate[1] else []
        cls = classify(found_tags, nonadjacent_found=nonadj, contours=contours) \
            if found_tags else {"type": "Не классифицировано", "rule": None,
                                "esthetic_markers_found": [], "nonadjacent_found": nonadj,
                                "flag_single_nonadjacent": False,
                                "flag_removal_outside_derm": False}
        db.execute(
            "UPDATE clinics SET type=?, type_status=?, rule=?, esthetic_markers=?, "
            "flag_single_nonadjacent=?, flag_removal_outside_derm=? WHERE clinic_id=?",
            (cls["type"], TYPE_STATUS_FINAL, cls.get("rule"),
             "; ".join(cls["esthetic_markers_found"]) or None,
             int(cls["flag_single_nonadjacent"]), int(cls["flag_removal_outside_derm"]),
             cid))
        reclassified += 1
    db.commit()
    return {"applied": applied, "rejected": rejected,
            "clinics_reclassified": reclassified,
            "still_pending": len(pending)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--db", default="data/osint.db")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    res = merge_markup(args.city, pathlib.Path(args.file), db)
    print(f"Разметка слита: строк принято {res['applied']} · "
          f"отклонено {len(res['rejected'])} · "
          f"клиник переклассифицировано {res['clinics_reclassified']} · "
          f"осталось на разметке {res['still_pending']}")
    for r in res["rejected"]:
        print(f"  ✗ row_id={r['row_id']}: {'; '.join(r['problems'])}")
    from src.export_stage6 import export_intermediate
    out = export_intermediate(args.city, db)
    print(f"Промежуточная выгрузка перевыпущена: {out}")
    return 0 if not res["rejected"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
