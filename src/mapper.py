"""Двухступенчатый маппинг услуг (п.3 промпта этапа 6, 2026-08-26).

Ступень 1 — код, без модели: нормализация строки и точное совпадение
с формулировками справочника.
Ступень 2 — модель (Anthropic API), ТОЛЬКО для несовпавшего, ОДИН вызов
на клинику со всеми её неопознанными услугами разом.

Жёсткие запреты модели (вшиты в системный промпт):
- не додумывать услугу сверх текста названия и описания;
- не присваивать код 804н «по смыслу» — лучше «код не определён»
  (официальных разъяснений к 804н не существует — принято как факт);
- маркетинговые пакеты кодом не размечать, помечать «пакет».
"""

import json
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


# ── Ступень 2: модель, батч на клинику ───────────────────────────────────
_SCHEMA = {
    "type": "object",
    "properties": {
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "tag": {"type": ["string", "null"]},
                    "code_804n": {"type": ["string", "null"]},
                    "basis": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["высокая", "средняя", "низкая"]},
                    "is_package": {"type": "boolean"},
                },
                "required": ["name", "tag", "code_804n", "basis", "confidence", "is_package"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["services"],
    "additionalProperties": False,
}

_SYSTEM = """Ты — врач-методолог проекта разведки рынка дерматологических клиник.
Твоя задача: сопоставить услуги с сайта клиники с тегами справочника проекта.

ЖЁСТКИЕ ЗАПРЕТЫ (методология проекта запрещает галлюцинации):
1. НЕ додумывай услугу сверх текста названия и описания.
2. НЕ присваивай код номенклатуры 804н «по смыслу»: официальных разъяснений
   к кодам не существует; если наименование в номенклатуре не соответствует
   тексту услуги — ставь code_804n = null (это нормальный результат, не дефект).
3. Маркетинговые упаковки («Онкодозор», «Комплекс …», «… под ключ», Check UP)
   кодом НЕ размечай: is_package = true, tag = null.
4. Если подходящего тега нет — tag = null. Не натягивай ближайший.
5. basis — короткая фраза, ПО КАКОМУ ПРИЗНАКУ решено (проверяемая).
6. Если описания нет, маппинг только по названию — в basis добавь
   «только название, описание отсутствует».
Верни ровно по одной записи на каждую входную услугу, в том же порядке."""


def map_tier2_batch(clinic_name: str, unmapped: list[dict],
                    tags_reference: str, model: str, budget=None,
                    client=None) -> list[dict]:
    """unmapped: [{name, description|None}]. ОДИН вызов на клинику.
    Возвращает список в формате _SCHEMA['services']."""
    import anthropic
    client = client or anthropic.Anthropic()

    lines = []
    for s in unmapped:
        d = s.get("description") or ""
        lines.append(f"- {s['name']}" + (f" — {d[:300]}" if d else " (описания нет)"))
    user = (f"Клиника: {clinic_name}\n\nСправочник тегов проекта:\n{tags_reference}\n\n"
            f"Неопознанные услуги ({len(unmapped)}):\n" + "\n".join(lines))

    if budget is not None:
        budget.charge("anthropic", 1)  # фиксация вызова до отправки
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": user}],
    )
    if budget is not None:
        budget.charge_tokens("anthropic", response.usage.input_tokens,
                             response.usage.output_tokens)
    text = next(b.text for b in response.content if b.type == "text")
    result = json.loads(text)["services"]
    for r in result:
        r["tier"] = "модель"
    return result


def tags_reference_text(services: dict | None = None) -> str:
    """Компактная справка тегов для промпта ступени 2."""
    services = services or yaml.safe_load(_SERVICES.read_text(encoding="utf-8"))
    lines = []
    for t in services["tags"]:
        codes = ", ".join(n["code"] for n in t.get("nomenclature", [])) or "кода в 804н нет"
        lines.append(f"{t['tag']} · {t['name_ru']} · контур {t['contour']} · 804н: {codes}")
    return "\n".join(lines)
