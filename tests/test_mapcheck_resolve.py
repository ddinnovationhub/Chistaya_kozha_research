"""Авторазрешение расхождений даблчека (заказчик, 2026-09-02: в колонке Q
у 265 строк стояло «РАСХОЖДЕНИЕ … — на ручную»; «перепроверять 500+ строк
я не буду»). Домен карточки идёт через ту же лестницу; человеку — ничего."""

import sqlite3

from src import test40


def _db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t40_companies (row_no INTEGER, inn TEXT PRIMARY KEY, "
                "name TEXT, city TEXT, found_site TEXT, map_check TEXT)")
    con.execute("CREATE TABLE rzn_licenses (inn TEXT, is_med INTEGER, raw_gz BLOB, "
                "number TEXT, specialties TEXT)")
    return con


def _common(monkeypatch, ladder_result):
    monkeypatch.setattr("src.rzn_licenses.license_addresses", lambda db, inn: [])
    monkeypatch.setattr("src.rzn_licenses.license_numbers", lambda db, inn: [])
    monkeypatch.setattr(test40, "_check_candidates_flex",
                        lambda inn, name, city, cands, **k: ladder_result)
    monkeypatch.setattr("src.quota.status", lambda s: (0, 1000))


def test_backlog_discrepancy_resolved_without_manual(monkeypatch):
    db = _db()
    db.execute("INSERT INTO t40_companies VALUES (1,'1','АУРА, ООО','Челябинск',"
               "'med-aura.ru','РАСХОЖДЕНИЕ: в карточке zabor-krd.ru — на ручную')")
    _common(monkeypatch, None)                 # карточка лестницу не прошла
    monkeypatch.setattr("src.map_candidates.yandex_doublecheck",
                        lambda *a: "совпадает с карточкой")
    stats = test40.map_doublecheck(db, budget_sec=60)
    mc, site = db.execute("SELECT map_check, found_site FROM t40_companies").fetchone()
    assert site == "med-aura.ru"                            # H не тронута
    assert mc.startswith("РАСХОЖДЕНИЕ разрешено")
    assert "не сайт компании" in mc and "на ручную" not in mc
    assert stats["расхождение: карточка не сайт компании"] == 1


def test_new_discrepancy_where_card_domain_is_also_confirmed(monkeypatch):
    db = _db()
    db.execute("INSERT INTO t40_companies VALUES (1,'1','А2МЕД, ООО','Самара',"
               "'samara.a2med.ru',NULL)")
    _common(monkeypatch, ("a2med.com", "подтверждён ИНН", "ИНН на /rekvizity"))
    monkeypatch.setattr("src.map_candidates.yandex_doublecheck",
                        lambda *a: "РАСХОЖДЕНИЕ: в карточке a2med.com")
    monkeypatch.setattr(test40.time, "sleep", lambda s: None)
    stats = test40.map_doublecheck(db, budget_sec=60)
    mc = db.execute("SELECT map_check FROM t40_companies").fetchone()[0]
    assert "второй домен компании" in mc and "на ручную" not in mc
    assert "samara.a2med.ru" in mc                          # наш сайт остаётся
    assert stats["расхождение: второй домен компании"] == 1


def test_no_manual_marker_anywhere_in_map_verdicts():
    from src.map_candidates import yandex_doublecheck
    import src.map_candidates as mc
    mc.yandex_map_urls = lambda name, city, n=5: ["https://other.ru"]
    assert "на ручную" not in yandex_doublecheck("X", "Y", "our.ru")
