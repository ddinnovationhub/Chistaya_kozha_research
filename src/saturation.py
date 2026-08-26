"""[ВЫВЕДЕН ИЗ ФЛОУ — promt_spark_krug, 2026-08-25] Вход теперь — выборка СПАРК (src/spark_import.py), слепой discovery отключён. Код сохранён на случай возврата подхода.

Критерий насыщения разведки по городу (уточнён заказчиком 2026-08-24).

Правила (config/thresholds.yaml, секция saturation):
- при списке < min_list_len (200) критерий НЕ применяется — идём до конца списка;
- окно = max(window_min=50, window_share=25% длины списка);
- срабатывание не раньше earliest_trigger_share=60% списка;
- стоп, когда в окне < new_candidates_threshold (3) новых уникальных кандидатов.

Факт срабатывания и номер запроса записываются вызывающей стороной; критерий,
не сработавший к концу списка, означает «выборка не насыщена» в отчёте.
"""

import pathlib

import yaml

_THRESHOLDS = pathlib.Path("config/thresholds.yaml")


def load_params(path: pathlib.Path = _THRESHOLDS) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["saturation"]


def check_saturation(new_per_query: list[int], total_list_len: int,
                     params: dict | None = None) -> dict:
    """new_per_query — число новых уникальных кандидатов после дедупликации,
    по каждому УЖЕ выполненному запросу, в порядке выполнения.
    Возвращает {stopped, reason, at_query (1-based) | None, window}."""
    p = params or load_params()
    executed = len(new_per_query)

    if total_list_len < p["min_list_len"]:
        return {"stopped": False, "at_query": None, "window": None,
                "reason": f"список {total_list_len} < {p['min_list_len']} — критерий не применяется"}

    window = max(p["window_min"], int(p["window_share"] * total_list_len))
    earliest = int(p["earliest_trigger_share"] * total_list_len)

    if executed < max(window, earliest):
        return {"stopped": False, "at_query": None, "window": window,
                "reason": f"выполнено {executed}: рано (окно {window}, порог с {earliest}-го запроса)"}

    recent_new = sum(new_per_query[-window:])
    if recent_new < p["new_candidates_threshold"]:
        return {"stopped": True, "at_query": executed, "window": window,
                "reason": (f"последние {window} запросов дали {recent_new} новых "
                           f"(< {p['new_candidates_threshold']}) — насыщение")}
    return {"stopped": False, "at_query": None, "window": window,
            "reason": f"в окне {window} ещё {recent_new} новых — продолжаем"}
