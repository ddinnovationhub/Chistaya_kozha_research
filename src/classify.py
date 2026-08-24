"""Механическое применение правил классификатора (dictionaries/classifier.yaml).

Правила R1-R5 применяются по порядку, первое сработавшее — финальное.
Никакой эвристики: контуры берутся из services.yaml, группы и маркеры —
из classifier.yaml. Изменение правил = изменение YAML, не кода.
"""

import pathlib

import yaml

_SERVICES = pathlib.Path("dictionaries/services.yaml")
_CLASSIFIER = pathlib.Path("dictionaries/classifier.yaml")

# Диагностические/приёмные теги дерматологии для флага
# flag_removal_outside_derm (синхронизировано с classifier.yaml, output_fields)
_DERM_DIAG_TAGS = {
    "derm_consult", "derm_consult_child", "dermsurg_consult", "onco_consult",
    "dermatoscopy", "mole_mapping", "histology_skin", "skin_biopsy",
    "std_consult", "std_lab",
}


def load_contours(services_path: pathlib.Path = _SERVICES) -> dict[str, str]:
    sv = yaml.safe_load(services_path.read_text(encoding="utf-8"))
    return {t["tag"]: t["contour"] for t in sv["tags"]}


def load_groups(classifier_path: pathlib.Path = _CLASSIFIER) -> dict:
    cl = yaml.safe_load(classifier_path.read_text(encoding="utf-8"))
    decisive = [d["name"] for d in cl["nonadjacent_directions"]
                if d.get("decisive", True)]
    return {"groups": cl["contour_groups"], "decisive_nonadjacent": decisive}


def classify(found_tags: set[str], nonadjacent_found: list[str] | None = None,
             sections_viewed: bool = True,
             contours: dict[str, str] | None = None,
             classifier: dict | None = None) -> dict:
    """found_tags — теги services.yaml, найденные на сайте Facility.
    nonadjacent_found — имена несмежных направлений (по classifier.yaml).
    Возвращает тип + выходные поля классификации."""
    contours = contours or load_contours()
    cfg = classifier or load_groups()
    groups = cfg["groups"]
    nonadjacent_found = nonadjacent_found or []

    unknown = found_tags - set(contours)
    if unknown:
        raise ValueError(f"неизвестные теги (нет в services.yaml): {sorted(unknown)}")

    decisive_nonadj = [n for n in nonadjacent_found if n in cfg["decisive_nonadjacent"]]
    tag_contours = {t: contours[t] for t in found_tags}
    has = lambda group: any(c in groups[group] for c in tag_contours.values())  # noqa: E731

    esthetic_markers = sorted(t for t, c in tag_contours.items() if c in groups["esthetic"])
    dermsurg_tags = sorted(t for t, c in tag_contours.items() if c == "dermsurg")

    if not sections_viewed or not found_tags:
        ctype, rule = "Не классифицировано", "R5_unclassified"
    elif decisive_nonadj:
        ctype, rule = "Тип 3", "R1_type3"
    elif has("esthetic") and has("medical_derm"):
        ctype, rule = "Тип 2", "R2_type2"
    elif has("medical_derm"):
        ctype, rule = "Тип 1", "R3_type1_derm"
    elif has("esthetic"):
        ctype, rule = "Тип 1 (косметологический)", "R4_type1_cosm"
    elif has("medical_cosmetology"):
        ctype, rule = "Тип 1 (косметологический)", "R4a_type1_cosm_med_only"
    else:
        ctype, rule = "Не классифицировано", "R5_unclassified"

    return {
        "type": ctype,
        "rule": rule,
        "esthetic_markers_found": esthetic_markers,
        "nonadjacent_found": sorted(nonadjacent_found),
        "flag_single_nonadjacent": ctype == "Тип 3" and len(decisive_nonadj) == 1,
        "flag_removal_outside_derm": bool(dermsurg_tags) and not (found_tags & _DERM_DIAG_TAGS),
    }
