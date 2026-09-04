"""Слияние баз параллельных шардов в основную data/osint.db.

ЗАЧЕМ (заказчик, 2026-09-04: «надо плодить actions и раздавать им задания
на параллельный анализ различного набора строк»). Пять прогонов идут
одновременно, каждый со СВОИМ диапазоном строк (SHARD_FROM/SHARD_TO) и
своей копией базы; писать в один файл они не могут — сегодняшний разбор
прайсов показал, чем это кончается («database is locked»). Поэтому каждый
шард отдаёт свою базу артефактом, а этот модуль сливает их в основную.

ПРАВИЛО СЛИЯНИЯ: из шардовой базы берутся ТОЛЬКО строки её диапазона и
только то, что в них добыто. Чужие строки шард видел в стартовой копии, но
не трогал — их версия в основной базе новее, и она побеждает. Так слияние
остаётся идемпотентным: повторный запуск ничего не портит.
"""

import os
import sqlite3
import sys

# что переносится из шарда: (таблица, ключ, «привязано к ИНН строки»)
_COPY_TABLES = [
    ("t40_positions", "inn"),
    ("t40_page_texts", "inn"),
    ("llm_judgments", "inn"),
    ("fetch_attempts", "inn"),
]

# поля результата работы конвейера (всё, что шард мог добыть по своей строке)
_RESULT_COLS = [
    "found_site", "site_source", "grade", "grade_evidence", "search_attempts",
    "search_status", "search_candidates", "fetch_status", "fetch_level",
    "pages_seen", "med_judgment", "med_basis", "mgmt_network",
    "profile_judgment", "profile_matches_n", "profile_matches",
    "positions_seen", "site_specialties", "passport", "checked_at",
    "map_check", "fetch_retries",
]


def merge_shard(main: sqlite3.Connection, shard_path: str,
                row_from: int, row_to: int) -> dict:
    """Одна шардовая база → основная. Возвращает счётчики перенесённого."""
    main.execute("PRAGMA busy_timeout=30000")
    main.execute("ATTACH DATABASE ? AS s", (f"file:{shard_path}?mode=ro",))
    out = {"строк обновлено": 0}
    try:
        # схемы основной и шардовой базы могут отличаться на миграцию
        # (fetch_retries добавляется автопилотом) — берём общие колонки
        def _cols(scope):
            return {r[1] for r in main.execute(
                f"PRAGMA {scope}table_info(t40_companies)")}
        have = _cols("") & _cols("s.")
        use = [c for c in _RESULT_COLS if c in have]
        cols = ", ".join(use)
        setter = ", ".join(f"{c}=s2.{c}" for c in use)
        # строки диапазона: результат шарда накатывается на основную базу
        out["строк обновлено"] = main.execute(
            f"UPDATE t40_companies AS m SET {setter} FROM ("
            f"  SELECT inn, {cols} FROM s.t40_companies"
            f"  WHERE row_no BETWEEN ? AND ?) AS s2 "
            f"WHERE m.inn = s2.inn", (row_from, row_to)).rowcount
        # строки, которых в основной базе ещё нет (шард импортировал новые)
        main.execute(
            "INSERT OR IGNORE INTO t40_companies "
            "SELECT * FROM s.t40_companies WHERE row_no BETWEEN ? AND ?",
            (row_from, row_to))
        # производные таблицы — только по ИНН строк этого диапазона
        existing = {r[0] for r in main.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table, key in _COPY_TABLES:
            if table not in existing:
                # таблицы может не быть в основной базе (её создаёт свой
                # модуль при первом запуске) — переносим вместе со схемой
                ddl = main.execute("SELECT sql FROM s.sqlite_master "
                                   "WHERE type='table' AND name=?",
                                   (table,)).fetchone()
                if not ddl:
                    out[table] = "нет в шарде"
                    continue
                main.execute(ddl[0])
            try:
                n = main.execute(
                    f"INSERT OR IGNORE INTO {table} SELECT * FROM s.{table} "
                    f"WHERE {key} IN (SELECT inn FROM s.t40_companies "
                    f"                WHERE row_no BETWEEN ? AND ?)",
                    (row_from, row_to)).rowcount
                out[table] = n
            except sqlite3.OperationalError as e:
                out[table] = f"пропущено ({e})"
        # реестр лицензий и журнал запросов — общие, добираем недостающее
        for table in ("rzn_licenses", "rzn_checked", "queries", "candidates"):
            try:
                main.execute(f"INSERT OR IGNORE INTO {table} "
                             f"SELECT * FROM s.{table}")
            except sqlite3.OperationalError:
                pass
        # РАСХОД КВОТ СУММИРУЕТСЯ, а не берётся от одного шарда (2026-09-04,
        # разбор первого прогона: у каждого шарда своя копия счётчика, и без
        # суммирования основная база видела расход лишь одного из пяти —
        # следующий прогон считал бы квоту почти нетронутой и пробил бы
        # суточный лимит ключа).
        try:
            for service, day, used in main.execute(
                    "SELECT service, day, used FROM s.api_quota").fetchall():
                main.execute("INSERT OR IGNORE INTO api_quota VALUES (?,?,0)",
                             (service, day))
                main.execute("UPDATE api_quota SET used=used+? "
                             "WHERE service=? AND day=?", (used, service, day))
            out["квоты просуммированы"] = 1
        except sqlite3.OperationalError:
            pass
        main.commit()
    finally:
        main.execute("DETACH DATABASE s")
    return out


def merge_all(main_path: str, shards: list[tuple[str, int, int]]) -> dict:
    """[(путь, row_from, row_to), …] → сводка слияния."""
    main = sqlite3.connect(main_path)
    total = {}
    for path, lo, hi in shards:
        if not os.path.exists(path):
            print(f"⚠ шард {path} не найден — пропуск", flush=True)
            continue
        res = merge_shard(main, path, lo, hi)
        print(f"  шард {lo}-{hi} ({path}): {res}", flush=True)
        for k, v in res.items():
            if isinstance(v, int):
                total[k] = total.get(k, 0) + v
    main.execute("VACUUM")
    main.close()
    return total


if __name__ == "__main__":
    # python -m src.shard_merge data/osint.db shard1.db:1:467 shard2.db:468:935
    main_path = sys.argv[1] if len(sys.argv) > 1 else "data/osint.db"
    spec = []
    for arg in sys.argv[2:]:
        path, lo, hi = arg.rsplit(":", 2)
        spec.append((path, int(lo), int(hi)))
    print("СЛИЯНИЕ ШАРДОВ:", merge_all(main_path, spec))
