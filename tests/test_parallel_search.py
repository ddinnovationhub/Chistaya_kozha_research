"""Параллельный поиск и разрешение расхождений (заказчик, 2026-09-02:
«это очень долго; лимит 2000 минут Actions»). Проверяется, что при работе
в пуле потоков результаты пишутся корректно, исходов по-прежнему два,
отложенные по квоте строки остаются в очереди, шлюз Яндекса держит ≤1 rps."""

import base64
import sqlite3
import time

from src import test40


class _Resp:
    status_code = 200

    def __init__(self, urls):
        docs = "".join(f"<doc><url>https://{u}/</url><domain>{u}</domain>"
                       f"<title>t</title></doc>" for u in urls)
        self._raw = base64.b64encode(
            f"<yandexsearch><response><results><grouping>{docs}"
            f"</grouping></results></response></yandexsearch>".encode()).decode()

    def json(self):
        return {"rawData": self._raw}


def _db():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
    CREATE TABLE t40_companies (row_no INTEGER, inn TEXT PRIMARY KEY, name TEXT,
        city TEXT, found_site TEXT, grade TEXT, grade_evidence TEXT,
        site_source TEXT, search_attempts INTEGER, search_status TEXT,
        search_candidates TEXT, map_check TEXT);
    CREATE TABLE rzn_checked (inn TEXT, status TEXT);
    """)
    for i in range(1, 13):
        db.execute("INSERT INTO t40_companies (row_no, inn, name, city) VALUES (?,?,?,?)",
                   (i, str(i), f"КОМПАНИЯ{i}, ООО", "Казань"))
        db.execute("INSERT INTO rzn_checked VALUES (?, 'проверен')", (str(i),))
    return db


class _Budget:
    def __init__(self):
        self.calls = []

    def charge(self, service, n=1):
        self.calls.append(service)


def test_parallel_search_two_outcomes(monkeypatch):
    db = _db()
    monkeypatch.setattr("src.api_client.yandex_search_raw",
                        lambda q, n=10: _Resp([f"site{q[8:9]}.ru"]))
    monkeypatch.setattr("src.api_client.handle_api_response", lambda r, s: r)
    monkeypatch.setattr("src.keenable.keenable_search", lambda q, n=20: [])
    monkeypatch.setattr("src.map_candidates.map_candidates",
                        lambda name, city: {"urls": [], "brands": []})
    monkeypatch.setattr("src.rzn_licenses.license_addresses", lambda db, inn: [])
    monkeypatch.setattr("src.rzn_licenses.license_numbers", lambda db, inn: [])
    monkeypatch.setattr("src.budget.BudgetTracker", _Budget)
    monkeypatch.delenv("YANDEX_GEOSEARCH_API_KEY", raising=False)

    # чётные ИНН подтверждаются ИНН, нечётные — лестницу не проходят
    def fake_ladder(inn, name, city, cands, license_addrs=None, license_nums=None):
        time.sleep(0.05)                       # имитация обхода
        if int(inn) % 2 == 0 and cands:
            return cands[0], "подтверждён ИНН", "ИНН на странице"
        return None
    monkeypatch.setattr(test40, "_check_candidates_flex", fake_ladder)

    stats = test40.run_search(db, budget_sec=60, workers=4)
    assert stats["done"] == 12
    assert stats["found_inn"] == 6 and stats["not_found"] == 6
    rows = db.execute("SELECT inn, found_site, search_status FROM t40_companies "
                      "ORDER BY CAST(inn AS INTEGER)").fetchall()
    for inn, site, st in rows:
        if int(inn) % 2 == 0:
            assert site and st == "найден"
        else:
            assert site is None and st == "сайт не найден"
    # промежуточных статусов нет ни у кого
    assert not db.execute("SELECT COUNT(*) FROM t40_companies WHERE search_status "
                          "LIKE 'Требует%' OR search_status LIKE 'Ручная%'").fetchone()[0]


def test_deferred_rows_stay_in_queue_when_geo_quota_low(monkeypatch):
    db = _db()
    monkeypatch.setattr("src.api_client.yandex_search_raw", lambda q, n=10: _Resp([]))
    monkeypatch.setattr("src.api_client.handle_api_response", lambda r, s: r)
    monkeypatch.setattr("src.keenable.keenable_search", lambda q, n=20: [])
    monkeypatch.setattr("src.rzn_licenses.license_addresses", lambda db, inn: [])
    monkeypatch.setattr("src.rzn_licenses.license_numbers", lambda db, inn: [])
    monkeypatch.setattr("src.budget.BudgetTracker", _Budget)
    monkeypatch.setenv("YANDEX_GEOSEARCH_API_KEY", "k")
    monkeypatch.setattr("src.quota.status", lambda s: (990, 1000))   # у резерва
    monkeypatch.setattr(test40, "_check_candidates_flex", lambda *a, **k: None)
    stats = test40.run_search(db, budget_sec=60, workers=4)
    assert stats.get("deferred_quota", 0) >= 3
    assert stats["done"] == 0
    # ни одной строке не поставлено «сайт не найден» по неполному каскаду
    assert db.execute("SELECT COUNT(*) FROM t40_companies WHERE search_status IS NOT NULL"
                      ).fetchone()[0] == 0


def test_yandex_gate_spacing(monkeypatch):
    stamps = []
    monkeypatch.setattr("src.api_client.yandex_search_raw",
                        lambda q, n=10: stamps.append(time.time()))
    test40._yandex_last[0] = 0.0
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda i: test40._paced_yandex("q"), range(4)))
    stamps.sort()
    assert all(b - a >= 0.95 for a, b in zip(stamps, stamps[1:]))


def test_mapcheck_resolves_backlog_in_parallel(monkeypatch):
    db = _db()
    db.execute("UPDATE t40_companies SET found_site='ours'||inn||'.ru'")
    db.execute("UPDATE t40_companies SET map_check='РАСХОЖДЕНИЕ: в карточке card'||inn||'.ru — на ручную'")
    db.commit()
    monkeypatch.setattr("src.rzn_licenses.license_addresses", lambda db, inn: [])
    monkeypatch.setattr("src.rzn_licenses.license_numbers", lambda db, inn: [])
    monkeypatch.setattr("src.map_candidates.yandex_doublecheck",
                        lambda name, city, site: "совпадает с карточкой")

    def fake_ladder(inn, name, city, cands, license_addrs=None, license_nums=None):
        time.sleep(0.02)
        return (cands[0], "подтверждён ИНН", "x") if int(inn) % 3 == 0 else None
    monkeypatch.setattr(test40, "_check_candidates_flex", fake_ladder)
    stats = test40.map_doublecheck(db, budget_sec=60, workers=4)
    assert stats["расхождение: второй домен компании"] == 4
    assert stats["расхождение: карточка не сайт компании"] == 8
    assert db.execute("SELECT COUNT(*) FROM t40_companies WHERE map_check LIKE '%на ручную%'"
                      ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM t40_companies WHERE found_site LIKE 'ours%'"
                      ).fetchone()[0] == 12          # колонка H не тронута


def test_quota_defers_row_not_whole_search(monkeypatch):
    """Карты — запасной слой, и по факту базы они не нашли ни одного сайта
    из 1403. Раньше три отложенных строки подряд роняли весь этап поиска,
    унося с собой работающие веб-слои (разбор 2026-09-04)."""
    from src import test40

    db = _db()          # 12 строк, реестр по всем пройден

    # первые три строки требуют карт (квоты нет) → откладываются;
    # остальные находятся веб-слоем и ДОЛЖНЫ быть обработаны
    def fake_one(inn, name, city, addrs, nums, budget, geo_ok):
        idx = int(inn)
        if idx <= 3:
            return {"res": None, "src_label": "", "cands": [], "skipped": False,
                    "deferred": True, "spent": 0.0}
        return {"res": (f"c{idx}.ru", "подтверждён ИНН", "ИНН на сайте"),
                "src_label": "название+город", "cands": [f"c{idx}.ru"],
                "skipped": False, "deferred": False, "spent": 0.5}

    monkeypatch.setattr(test40, "_search_one", fake_one)
    monkeypatch.setattr("src.rzn_licenses.license_addresses", lambda db, inn: [])
    monkeypatch.setattr("src.rzn_licenses.license_numbers", lambda db, inn: [])
    monkeypatch.setattr("src.budget.BudgetTracker", _Budget)
    monkeypatch.setenv("YANDEX_GEOSEARCH_API_KEY", "x")
    stats = test40.run_search(db, budget_sec=60, workers=2)

    assert stats["deferred_quota"] >= 1          # отложенные честно посчитаны
    assert stats["done"] == 9                    # остальные девять обработаны
    found = db.execute("SELECT COUNT(*) FROM t40_companies "
                       "WHERE found_site IS NOT NULL").fetchone()[0]
    assert found == 9
    # отложенные остались в очереди, а не получили «сайт не найден»
    waiting = db.execute("SELECT COUNT(*) FROM t40_companies WHERE "
                         "found_site IS NULL AND search_status IS NULL"
                         ).fetchone()[0]
    assert waiting == 3
