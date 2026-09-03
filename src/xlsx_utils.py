"""Санация значений для ячеек Excel — общая для ВСЕХ выгрузок проекта.

История: run 33327894175 (test-40) упал на управляющих символах в паспорте
diagnostrentgen.com — фикс _xl() добавлен только в test40. Run 33618854799
(prices) упал на том же классе бага: нулевой байт \x00 в названии
прайс-позиции («FIGHT TDA … Сыворотка»). Урок: санация живёт в одном месте
и применяется каждым экспортом; мусор заменяется на «·» и остаётся ВИДИМЫМ
на ручной проверке — файл собирается, данные не подчищаются молча."""


def xl(v):
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    if isinstance(v, str) and ILLEGAL_CHARACTERS_RE.search(v):
        return ILLEGAL_CHARACTERS_RE.sub("·", v)
    return v


def xl_row(row):
    """Санация целого кортежа/списка для ws.append(...)."""
    return [xl(v) for v in row]
