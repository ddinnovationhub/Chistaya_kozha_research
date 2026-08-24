"""Политика поиска (промпт исправления заказчика, 2026-08-26, пп.1-4).

Обязательные проверки перед каждой генерацией запросов:
- ни один тег с use_in_search=нет не попадает в список запросов (п.2);
- фразы-процедуры без уточнения профиля в поиск не идут (п.3);
- приоритет: профиль клиента → ядро дерм-контура → прочее (п.1);
- тег профиля клиента в контуре cosm_est = ошибка СБОРКИ (п.4).
"""

import copy
import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.query_gen import (  # noqa: E402
    DERM_CORE_CONTOURS, L6_KEY_TAGS, assert_profile_contours, generate_all,
    load_client_tags, tag_search_phrases,
)

ROOT = Path(__file__).resolve().parent.parent
SERVICES = yaml.safe_load((ROOT / "dictionaries/services.yaml").read_text(encoding="utf-8"))
NOSOLOGY = yaml.safe_load((ROOT / "dictionaries/nosology.yaml").read_text(encoding="utf-8"))
DISTRICTS = json.loads((ROOT / "data/city_districts.json").read_text(encoding="utf-8"))["Новосибирск"]
CLIENT = load_client_tags(ROOT / "data/client_profile.yaml")
QUERIES = generate_all("Новосибирск", SERVICES, NOSOLOGY, DISTRICTS, client_tags=CLIENT)
BY_TAG = {t["tag"]: t for t in SERVICES["tags"]}


def _query_tags(layers=(2, 6)):
    return {q["template_id"].split("-", 1)[1] for q in QUERIES if q["layer"] in layers}


def test_no_banned_tag_in_queries():
    """Тег с use_in_search=нет не попадает в запросы (обязателен перед каждой генерацией)."""
    banned = {t["tag"] for t in SERVICES["tags"] if t.get("use_in_search") == "нет"}
    assert banned, "в справочнике нет ни одного use_in_search=нет — поле потеряно"
    assert not (_query_tags() & banned)


def test_esthetic_markers_never_searched():
    """Эстетические маркеры (список заказчика, п.2) существуют только для распознавания."""
    for tag in ("botulinum_cosm", "contour_filler", "biorevitalization",
                "hardware_rejuvenation", "mesotherapy_face", "epilation", "peeling",
                "face_cleaning", "laser_resurfacing", "face_massage",
                "carboxytherapy_derm", "carboxytherapy_scalp"):
        assert BY_TAG[tag].get("use_in_search") == "нет", tag
        assert tag not in _query_tags(), tag


def test_generic_procedure_phrases_absent():
    """Процедуры вне дерм-профиля без уточнения «кожи» в поиск не идут (п.3)."""
    banned_texts = [
        "ботокс", "ботулинотерапия", "биоревитализация", "аппаратное омоложение",
        "карбокситерапия", "биопсия щипковая", "панч-биопсия",
        "гистологическое исследование новосибирск",
        "гистологическое исследование биопсийного материала",
        "иммуногистохимическое", "цитологическое исследование пунктата",
        "эксцизионная биопсия новосибирск",
        "анализ на социально-значимые", "анализ волос на микроэлементы",
        "извлечение клеща", "лечение иппп",
        "хирургическое удаление новообразований новосибирск",
    ]
    for q in QUERIES:
        low = q["text"].lower()
        for b in banned_texts:
            assert b not in low, f"{q['query_id']}: «{q['text']}» содержит «{b}»"


def test_qualifier_tags_use_only_search_phrases():
    """qualifier=да → в поиск идут ТОЛЬКО явные search_phrases."""
    for t in SERVICES["tags"]:
        if t.get("search_needs_profile_qualifier") == "да" and t.get("use_in_search") == "да":
            assert t.get("search_phrases"), f"{t['tag']}: qualifier=да без search_phrases"
            assert set(tag_search_phrases(t)) <= set(t["search_phrases"])


def test_every_tag_has_policy_fields():
    """Каждый тег помечен обоими полями политики (п.2-3: «пройти по всему списку»)."""
    for t in SERVICES["tags"]:
        assert t.get("use_in_search") in ("да", "нет"), t["tag"]
        assert t.get("search_needs_profile_qualifier") in ("да", "нет"), t["tag"]


def test_l2_priority_groups_nondecreasing():
    """Приоритет L2 (п.1): профиль клиента → ядро дерм-контура → прочее."""
    groups = []
    for q in (q for q in QUERIES if q["layer"] == 2):
        tag = q["template_id"].split("-", 1)[1]
        groups.append(1 if tag in CLIENT
                      else 2 if BY_TAG[tag]["contour"] in DERM_CORE_CONTOURS else 3)
    assert groups == sorted(groups)
    assert groups[0] == 1   # список открывается профилем клиента


def test_l6_restricted_to_key_tags():
    """L6 — только ключевые услуги (п.5): 11 тегов × районы, не весь справочник."""
    l6_tags = _query_tags(layers=(6,))
    assert l6_tags <= set(L6_KEY_TAGS)
    l6_count = sum(1 for q in QUERIES if q["layer"] == 6)
    assert l6_count <= len(L6_KEY_TAGS) * len(DISTRICTS)


def test_client_profile_cosm_est_is_build_error():
    """Тег профиля клиента в контуре cosm_est → ошибка сборки (п.4),
    второй случай после laser_vascular не должен пройти молча."""
    services = copy.deepcopy(SERVICES)
    for t in services["tags"]:
        if t["tag"] == "carboxytherapy_derm":
            t["contour"] = "cosm_est"
    with pytest.raises(ValueError, match="cosm_est"):
        assert_profile_contours(services, CLIENT)


def test_unknown_profile_tag_is_build_error():
    """Переименование тега в справочнике без правки профиля — ошибка сборки."""
    with pytest.raises(ValueError, match="отсутствуют"):
        assert_profile_contours(SERVICES, CLIENT | {"carboxytherapy_face"})


def test_real_profile_passes():
    assert_profile_contours(SERVICES, CLIENT)
