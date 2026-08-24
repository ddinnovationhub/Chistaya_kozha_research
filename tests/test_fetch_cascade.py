"""Каскад доступа к сайтам (второй промпт исправления, 2026-08-26, пп.3-6)."""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.fetch_cascade as fc  # noqa: E402
from src.fetch_cascade import (  # noqa: E402
    ensure_fetch_tables, fetch_cascade, has_content_signals,
)
from src.mapper import build_formulation_index  # noqa: E402

FORM_INDEX = build_formulation_index()


def test_price_is_content_signal():
    assert has_content_signals("Удаление папилломы — 500 ₽", FORM_INDEX)


def test_dictionary_hit_is_content_signal():
    assert has_content_signals("Меню\nДерматоскопия\nКонтакты", FORM_INDEX)


def test_big_page_is_content():
    assert has_content_signals("х" * 4000, FORM_INDEX, min_bytes=3000)


def test_stub_page_is_suspicious_zero():
    """200 со заглушкой без цен/услуг/объёма — НЕ взято (формальный успех ≠ успех)."""
    assert not has_content_signals("Loading...", FORM_INDEX)
    assert not has_content_signals(None, FORM_INDEX)
    assert not has_content_signals("", FORM_INDEX)


def _run_cascade(monkeypatch, l1, l2, robots=True):
    db = sqlite3.connect(":memory:")
    ensure_fetch_tables(db)
    monkeypatch.setattr(fc, "robots_allows", lambda url: robots)
    monkeypatch.setattr(fc, "_level1_jina", lambda url: l1)
    monkeypatch.setattr(fc, "_level2_direct", lambda url: l2)
    monkeypatch.setattr(fc, "RATE_DELAY_SEC", 0)
    text, meta = fetch_cascade("https://x.ru", "x.ru", FORM_INDEX, db=db, max_level=2)
    return text, meta, db


def test_level1_takes_good_page(monkeypatch):
    good = ("Дерматоскопия — 1 200 ₽", "200", 25)
    text, meta, db = _run_cascade(monkeypatch, good, ("не должен вызываться", "0", 0))
    assert meta["level"] == 1 and text
    assert db.execute("SELECT COUNT(*) FROM fetch_attempts").fetchone()[0] == 1


def test_stub_escalates_to_level2(monkeypatch):
    """Уровень 2 применяется ТОЛЬКО к тому, что не взял уровень 1."""
    stub = ("Loading...", "200", 10)
    good = ("Приём дерматолога — 1 500 ₽", "200", 27)
    text, meta, db = _run_cascade(monkeypatch, stub, good)
    assert meta["level"] == 2 and text
    rows = list(db.execute("SELECT level, content_ok, note FROM fetch_attempts ORDER BY level"))
    assert rows[0][1] == 0 and "suspicious_zero" in rows[0][2]
    assert rows[1][1] == 1


def test_nothing_taken_returns_none_with_telemetry(monkeypatch):
    text, meta, db = _run_cascade(monkeypatch, (None, "403", 0), (None, "403", 0))
    assert text is None and meta["level"] is None
    assert meta["last_level"] == 2 and meta["last_status"] == "403"


def test_robots_disallow_blocks_all_levels(monkeypatch):
    """Юрист OSINT: запрет robots.txt → сбор не выполняется ни одним уровнем."""
    good = ("Дерматоскопия — 1 200 ₽", "200", 25)
    text, meta, db = _run_cascade(monkeypatch, good, good, robots=False)
    assert text is None and meta["blocked_by_robots"]
    row = db.execute("SELECT status FROM fetch_attempts").fetchone()
    assert row[0] == "robots_disallow"


def test_unreachable_share_threshold_in_config():
    import yaml
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent /
                          "config/thresholds.yaml").read_text(encoding="utf-8"))
    assert cfg["cascade"]["unreachable_share_stop"] == 0.15
    assert cfg["cascade"]["content_min_bytes"] > 0


def test_vocab_has_site_unreachable_status():
    """«Сайт недоступен» ≠ «Не найдено» — оба в словаре, не смешиваются."""
    import yaml
    vocab = yaml.safe_load((Path(__file__).resolve().parent.parent /
                            "dictionaries/vocab.yaml").read_text(encoding="utf-8"))
    assert "Сайт недоступен" in vocab["field_status"]
    assert "Не найдено" in vocab["field_status"]
    assert "Сайт недоступен" in vocab["service_presence"]
