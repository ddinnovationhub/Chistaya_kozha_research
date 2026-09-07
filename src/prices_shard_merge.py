"""Слияние прайс-баз параллельных шардов в основную data/prices.db.

ЗАЧЕМ (заказчик, 2026-09-07: 15 шардов для прайсов). У прайс-каскада нет
платных квот — делить нечего, — поэтому шардов больше, чем у поиска, а
разбиение идёт не по строкам, а по СТАБИЛЬНОМУ ХЭШУ ДОМЕНА
(src.prices.shard_of): домен всегда попадает в один и тот же шард, шарды
не пересекаются, вежливость к сайту (1 запрос / 3 с) не страдает.

ПРАВИЛО СЛИЯНИЯ: из шардовой базы берутся ТОЛЬКО домены этого шарда
(по хэшу) и ТОЛЬКО с терминальным статусом. Домен со статусом «в работе»
шард не закончил — его версия в основной базе остаётся нетронутой, и
следующая волна его доберёт. Слияние идемпотентно: повторный запуск
переносит те же данные поверх самих себя.

Гвард «перепрогон не ухудшает» отработал внутри шарда (run_company);
сюда доезжает уже согласованный результат, поэтому price_rerun по
перенесённым доменам чистится и здесь — шард свою запись удалил, а в
основной базе она осталась бы висеть.
"""

import os
import sqlite3
import sys

from src.prices import shard_of

_SKIP = ("", "в работе")     # не терминальные статусы — домен не закончен


def merge_shard(main: sqlite3.Connection, shard_path: str,
                shard_no: int, shards: int) -> dict:
    """Одна шардовая прайс-база → основная. Возвращает счётчики."""
    main.execute("PRAGMA busy_timeout=30000")
    main.execute("ATTACH DATABASE ? AS s", (f"file:{shard_path}?mode=ro",))
    out = {"доменов перенесено": 0, "позиций": 0, "нав-записей": 0,
           "rerun очищено": 0}
    try:
        rows = main.execute(
            "SELECT domain, status FROM s.price_recipes").fetchall()
        doms = [d for d, st in rows
                if st not in _SKIP and shard_of(d, shards) == shard_no]
        for d in doms:
            # рецепт шарда — авторитет по своему домену
            main.execute("DELETE FROM price_recipes WHERE domain=?", (d,))
            main.execute("INSERT INTO price_recipes "
                         "SELECT * FROM s.price_recipes WHERE domain=?", (d,))
            # позиции: полная замена — внутри шарда _save_items уже удалял
            # прежние, здесь то же правило для основной базы
            main.execute("DELETE FROM price_items WHERE domain=?", (d,))
            out["позиций"] += main.execute(
                "INSERT INTO price_items (inn, domain, url, section, code, "
                "name_raw, price_raw, price_value, currency, checked_at) "
                "SELECT inn, domain, url, section, code, name_raw, "
                "price_raw, price_value, currency, checked_at "
                "FROM s.price_items WHERE domain=?", (d,)).rowcount
            # журнал навигатора — маршрут этого прогона вместо прежнего
            main.execute("DELETE FROM price_nav_log WHERE domain=?", (d,))
            out["нав-записей"] += main.execute(
                "INSERT INTO price_nav_log SELECT * FROM s.price_nav_log "
                "WHERE domain=?", (d,)).rowcount
            # домен закончен — снятый чекпойнт перепрогона больше не нужен
            out["rerun очищено"] += main.execute(
                "DELETE FROM price_rerun WHERE domain=?", (d,)).rowcount
        out["доменов перенесено"] = len(doms)
        main.commit()
    finally:
        main.execute("DETACH DATABASE s")
    return out


def merge_all(main_path: str, shards_spec: list[tuple[str, int, int]]) -> dict:
    """[(путь, номер шарда, всего шардов), …] → сводка слияния."""
    from src.prices import ensure_price_tables
    main = sqlite3.connect(main_path)
    ensure_price_tables(main)
    total = {}
    for path, no, n in shards_spec:
        if not os.path.exists(path):
            print(f"⚠ шард {no}: базы {path} нет — пропуск", flush=True)
            continue
        res = merge_shard(main, path, no, n)
        print(f"  шард {no}/{n} ({path}): {res}", flush=True)
        for k, v in res.items():
            total[k] = total.get(k, 0) + v
    main.execute("VACUUM")
    main.close()
    return total


if __name__ == "__main__":
    # python -m src.prices_shard_merge data/prices.db shards/p1/prices.db:1:15 …
    main_path = sys.argv[1] if len(sys.argv) > 1 else "data/prices.db"
    spec = []
    for arg in sys.argv[2:]:
        path, no, n = arg.rsplit(":", 2)
        spec.append((path, int(no), int(n)))
    print("СЛИЯНИЕ ПРАЙС-ШАРДОВ:", merge_all(main_path, spec))
