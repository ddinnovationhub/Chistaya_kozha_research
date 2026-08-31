"""Фильтр серой зоны (заказчик, 2026-08-31: «половина строк — сайты на
ручную, я типа должен всё проверить?»). Карточка карт отдаёт любой сайт по
адресу здания; на ручную должен уходить только медицинский сайт."""

from src import test40
from src.site_finder import med_site_signal

CLINIC = """<html><body>
<h1>Клиника «Аура»</h1><a href="/zapis">Запись на приём</a>
<p>Наши врачи ведут приём пациентов. Дерматолог, косметолог.</p>
<p>Лицензия на осуществление медицинской деятельности № Л041-...</p>
</body></html>"""

FENCE = """<html><body><h1>Заборы под ключ в Краснодаре</h1>
<p>Профнастил, сетка-рабица, евроштакетник, монтаж и доставка по городу и
краю. Работаем с 2008 года, собственное производство секций и столбов.
Замер бесплатно, выезд мастера в день обращения, гарантия на монтаж три
года. Рассчитаем смету по вашим размерам, вывезем мусор после установки.
Оплата наличными и по карте, для организаций — безналичный расчёт и полный
пакет документов. Наш врач по заборам — шутка, конечно.</p>
</body></html>"""


def test_med_signal_detects_clinic():
    sig = med_site_signal([CLINIC])
    assert "клиника" in sig and "приём врача" in sig
    assert len(sig) >= 2


def test_med_signal_rejects_non_medical():
    assert len(med_site_signal([FENCE])) < 2


def test_gray_zone_filters_out_non_medical(monkeypatch):
    monkeypatch.setattr("src.site_finder.flexible_contact_texts",
                        lambda d, **k: [FENCE])
    assert test40.gray_zone_verdict("zabor-krd.ru", "ВНР, ООО МК") is None


def test_gray_zone_keeps_medical_with_name_match(monkeypatch):
    monkeypatch.setattr("src.site_finder.flexible_contact_texts",
                        lambda d, **k: [CLINIC])
    verdict = test40.gray_zone_verdict("med-aura.ru", "АУРА, ООО")
    assert verdict is not None
    assert verdict[0] == "вероятно наш сайт"


def test_gray_zone_marks_brand_site_when_name_absent(monkeypatch):
    monkeypatch.setattr("src.site_finder.flexible_contact_texts",
                        lambda d, **k: [CLINIC])
    verdict = test40.gray_zone_verdict("helix.expert", "БИОМЕД ПЛЮС, ООО")
    assert verdict is not None
    assert verdict[0] == "сайт сети/бренда"


def test_thin_site_is_not_called_non_medical(monkeypatch):
    """invitro.ru отдал 3.8 КБ JS-оболочки: «не прочитали» ≠ «не медицинский»
    (ЖЁСТКИЕ ОГРАНИЧЕНИЯ: нет страницы ≠ нет факта)."""
    monkeypatch.setattr("src.site_finder.flexible_contact_texts",
                        lambda d, **k: ["<html><body>загрузка…</body></html>"])
    monkeypatch.setattr("src.fetch_cascade._level1_jina", lambda u, **k: (None, None))
    monkeypatch.setattr("src.fetch_cascade._level3_headless", lambda u, **k: (None, None))
    verdict = test40.gray_zone_verdict("invitro.ru", "БИОСФЕРА, ООО")
    assert verdict is not None
    assert verdict[0] == "сайт не прочитан"


def test_dead_domain_gives_no_manual_work(monkeypatch):
    monkeypatch.setattr("src.site_finder.flexible_contact_texts",
                        lambda d, **k: [])
    assert test40.gray_zone_verdict("nowhere.ru", "АЛВИС, ООО") is None
