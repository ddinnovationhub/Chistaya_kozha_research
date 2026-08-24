"""Маппинг услуг: ступень 1 — код (п.3 промпта этапа 6, 2026-08-26).

Ступень 1 — нормализация строки и точное совпадение с формулировками
справочника. Ступень 2 — РУЧНАЯ разметка батчей в Claude Code (решение
заказчика 2026-08-26, п.6: Anthropic API не используется, вызовов к внешним
моделям нет ни одного): всё, что ступень 1 не закрыла, выгружается в
output/{город}_на_разметку_{дата}.json, размечается агентом в Claude Code
по prompts/06_markup_batch.md и подхватывается src/merge_markup.py.
"""

import pathlib
import re

import yaml

_SERVICES = pathlib.Path("dictionaries/services.yaml")

# ── Ступень 1: нормализация и точное совпадение ──────────────────────────
_SIZE_RE = re.compile(
    r"(от\s+)?\d+([,.]\d+)?\s*(-|–|до)\s*\d+([,.]\d+)?\s*(см|мм|мл|ml|г|кв\.?\s*см)"
    r"|\d+([,.]\d+)?\s*(см|мм|мл|ml|г|кв\.?\s*см)"
    r"|(от|до|св\.?|свыше)\s+\d+([,.]\d+)?(\s*(см|мм|мл|ml))?"
    r"|№\s*\d+(\s*-\s*\d+)?"
    r"|\d+\s*(эл(емент(ов|а)?)?|ед|шт|процедур[аы]?|зон[аы]?|этап(ная|а)?)\b\.?"
    r"|\d+\s*кат\.?\s*сложности"
    r"|за\s+1\s+ед\.?"
    r"|\bмелких\b|\bкрупных\b",   # размерные прилагательные — та же ценовая градация
    re.IGNORECASE)
_PAREN_RE = re.compile(r"\([^)]*\)")
_SPACE_RE = re.compile(r"\s+")


def normalize_service_name(name: str) -> str:
    s = (name or "").lower()
    s = _PAREN_RE.sub(" ", s)          # скобочные уточнения
    s = s.split("/")[0]                # слэш-хвосты
    s = _SIZE_RE.sub(" ", s)           # размеры, единицы, номера, кратности
    s = re.sub(r"[«»\"'.,;:№]", " ", s)
    s = _SPACE_RE.sub(" ", s).strip(" -–")
    return s


def build_formulation_index(services: dict | None = None) -> dict[str, str]:
    services = services or yaml.safe_load(_SERVICES.read_text(encoding="utf-8"))
    index = {}
    for t in services["tags"]:
        for phrase in [t["name_ru"], *t.get("formulations_site", []),
                       *t.get("formulations_wordstat", [])]:
            index.setdefault(normalize_service_name(phrase), t["tag"])
    index.pop("", None)
    return index


def map_tier1(raw_name: str, index: dict[str, str]) -> dict | None:
    key = normalize_service_name(raw_name)
    tag = index.get(key)
    if tag:
        return {"tag": tag, "code_804n": None, "basis": "точное совпадение формулировки",
                "tier": "код", "confidence": "высокая"}
    return None


def tags_reference_text(services: dict | None = None) -> str:
    """Компактная справка тегов — вкладывается в файл «на разметку», чтобы
    батч был самодостаточным для разметки в Claude Code."""
    services = services or yaml.safe_load(_SERVICES.read_text(encoding="utf-8"))
    lines = []
    for t in services["tags"]:
        codes = ", ".join(n["code"] for n in t.get("nomenclature", [])) or "кода в 804н нет"
        lines.append(f"{t['tag']} · {t['name_ru']} · контур {t['contour']} · 804н: {codes}")
    return "\n".join(lines)
