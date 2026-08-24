"""Recall-тест: сколько ЗАРАНЕЕ ИЗВЕСТНЫХ клиник города нашла разведка.

Список известных — data/recall_test_{Город}.yaml (ручная выгрузка заказчика,
каждая запись с source_id). Совпадение считается по домену сайта ИЛИ по
нормализованному названию (G0-нормализация). Результат выводится в финальный
отчёт прогона: «Recall-тест (известные клиники): {найдено}/{всего}».
"""

import pathlib
import sqlite3

import yaml

from src.dedup import normalize_domain, normalize_name


def recall_path(city: str) -> pathlib.Path:
    return pathlib.Path("data") / f"recall_test_{city}.yaml"


def compute_recall(city: str, db: sqlite3.Connection) -> dict | None:
    """None — файла recall-теста нет (отметить в отчёте, не молчать)."""
    path = recall_path(city)
    if not path.exists():
        return None
    known = yaml.safe_load(path.read_text(encoding="utf-8"))["clinics"]

    cand_domains, cand_names = set(), set()
    for title, url, domain in db.execute("SELECT title, url, domain FROM candidates"):
        if domain:
            cand_domains.add(domain)
        if title:
            cand_names.add(normalize_name(title))

    found, missed = [], []
    for c in known:
        dom = normalize_domain(c.get("site") or "")
        name = normalize_name(c["name"])
        hit_by = None
        if dom and dom in cand_domains:
            hit_by = f"домен {dom}"
        elif name and any(name in cn or cn in name for cn in cand_names if cn):
            hit_by = "название"
        (found if hit_by else missed).append({"name": c["name"], "hit_by": hit_by})
    return {
        "total": len(known),
        "found": len(found),
        "missed": [m["name"] for m in missed],
        "source": str(path),
    }
