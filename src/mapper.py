"""Маппинг услуг: ступень 1 — код (п.3 промпта этапа 6, 2026-08-26).

Ступень 1 — нормализация строки и точное совпадение с формулировками
справочника. Ступень 2 — РУЧНАЯ разметка батчей в Claude Code (решение
заказчика 2026-08-26, п.6: Anthropic API не используется, вызовов к внешним
моделям нет ни одного): всё, что ступень 1 не закрыла, выгружается в
output/{город}_на_разметку_{дата}.json, размечается агентом в Claude Code
по prompts/06_markup_batch.md и подхватывается src/merge_markup.py.
"""

import difflib
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

# Мера 1 (решение заказчика 2026-08-26): орфографические варианты одного и
# того же — риска нет. Унификация приставки приёма, ё→е, отсечение
# модификаторов (первичный/повторный, категории сложности, учёные степени).
_VISIT_PREFIX_RE = re.compile(
    r"^(прием|осмотр|консультация)(\s+(осмотр|консультация))*\s*(врача[-\s]*)?")
_MODIFIER_RE = re.compile(
    r"\b(первичн\w+|повторн\w+|амбулаторн\w+|лечебно-диагностическ\w+"
    r"|\d\s*(степени|категории)\s*сложности|высшей категории|\d\s*категории"
    r"|с выдачей плана( лечения)?|с назначением лечения"
    r"|кмн|дмн|кандидата медицинских наук|доктора медицинских наук|профессора?)\b")


def normalize_service_name(name: str) -> str:
    s = (name or "").lower().replace("ё", "е")
    s = _PAREN_RE.sub(" ", s)          # скобочные уточнения
    s = s.split("/")[0]                # слэш-хвосты
    s = _SIZE_RE.sub(" ", s)           # размеры, единицы, номера, кратности
    s = re.sub(r"[«»\"'.,;:№]", " ", s)
    s = _SPACE_RE.sub(" ", s).strip(" -–")
    # модификаторы — ДО унификации приставки: «Первичная консультация врача-…»
    # начинается с модификатора, иначе приставка не распознаётся (такт 3)
    s = _MODIFIER_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip(" -–")
    s = _VISIT_PREFIX_RE.sub("прием ", s)   # приём/консультация/осмотр = одно
    return _SPACE_RE.sub(" ", s).strip(" -–")


def build_formulation_index(services: dict | None = None) -> dict[str, str]:
    services = services or yaml.safe_load(_SERVICES.read_text(encoding="utf-8"))
    index = {}
    for t in services["tags"]:
        for phrase in [t["name_ru"], *t.get("formulations_site", []),
                       *t.get("formulations_wordstat", [])]:
            index.setdefault(normalize_service_name(phrase), t["tag"])
    index.pop("", None)
    return index


# Мера 3 (заказчик 2026-08-26): клинически опасные почти-совпадения —
# «удаление ДОБРОкачественных» ↔ «иссечение ЗЛОкачественных» дало 0.84;
# порог 0.92 в config/thresholds.yaml, ниже не опускать.
_ANTONYM_STEMS = ("злокачествен", "доброкачествен")


def map_tier1(raw_name: str, index: dict[str, str],
              fuzzy_cutoff: float | None = None) -> dict | None:
    key = normalize_service_name(raw_name)
    tag = index.get(key)
    if tag:
        return {"tag": tag, "code_804n": None, "basis": "точное совпадение формулировки",
                "tier": "код", "confidence": "высокая"}
    if fuzzy_cutoff:
        best = difflib.get_close_matches(key, list(index), n=1, cutoff=fuzzy_cutoff)
        if best:
            b = best[0]
            for stem in _ANTONYM_STEMS:   # добро/злокачественный не сближаем
                if (stem in key) != (stem in b):
                    return None
            return {"tag": index[b], "code_804n": None,
                    "basis": f"нечёткое совпадение ≥{fuzzy_cutoff} с «{b}»",
                    "tier": "код", "confidence": "средняя"}
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
