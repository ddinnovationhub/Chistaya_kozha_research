"""Автопилот (заказчик, 2026-08-30): решение continue/stop, возврат упавших
строк с потолком повторов, гвард нулевого прогресса, потолок цепочки."""

import sqlite3

import pytest

from src import autopilot


@pytest.fixture
def db():
    con = sqlite3.connect(":memory:")
    con.execute("""CREATE TABLE t40_companies (
        row_no INTEGER, inn TEXT PRIMARY KEY, name TEXT,
        found_site TEXT, site_source TEXT, search_status TEXT,
        fetch_status TEXT, map_check TEXT, passport TEXT,
        fetch_level TEXT, pages_seen TEXT, med_judgment TEXT,
        med_basis TEXT, mgmt_network TEXT, profile_judgment TEXT,
        profile_matches_n INTEGER, profile_matches TEXT,
        positions_seen TEXT, site_specialties TEXT, checked_at TEXT)""")
    con.execute("""CREATE TABLE llm_judgments (
        inn TEXT, provider TEXT, judgment TEXT, profile TEXT,
        basis TEXT, judged_at TEXT)""")
    autopilot.ensure_tables(con)
    return con


def _row(db, no, inn, **kw):
    cols = ", ".join(["row_no", "inn"] + list(kw))
    q = ", ".join("?" * (2 + len(kw)))
    db.execute(f"INSERT INTO t40_companies ({cols}) VALUES ({q})",
               (no, inn, *kw.values()))


def _ok_budget(monkeypatch):
    monkeypatch.setattr(autopilot, "_budget_ok", lambda: (True, "бюджет ок"))


def test_prepare_resets_retry_rows_with_cap(db):
    _row(db, 1, "1", found_site="a.ru", fetch_status=autopilot.RETRY_STATUS)
    db.execute("UPDATE t40_companies SET fetch_retries=0 WHERE inn='1'")
    _row(db, 2, "2", found_site="b.ru", fetch_status=autopilot.RETRY_STATUS)
    db.execute(f"UPDATE t40_companies SET fetch_retries="
               f"{autopilot.RETRY_CAP} WHERE inn='2'")
    res = autopilot.prepare(db)
    assert res == {"возвращено в очередь": 1, "исчерпали повторы": 1,
                   "очищено остатков сброшенных сайтов": 0}
    s1, s2 = [db.execute("SELECT fetch_status FROM t40_companies "
                         "WHERE inn=?", (i,)).fetchone()[0] for i in "12"]
    assert s1 is None                                   # вернулась в очередь
    assert s2 == autopilot.FINAL_RETRY_STATUS           # честный финал


def test_prepare_wipes_orphans_of_reset_sites(db):
    """ИНВАРИАНТ (заказчик, 2026-09-03: «в колонке M специализация с сайтов,
    а сайт не найден»): у строки без found_site не бывает производных обхода —
    остатки от сайта, сброшенного лестницей, вычищаются каждый прогон."""
    _row(db, 1, "1", search_status="сайт не найден",
         site_specialties="дерматолог", positions_seen="приём дерматолога",
         passport="п", fetch_status="ok", med_judgment="медорганизация",
         map_check="совпадает")
    _row(db, 2, "2", found_site="a.ru", site_specialties="косметолог",
         passport="п2")
    db.execute("INSERT INTO llm_judgments VALUES ('1','groq','м','','','')")
    db.execute("INSERT INTO llm_judgments VALUES ('2','groq','м','','','')")
    res = autopilot.prepare(db)
    assert res["очищено остатков сброшенных сайтов"] == 1
    r1 = db.execute("SELECT site_specialties, positions_seen, passport, "
                    "fetch_status, med_judgment, map_check "
                    "FROM t40_companies WHERE inn='1'").fetchone()
    assert r1 == (None,) * 6
    # строка С сайтом не тронута, её суждения живы
    r2 = db.execute("SELECT site_specialties, passport FROM t40_companies "
                    "WHERE inn='2'").fetchone()
    assert r2 == ("косметолог", "п2")
    js = {r[0] for r in db.execute("SELECT inn FROM llm_judgments")}
    assert js == {"2"}
    # идемпотентность: второй прогон ничего не находит
    assert autopilot.prepare(db)["очищено остатков сброшенных сайтов"] == 0


def test_decide_stops_when_done(db, monkeypatch, capsys):
    _ok_budget(monkeypatch)
    _row(db, 1, "1", found_site="a.ru", search_status="найден",
         fetch_status="ok", map_check="совпадает", passport="p")
    db.execute("INSERT INTO llm_judgments VALUES ('1','groq','мед','','','')")
    assert autopilot.decide(db, 1, 3) == "stop"
    enabled = db.execute("SELECT enabled FROM autopilot_state").fetchone()[0]
    assert enabled == 0


def test_decide_continues_when_rows_remain(db, monkeypatch):
    _ok_budget(monkeypatch)
    _row(db, 1, "1")   # вся работа впереди
    assert autopilot.decide(db, 40, 0) == "continue"


def test_decide_zero_progress_guard(db, monkeypatch):
    _ok_budget(monkeypatch)
    _row(db, 1, "1")
    assert autopilot.decide(db, 40, 0) == "continue"    # снапшот записан
    # прогон "прошёл", но счётчики не сдвинулись → стоп (квота), крон дожмёт
    assert autopilot.decide(db, 40, 1) == "stop"
    # прогресс есть → продолжаем
    db.execute("UPDATE t40_companies SET found_site='a.ru' WHERE inn='1'")
    assert autopilot.decide(db, 40, 2) == "continue"


def test_decide_chain_cap(db, monkeypatch):
    _ok_budget(monkeypatch)
    _row(db, 1, "1")
    assert autopilot.decide(db, 40, autopilot.CHAIN_CAP) == "stop"


def test_decide_budget_ceiling(db, monkeypatch):
    monkeypatch.setattr(autopilot, "_budget_ok",
                        lambda: (False, "бюджет 4990 ₽ из 5000 ₽"))
    _row(db, 1, "1")
    assert autopilot.decide(db, 40, 0) == "stop"


def test_cron_restarts_unfinished_target(db, monkeypatch):
    _ok_budget(monkeypatch)
    _row(db, 1, "1")
    autopilot.decide(db, 1500, 0)                       # цель сохранена
    assert autopilot.cron(db) == "1500"
    # работа закрыта → крон выключает автопилот
    db.execute("UPDATE t40_companies SET found_site='a.ru', "
               "search_status='найден', fetch_status='ok', "
               "map_check='совпадает' WHERE inn='1'")
    db.execute("UPDATE t40_companies SET row_no=1500 WHERE inn='1'")
    assert autopilot.cron(db) == "stop"
    assert db.execute("SELECT enabled FROM autopilot_state").fetchone()[0] == 0


def test_cron_silent_when_never_configured(db):
    assert autopilot.cron(db) == "stop"
