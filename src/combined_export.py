"""СВОДНЫЙ ФАЙЛ (заказчик, 2026-09-03: «сведи все в один файл»): один Excel
со всеми листами двух конвейеров — сайты/лицензии/паспорта/судьи из
data/osint.db и прайсы из data/prices.db.

Листы: ИТОГ · Лицензии_РЗН · Адреса_точек · Паспорта · Судьи_нейронки ·
Полные_колонки · Прайсы_рецепты · Позиции · Выбросы_на_проверку.

Собирается в конце ОБОИХ воркфлоу (каждый прогон обновляет файл по
актуальному состоянию обеих баз из checkout) и локально:
    python -m src.combined_export data/Выборка_компаний_V2.xlsx
"""

import datetime
import sqlite3


def export_combined(src_path: str, path: str | None = None) -> str:
    import openpyxl

    from src.prices import export_prices, open_dbs
    from src.test40 import export_t40
    wb = openpyxl.Workbook()
    db = sqlite3.connect("data/osint.db")
    export_t40(db, src_path, wb=wb)
    pdb = open_dbs()
    export_prices(pdb, wb=wb)
    path = path or (f"output/ЧК_ОСИНТ_сводный_"
                    f"{datetime.date.today().isoformat()}.xlsx")
    wb.save(path)
    return path


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "data/Выборка_компаний_V2.xlsx"
    print("сводный файл:", export_combined(src))
