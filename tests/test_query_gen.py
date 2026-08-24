"""Этап 4: детерминизм и контракт генератора запросов."""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.query_gen import (  # noqa: E402
    city_code, generate_all, generate_l5, L1_RUBRICS, L4_SPECIALTIES,
)

ROOT = Path(__file__).resolve().parent.parent
SERVICES = yaml.safe_load((ROOT / "dictionaries/services.yaml").read_text(encoding="utf-8"))
NOSOLOGY = yaml.safe_load((ROOT / "dictionaries/nosology.yaml").read_text(encoding="utf-8"))

QID_RE = re.compile(r"^L[1-6]-[a-zA-Z0-9._\-]+-[a-z0-9]+-[0-9a-f]{8}$")


def test_determinism_two_runs_identical():
    a = generate_all("Казань", SERVICES, NOSOLOGY)
    b = generate_all("Казань", SERVICES, NOSOLOGY)
    assert [q["query_id"] for q in a] == [q["query_id"] for q in b]
    assert a == b


def test_query_id_format():
    for q in generate_all("Нижний Новгород", SERVICES, NOSOLOGY):
        assert QID_RE.match(q["query_id"]), q["query_id"]


def test_city_code_translit():
    assert city_code("Казань") == "kazan"
    assert city_code("Нижний Новгород") == "nizhniynovgorod"


def test_no_l7_generated():
    layers = {q["layer"] for q in generate_all("Казань", SERVICES, NOSOLOGY)}
    assert 7 not in layers


def test_l1_l4_fixed_sizes():
    qs = generate_all("Казань", SERVICES, NOSOLOGY)
    assert sum(1 for q in qs if q["layer"] == 1) == len(L1_RUBRICS) == 12
    assert sum(1 for q in qs if q["layer"] == 4) == len(L4_SPECIALTIES) == 6


def test_l6_off_without_districts_on_with():
    base = generate_all("Казань", SERVICES, NOSOLOGY)
    assert not any(q["layer"] == 6 for q in base)
    with_d = generate_all("Казань", SERVICES, NOSOLOGY, ["Вахитовский район"])
    assert any(q["layer"] == 6 for q in with_d)


def test_l5_brand_wave():
    qs = generate_l5("Казань", ["Чистая Кожа", "Чистая Кожа"])  # дубль бренда
    assert len(qs) == 4  # 4 шаблона, бренд не задвоен
    assert all(q["layer"] == 5 for q in qs)


def test_freq_orders_within_l2():
    qs = [q for q in generate_all("Казань", SERVICES, NOSOLOGY) if q["layer"] == 2]
    freqs = [q["wordstat_freq"] for q in qs]
    known = [f for f in freqs if f is not None]
    assert known == sorted(known, reverse=True)  # известные частоты убывают
    if None in freqs and known:
        assert freqs.index(None) >= len(known)  # None-хвост после частотных


def test_unique_ids_across_cities():
    a = {q["query_id"] for q in generate_all("Казань", SERVICES, NOSOLOGY)}
    b = {q["query_id"] for q in generate_all("Самара", SERVICES, NOSOLOGY)}
    assert not (a & b)  # города не пересекаются по query_id
