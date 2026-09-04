"""Параллельные шарды (заказчик, 2026-09-04: «плодить actions и раздавать
им задания на различный набор строк»): диапазон строк и слияние баз."""

import os
import sqlite3

import pytest

from src import shard_merge
from src.test40 import ensure_t40_tables, shard_clause


@pytest.fixture
def db(tmp_path):
    con = sqlite3.connect(str(tmp_path / "main.db"))
    ensure_t40_tables(con)
    return con


def _row(con, row_no, inn, **kw):
    cols = ", ".join(["row_no", "inn", "name"] + list(kw))
    q = ", ".join("?" * (3 + len(kw)))
    con.execute(f"INSERT INTO t40_companies ({cols}) VALUES ({q})",
                (row_no, inn, f"К{row_no}", *kw.values()))


def test_shard_clause_reads_environment(monkeypatch):
    monkeypatch.delenv("SHARD_FROM", raising=False)
    monkeypatch.delenv("SHARD_TO", raising=False)
    assert shard_clause() == ""            # без шардов поведение прежнее
    monkeypatch.setenv("SHARD_FROM", "468")
    monkeypatch.setenv("SHARD_TO", "935")
    assert shard_clause() == " AND row_no BETWEEN 468 AND 935"
    assert shard_clause("c") == " AND c.row_no BETWEEN 468 AND 935"


def test_shard_clause_rejects_injection(monkeypatch):
    """Диапазон приходит из воркфлоу — приводится к int, а не подставляется."""
    monkeypatch.setenv("SHARD_FROM", "1; DROP TABLE t40_companies")
    monkeypatch.setenv("SHARD_TO", "5")
    with pytest.raises(ValueError):
        shard_clause()


def test_merge_takes_only_own_range(db, tmp_path, monkeypatch):
    """Шард переносит результат ТОЛЬКО по своим строкам: чужие он видел в
    стартовой копии, но не трогал — версия основной базы новее и побеждает."""
    _row(db, 1, "a")                                  # чужая для шарда 2
    _row(db, 500, "b")                                # своя
    db.execute("UPDATE t40_companies SET found_site='свежий.ru' WHERE inn='a'")
    db.commit()

    sp = str(tmp_path / "shard2.db")
    sh = sqlite3.connect(sp)
    ensure_t40_tables(sh)
    _row(sh, 1, "a", found_site="устаревший.ru")      # копия ДО работы
    _row(sh, 500, "b", found_site="найден.ru", search_status="найден",
         passport="паспорт")
    sh.commit()
    sh.close()

    res = shard_merge.merge_shard(db, sp, 468, 935)
    assert res["строк обновлено"] == 1
    got = dict(db.execute("SELECT inn, found_site FROM t40_companies"))
    assert got["b"] == "найден.ru"        # своя строка перенесена
    assert got["a"] == "свежий.ru"        # чужая НЕ затёрта устаревшей копией


def test_merge_is_idempotent(db, tmp_path):
    _row(db, 500, "b")
    db.commit()
    sp = str(tmp_path / "s.db")
    sh = sqlite3.connect(sp)
    ensure_t40_tables(sh)
    _row(sh, 500, "b", found_site="x.ru", search_status="найден")
    sh.execute("CREATE TABLE llm_judgments (inn TEXT, provider TEXT, "
               "judgment TEXT, profile TEXT, basis TEXT, judged_at TEXT, "
               "PRIMARY KEY (inn, provider))")
    sh.execute("INSERT INTO llm_judgments VALUES ('b','groq','мед','','','')")
    sh.commit(); sh.close()

    shard_merge.merge_shard(db, sp, 468, 935)
    shard_merge.merge_shard(db, sp, 468, 935)          # повтор
    assert db.execute("SELECT COUNT(*) FROM llm_judgments").fetchone()[0] == 1
    assert db.execute("SELECT found_site FROM t40_companies "
                      "WHERE inn='b'").fetchone()[0] == "x.ru"


def test_merge_adds_rows_absent_in_main(db, tmp_path):
    """Шард импортировал новые строки файла — они появляются в основной."""
    sp = str(tmp_path / "s.db")
    sh = sqlite3.connect(sp)
    ensure_t40_tables(sh)
    _row(sh, 1900, "new1", found_site="n.ru")
    sh.commit(); sh.close()
    shard_merge.merge_shard(db, sp, 1871, 2337)
    assert db.execute("SELECT COUNT(*) FROM t40_companies "
                      "WHERE inn='new1'").fetchone()[0] == 1


def test_quota_is_divided_between_shards(monkeypatch):
    """Квота Геопоиска суточная НА КЛЮЧ, а база у каждого шарда своя: без
    деления пять шардов независимо потратили бы по 1000 при лимите 1000."""
    from src import quota
    monkeypatch.delenv("QUOTA_SHARE", raising=False)
    assert quota.limit_for("yandex_geosearch") == 1000
    monkeypatch.setenv("QUOTA_SHARE", "5")
    assert quota.limit_for("yandex_geosearch") == 200
    assert quota.limit_for("dgis_places") == 200
    monkeypatch.setenv("QUOTA_SHARE", "мусор")     # не роняем прогон
    assert quota.limit_for("yandex_geosearch") == 1000


def test_import_takes_only_shard_range(tmp_path, monkeypatch):
    """Шард импортирует свой кусок файла, а не первые N строк."""
    import openpyxl

    from src.test40 import import_t40
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["№", "Название", "ОГРН", "Сайты", "ИНН", "Регион",
               "Отрасль", "Маркер", "Выручка"])
    for i in range(1, 21):
        ws.append([i, f"Клиника {i}", "1" * 13, None, f"77000000{i:02d}",
                   "Новосибирская область", "мед", "1", "100"])
    f = tmp_path / "base.xlsx"
    wb.save(f)

    con = sqlite3.connect(":memory:")
    monkeypatch.setenv("SHARD_FROM", "11")
    monkeypatch.setenv("SHARD_TO", "15")
    import_t40(str(f), con, 20)
    rows = sorted(r[0] for r in con.execute("SELECT row_no FROM t40_companies"))
    assert rows == [11, 12, 13, 14, 15]
