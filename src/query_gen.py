"""[ВЫВЕДЕН ИЗ ФЛОУ — promt_spark_krug, 2026-08-25] Вход теперь — выборка СПАРК (src/spark_import.py), слепой discovery отключён. Код сохранён на случай возврата подхода.

Детерминированная генерация поисковых запросов — 7 слоёв (этап 4).

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


# ── Приоритет L2 (промпт исправления 2026-08-26, п.1) ─────────────────────
# Группа 1 — теги из data/client_profile.yaml (что оказывает клиент);
# Группа 2 — остальное ядро дерматологического контура;
# Группа 3 — прочее. Внутри группы: частотность Вордстата по убыванию,
# при отсутствии частот (сейчас) — по алфавиту текста.
DERM_CORE_CONTOURS = {"derm", "oncoderm", "trich", "dermsurg"}
_CLIENT_PROFILE = pathlib.Path("data/client_profile.yaml")


def load_client_tags(path: pathlib.Path = _CLIENT_PROFILE) -> set[str]:
    return set(yaml.safe_load(path.read_text(encoding="utf-8"))["tags"])


def assert_profile_contours(services: dict, client_tags: set[str]):
    """Ошибка СБОРКИ, а не молчаливое расхождение (заказчик, 2026-08-26, п.4):
    тег из профиля клиента в контуре cosm_est противоречит регрессии
    «эстетических маркеров у клиента ноль» (второй случай после laser_vascular).
    Неизвестный тег профиля ловит переименования в справочнике."""
    by_tag = {t["tag"]: t for t in services["tags"]}
    unknown = sorted(client_tags - set(by_tag))
    if unknown:
        raise ValueError(f"ошибка сборки: теги client_profile отсутствуют в services.yaml: {unknown}")
    esthetic = sorted(t for t in client_tags if by_tag[t]["contour"] == "cosm_est")
    if esthetic:
        raise ValueError(
            f"ошибка сборки: теги client_profile лежат в контуре cosm_est: {esthetic} — "
            f"противоречит регрессии «эстетических маркеров у клиента ноль»")


def _priority_group(tag: dict, client_tags: set[str]) -> int:
    if tag["tag"] in client_tags:
        return 1
    if tag["contour"] in DERM_CORE_CONTOURS:
        return 2
    return 3


def _priority_order(items: list[tuple[int, dict]]) -> list[dict]:
    """(группа, запрос) → приоритет, внутри группы частотность, затем алфавит."""
    ranked = sorted(items, key=lambda gi: (
        gi[0],
        gi[1]["wordstat_freq"] is None,
        -(gi[1]["wordstat_freq"] or 0),
        gi[1]["text"],
    ))
    return [q for _, q in ranked]


_BAD_SEARCH_CHARS = set("/();№«»")


def make_search_phrase(phrase: str) -> str | None:
    """Правило заказчика (п.2.6, 2026-08-26): в поисковую фразу не попадают
    названия длиннее 5 слов и со спецсимволами / ( ) ; — длинные прайсовые
    формулировки остаются в справочнике для РАСПОЗНАВАНИЯ, но в поиск не идут.
    Скобочные/слэшевые хвосты детерминированно отсекаются перед проверкой."""
    s = phrase.split("(")[0].split("/")[0].strip(" ,·-")
    if not s or any(ch in _BAD_SEARCH_CHARS for ch in s):
        return None
    words = len(s.split())
    if words > 5:
        return None
    if words == 1 and s != phrase.strip():
        return None   # обрубок после обрезки («Гистологическое /...» → «Гистологическое»)
    return s


def tag_search_phrases(tag: dict) -> list[str]:
    """Поисковые фразы тега с учётом политики поиска (2026-08-26, пп.2-3):
    use_in_search=нет → тег в поиск не идёт (остаётся для распознавания);
    search_needs_profile_qualifier=да → ТОЛЬКО явные search_phrases
    (квалифицированные уточнением профиля), не name_ru/formulations."""
    if tag.get("use_in_search", "да") == "нет":
        return []
    if tag.get("search_needs_profile_qualifier", "нет") == "да":
        raw = tag.get("search_phrases", [])
    else:
        raw = [tag["name_ru"], *tag.get("formulations_site", [])]
    out, seen = [], set()
    for phrase in raw:
        sp = make_search_phrase(phrase)
        if sp is not None and sp.lower() not in seen:
            seen.add(sp.lower())
            out.append(sp)
    return out


def generate_l2(city: str, services: dict, client_tags: set[str]) -> list[dict]:
    entries = []
    for tag in services["tags"]:
        group = _priority_group(tag, client_tags)
        for sp in tag_search_phrases(tag):
            entries.append((group, _q(2, f"svc-{tag['tag']}", city, f"{sp} {city}",
                                      "services.yaml", tag.get("wordstat_freq"))))
    ordered = _priority_order(entries)
    # междутеговый дедуп текста: одинаковая фраза у двух тегов — один запрос,
    # остаётся у более приоритетного (первого в порядке)
    out, seen = [], set()
    for q in ordered:
        if q["text"].lower() not in seen:
            seen.add(q["text"].lower())
            out.append(q)
    return out


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


# L6 — ЗАКРЫТЫЙ список ключевых услуг (заказчик, 2026-08-26, п.5): в ТЗ
# «ключевые услуги × районы», а не «все услуги × районы» — иначе слой съедает
# 69% бюджета города. Состав: пять приёмов + пять групп удалений + дерматоскопия.
L6_KEY_TAGS = [
    "derm_consult", "onco_consult", "derm_consult_child", "trich_consult",
    "dermsurg_consult",
    "removal_pigmented", "removal_viral", "removal_keratosis",
    "removal_soft_tissue", "removal_vascular",
    "dermatoscopy",
]


def generate_l6(city: str, services: dict, districts: list[str]) -> list[dict]:
    """Только для городов >1 млн (наличие районов в data/city_districts.json —
    и есть включатель слоя). Ключевые услуги (L6_KEY_TAGS) × районы,
    одна каноническая фраза на тег."""
    by_tag = {t["tag"]: t for t in services["tags"]}
    out = []
    for d in sorted(districts):
        for key in L6_KEY_TAGS:
            phrases = tag_search_phrases(by_tag[key])
            if not phrases:
                continue
            out.append(_q(6, f"geo-{key}", city,
                          f"{phrases[0]} {city} {d}", "city_districts.json"))
    return out


def generate_all(city: str, services: dict, nosology: dict,
                 districts: list[str] | None = None,
                 client_tags: set[str] | None = None) -> list[dict]:
    client_tags = client_tags if client_tags is not None else load_client_tags()
    assert_profile_contours(services, client_tags)   # ошибка сборки, не расхождение
    queries = [
        *generate_l1(city),
        *generate_l2(city, services, client_tags),
        *generate_l3(city, nosology),
        *generate_l4(city),
        # L5 — вторая волна, после обнаружения брендов (generate_l5)
        *(generate_l6(city, services, districts) if districts else []),
    ]
    ids = [q["query_id"] for q in queries]
    assert len(ids) == len(set(ids)), "дубль query_id — нарушение детерминизма"
    banned = {t["tag"] for t in services["tags"] if t.get("use_in_search", "да") == "нет"}
    for q in queries:
        if q["layer"] in (2, 6):
            tag = q["template_id"].split("-", 1)[1]
            assert tag not in banned, f"тег {tag} с use_in_search=нет попал в запросы"
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
