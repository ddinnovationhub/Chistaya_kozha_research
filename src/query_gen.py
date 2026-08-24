"""Детерминированная генерация поисковых запросов — 7 слоёв (этап 4).

Контракт детерминизма (ТЗ + паттерн workflow-orchestration: determinism
constraints): никаких now()/random/переупорядочивания — один город, прогнанный
дважды, даёт байт-в-байт тот же набор query_id. Запрос НЕ придумывается
моделью: только шаблон × словарь.

query_id = L{слой}-{template_id}-{city_code}-{param_hash}

Слой L7 (разведочные) здесь НЕ генерируется: он создаётся вручную с полем
«что натолкнуло» и не входит в покрытие до одобрения заказчика.

Известная неполнота сида (такт 3, Методолог): пока Вордстат не выгружен,
L3 использует названия нозологий из МКБ («гнёздная алопеция») — пациент так
не ищет. Слой полноценен только после data/wordstat/. Отражать в отчёте прогона.

CLI: python -m src.query_gen --city "Казань" [--districts data/city_districts.json]
"""

import argparse
import hashlib
import json
import pathlib
import re
import unicodedata

import yaml

# ── Фиксированные словари слоёв (менять только через коммит) ──────────────
L1_RUBRICS = [
    "частная клиника", "медицинский центр", "многопрофильная клиника",
    "дерматология", "дерматовенерология", "трихология", "косметология",
    "лазерная косметология", "удаление новообразований", "онкодерматология",
    "эстетическая медицина", "лечение кожи",
]

L4_SPECIALTIES = [
    "дерматолог", "детский дерматолог", "дерматовенеролог",
    "онкодерматолог", "трихолог", "дерматохирург",
]

L5_TEMPLATES = [
    ("official", "{brand} {city} официальный сайт"),
    ("reviews", "{brand} {city} отзывы"),
    ("inn", "{brand} {city} ИНН"),
    ("license", "{brand} {city} лицензия"),
]

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def city_code(city: str) -> str:
    s = unicodedata.normalize("NFC", city).lower().strip()
    out = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    return re.sub(r"[^a-z0-9]+", "", out) or "x"


def _param_hash(*parts: str) -> str:
    norm = "|".join(" ".join(str(p).lower().split()) for p in parts)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


# Целевой источник исполнения слоя (такт 3, Разведчик): L1 — рубрики
# КАТАЛОГОВ (2ГИС, Яндекс Карты), не текстовый веб-поиск; остальные слои —
# Яндекс Search API. Исполнитель этапа 5 маршрутизирует по этому полю.
_LAYER_TARGETS = {
    1: ["gis2", "yandex_maps"],
    2: ["yandex_search_api"],
    3: ["yandex_search_api"],
    4: ["yandex_search_api"],
    5: ["yandex_search_api"],
    6: ["yandex_search_api"],
}


def _q(layer: int, template_id: str, city: str, text: str, source: str,
       wordstat_freq=None) -> dict:
    return {
        "query_id": f"L{layer}-{template_id}-{city_code(city)}-{_param_hash(template_id, text)}",
        "layer": layer,
        "template_id": template_id,
        "city": city,
        "text": text,
        "source": source,
        "target_sources": _LAYER_TARGETS[layer],
        "wordstat_freq": wordstat_freq,
    }


def generate_l1(city: str) -> list[dict]:
    return [_q(1, "rubric", city, f"{r} {city}", "L1_RUBRICS") for r in L1_RUBRICS]


def _freq_order(items: list[dict]) -> list[dict]:
    """Частотность управляет ПОРЯДКОМ, не составом (ТЗ): по убыванию,
    без частоты — после всех с частотой; ничья и None — по алфавиту текста."""
    return sorted(items, key=lambda d: (
        d["wordstat_freq"] is None,
        -(d["wordstat_freq"] or 0),
        d["text"],
    ))


def generate_l2(city: str, services: dict) -> list[dict]:
    out = []
    for tag in services["tags"]:
        seen_texts = set()
        for phrase in [tag["name_ru"], *tag.get("formulations_site", [])]:
            text = f"{phrase} {city}"
            if text in seen_texts:
                continue
            seen_texts.add(text)
            out.append(_q(2, f"svc-{tag['tag']}", city, text,
                          "services.yaml", tag.get("wordstat_freq")))
    return _freq_order(out)


def generate_l3(city: str, nosology: dict) -> list[dict]:
    out = []
    for n in nosology["nosologies"]:
        phrases = n.get("patient_phrases") or [n["name"]]
        for phrase in phrases:
            out.append(_q(3, f"nos-{n['icd10']}", city, f"{phrase} {city}",
                          "nosology.yaml", n.get("wordstat_freq")))
    return _freq_order(out)


def generate_l4(city: str) -> list[dict]:
    return [_q(4, "spec", city, f"{s} {city} запись", "L4_SPECIALTIES")
            for s in L4_SPECIALTIES]


def generate_l5(city: str, brands: list[str]) -> list[dict]:
    out = []
    for brand in sorted(set(brands)):
        for tid, tpl in L5_TEMPLATES:
            out.append(_q(5, f"brand-{tid}", city,
                          tpl.format(brand=brand, city=city), "discovered_brands"))
    return out


def generate_l6(city: str, services: dict, districts: list[str]) -> list[dict]:
    """Только для городов >1 млн (наличие районов в data/city_districts.json —
    и есть включатель слоя). Ключевые услуги × районы."""
    key_tags = [t for t in services["tags"]
                if t["contour"] in ("derm", "oncoderm", "dermsurg")]
    out = []
    for d in sorted(districts):
        for tag in key_tags:
            out.append(_q(6, f"geo-{tag['tag']}", city,
                          f"{tag['name_ru']} {city} {d}", "city_districts.json"))
    return out


def generate_all(city: str, services: dict, nosology: dict,
                 districts: list[str] | None = None) -> list[dict]:
    queries = [
        *generate_l1(city),
        *generate_l2(city, services),
        *generate_l3(city, nosology),
        *generate_l4(city),
        # L5 — вторая волна, после обнаружения брендов (generate_l5)
        *(generate_l6(city, services, districts) if districts else []),
    ]
    ids = [q["query_id"] for q in queries]
    assert len(ids) == len(set(ids)), "дубль query_id — нарушение детерминизма"
    return queries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--districts", default=None,
                    help="JSON-файл районов (включает слой L6)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    services = yaml.safe_load(pathlib.Path("dictionaries/services.yaml").read_text(encoding="utf-8"))
    nosology = yaml.safe_load(pathlib.Path("dictionaries/nosology.yaml").read_text(encoding="utf-8"))
    districts = None
    if args.districts:
        data = json.loads(pathlib.Path(args.districts).read_text(encoding="utf-8"))
        districts = data.get(args.city)

    queries = generate_all(args.city, services, nosology, districts)
    out_path = pathlib.Path(args.out or f"data/queries_{city_code(args.city)}.jsonl")
    with out_path.open("w", encoding="utf-8") as f:
        for q in queries:
            f.write(json.dumps(q, ensure_ascii=False) + "\n")
    by_layer = {}
    for q in queries:
        by_layer[q["layer"]] = by_layer.get(q["layer"], 0) + 1
    print(f"Город: {args.city} · запросов: {len(queries)} · по слоям: {by_layer}")
    print(f"Файл: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
