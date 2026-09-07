"""Шардирование прайс-каскада по доменам (заказчик, 2026-09-07: 15 шардов).

У прайсов нет платных квот — делить нечего, поэтому шардов больше, чем у
поиска, а разбиение идёт по стабильному хэшу домена, не по строкам."""

import sqlite3

from src.prices import shard_of


def test_shard_of_is_stable_and_covers_all_shards():
    """Хэш детерминирован (не hash() с солью), www не влияет, все шарды
    получают работу на реалистичном числе доменов."""
    assert shard_of("agk24.ru", 15) == shard_of("agk24.ru", 15)
    assert shard_of("www.agk24.ru", 15) == shard_of("AGK24.RU", 15)
    doms = [f"clinic-{i}.ru" for i in range(940)]
    buckets = {shard_of(d, 15) for d in doms}
    assert buckets == set(range(1, 16))         # ни одного пустого шарда
    sizes = [sum(1 for d in doms if shard_of(d, 15) == b) for b in buckets]
    assert max(sizes) < 2.2 * min(sizes)        # разбиение не перекошено


def test_run_batch_takes_only_own_shard(monkeypatch):
    """Шард берёт только свои домены; лимит применяется ПОСЛЕ фильтра —
    иначе SQL-LIMIT отдал бы шарду первые строки базы, из которых свои
    лишь каждая пятнадцатая."""
    from src import prices
    db = sqlite3.connect(":memory:")
    prices.ensure_price_tables(db)
    db.execute("CREATE TABLE t40_companies (inn TEXT, found_site TEXT, "
               "row_no INTEGER, name TEXT)")
    db.execute("CREATE TABLE rzn_licenses (inn TEXT, is_med INTEGER, "
               "specialties TEXT)")
    prices.T40, prices.RZN = "t40_companies", "rzn_licenses"
    doms = [f"c{i:03d}.ru" for i in range(60)]
    for i, d in enumerate(doms):
        db.execute("INSERT INTO t40_companies VALUES (?,?,?,?)",
                   (str(i), d, i + 1, "x"))
        db.execute("INSERT INTO rzn_licenses VALUES (?,1,'дерматовенерологии')",
                   (str(i),))
    db.commit()
    monkeypatch.setenv("PRICE_SHARDS", "5")
    monkeypatch.setenv("PRICE_SHARD", "3")
    seen = []
    monkeypatch.setattr(prices, "run_company",
                        lambda db, inn, dom: seen.append(dom) or
                        {"domain": dom, "status": "прайс извлечён",
                         "items": 1, "level": "P3"})
    prices.run_batch(db, limit=999)
    assert seen                                   # шарду досталась работа
    assert all(prices.shard_of(d, 5) == 3 for d in seen)
    mine = [d for d in doms if prices.shard_of(d, 5) == 3]
    assert seen == mine                           # все свои, ничего чужого


def test_no_shard_env_means_everything(monkeypatch):
    """Без переменных окружения поведение прежнее — берётся вся очередь."""
    from src import prices
    monkeypatch.delenv("PRICE_SHARDS", raising=False)
    monkeypatch.delenv("PRICE_SHARD", raising=False)
    assert prices._shard_env() == (1, 1)
    monkeypatch.setenv("PRICE_SHARDS", "15")
    monkeypatch.setenv("PRICE_SHARD", "99")       # мусор → без шардирования
    assert prices._shard_env() == (1, 1)


def _mk_recipe(db, dom, status, items=0):
    db.execute("INSERT INTO price_recipes VALUES (?,?,?,?,'','[]','[]',1,?,"
               "'','2026-09-07')", (dom, "1", "P3", status, items))


def test_merge_takes_only_terminal_own_domains(tmp_path):
    """Слияние переносит ТОЛЬКО домены своего шарда с терминальным
    статусом: чужие и «в работе» не трогаются, price_rerun перенесённых
    чистится, позиции заменяются полностью."""
    from src import prices
    from src.prices_shard_merge import merge_shard

    shards = 5
    # домены с известной принадлежностью
    own = next(f"own{i}.ru" for i in range(99)
               if prices.shard_of(f"own{i}.ru", shards) == 2)
    alien = next(f"al{i}.ru" for i in range(99)
                 if prices.shard_of(f"al{i}.ru", shards) != 2)
    busy = next(f"b{i}.ru" for i in range(99)
                if prices.shard_of(f"b{i}.ru", shards) == 2 and f"b{i}.ru" != own)

    sp = str(tmp_path / "shard.db")
    sdb = sqlite3.connect(sp)
    prices.ensure_price_tables(sdb)
    _mk_recipe(sdb, own, "прайс извлечён", 2)
    for i in range(2):
        sdb.execute("INSERT INTO price_items (inn, domain, url, section, code,"
                    " name_raw, price_raw, price_value, currency, checked_at)"
                    " VALUES ('1',?,?,'','',?,'100 ₽',100.0,'RUB','d')",
                    (own, f"https://{own}/p{i}", f"Услуга {i}"))
    _mk_recipe(sdb, alien, "прайс извлечён", 1)   # чужой шард — не переносить
    _mk_recipe(sdb, busy, "в работе")             # не закончен — не переносить
    sdb.commit(); sdb.close()

    main = sqlite3.connect(":memory:")
    prices.ensure_price_tables(main)
    _mk_recipe(main, own, "в работе", 1)          # прежнее состояние
    main.execute("INSERT INTO price_items (inn, domain, url, section, code,"
                 " name_raw, price_raw, price_value, currency, checked_at)"
                 " VALUES ('1',?,?,'','','Старая','50 ₽',50.0,'RUB','d')",
                 (own, f"https://{own}/old"))
    main.execute("INSERT INTO price_rerun VALUES (?,'прайс извлечён','P3',1,"
                 "'','2026-09-07')", (own,))
    res = merge_shard(main, sp, 2, shards)

    assert res["доменов перенесено"] == 1
    st = dict(main.execute("SELECT domain, status FROM price_recipes"))
    assert st[own] == "прайс извлечён"
    assert alien not in st                        # чужой не пришёл
    names = [r[0] for r in main.execute(
        "SELECT name_raw FROM price_items WHERE domain=?", (own,))]
    assert sorted(names) == ["Услуга 0", "Услуга 1"]   # старьё заменено
    assert main.execute("SELECT COUNT(*) FROM price_rerun").fetchone()[0] == 0


def test_merge_is_idempotent(tmp_path):
    """Повторное слияние того же шарда не плодит дублей."""
    from src import prices
    from src.prices_shard_merge import merge_shard
    own = next(f"own{i}.ru" for i in range(99)
               if prices.shard_of(f"own{i}.ru", 3) == 1)
    sp = str(tmp_path / "s.db")
    sdb = sqlite3.connect(sp)
    prices.ensure_price_tables(sdb)
    _mk_recipe(sdb, own, "прайс извлечён", 1)
    sdb.execute("INSERT INTO price_items (inn, domain, url, section, code,"
                " name_raw, price_raw, price_value, currency, checked_at)"
                " VALUES ('1',?,'u','','','Приём','900 ₽',900.0,'RUB','d')",
                (own,))
    sdb.commit(); sdb.close()
    main = sqlite3.connect(":memory:")
    prices.ensure_price_tables(main)
    merge_shard(main, sp, 1, 3)
    merge_shard(main, sp, 1, 3)
    assert main.execute("SELECT COUNT(*) FROM price_recipes").fetchone()[0] == 1
    assert main.execute("SELECT COUNT(*) FROM price_items").fetchone()[0] == 1
