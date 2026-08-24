"""Тест диапазонов МКБ (системная правка заказчика 2026-08-25):
- запрет диапазона шире одного блока классификации МКБ-10;
- запрет целой главы (L00-L99 и подобных);
- исключения — только явные, из dictionaries/icd_exceptions.yaml, с обоснованием.

Чинится КЛАСС ошибки, а не экземпляры: тест гоняется по всем тегам услуг
и всем нозологиям при каждом изменении словарей.
"""

import re
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SV = yaml.safe_load((ROOT / "dictionaries/services.yaml").read_text(encoding="utf-8"))
NOS = yaml.safe_load((ROOT / "dictionaries/nosology.yaml").read_text(encoding="utf-8"))
EXC = yaml.safe_load((ROOT / "dictionaries/icd_exceptions.yaml").read_text(encoding="utf-8"))
EXCEPTION_CODES = {e["code"] for e in EXC["exceptions"]}

# Официальные блоки МКБ-10, встречающиеся в проекте (главы I, II, IX, XII, XVIII)
ICD_BLOCKS = [
    "A00-A09", "A15-A19", "A50-A64", "A65-A69", "A70-A74", "A75-A79", "A80-A89", "A90-A99",
    "B00-B09", "B15-B19", "B20-B24", "B25-B34", "B35-B49", "B50-B64", "B65-B83", "B85-B89", "B90-B94", "B95-B98", "B99-B99",
    "C00-C14", "C15-C26", "C30-C39", "C40-C41", "C43-C44", "C45-C49", "C50-C50", "C51-C58", "C60-C63", "C64-C68", "C69-C72",
    "C73-C75", "C76-C80", "C81-C96", "C97-C97", "D00-D09", "D10-D36", "D37-D48",
    "I00-I02", "I05-I09", "I10-I15", "I20-I25", "I26-I28", "I30-I52", "I60-I69", "I70-I79", "I80-I89", "I95-I99",
    "L00-L08", "L10-L14", "L20-L30", "L40-L45", "L50-L54", "L55-L59", "L60-L75", "L80-L99",
    "R00-R09", "R10-R19", "R20-R23", "R25-R29", "R30-R39", "R40-R46", "R47-R49", "R50-R69", "R70-R79", "R80-R82",
    "R83-R89", "R90-R94", "R95-R99",
]

CODE_RE = re.compile(r"^([A-Z])(\d{2})(?:\.(\d+))?$")
RANGE_RE = re.compile(r"^([A-Z])(\d{2})-([A-Z])(\d{2})$")


def _num(letter: str, nn: str) -> tuple[str, int]:
    return letter, int(nn)


def range_fits_strictly_inside_block(start_l, start_n, end_l, end_n) -> bool:
    """True, если диапазон лежит внутри одного блока И УЖЕ его.
    Полный блок целиком (как B35-B49) требует записи в реестре исключений —
    ужесточение после негативной проверки 2026-08-25: правило «не шире блока»
    пропускало B35-B49, который заказчик отклонил (системные микозы не профиль)."""
    for b in ICD_BLOCKS:
        m = RANGE_RE.match(b)
        bl, bs, el_, be = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
        if start_l == bl == end_l == el_ and bs <= start_n and end_n <= be:
            return not (start_n == bs and end_n == be)  # полный блок → False
    return False


def check_entry(code: str) -> str | None:
    """None — ок; строка — описание нарушения."""
    code = code.strip()
    if code in EXCEPTION_CODES:
        return None
    if CODE_RE.match(code):
        return None
    m = RANGE_RE.match(code)
    if not m:
        return f"нераспознанный формат кода: {code!r}"
    sl, sn, el_, en = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
    if sl != el_:
        return f"{code}: диапазон через букву главы"
    if sn == 0 and en == 99:
        return f"{code}: ЦЕЛАЯ ГЛАВА — запрещено"
    if not range_fits_strictly_inside_block(sl, sn, el_, en):
        return f"{code}: полный блок МКБ-10 или шире — требуется запись в реестре исключений"
    return None


def collect_violations() -> list[str]:
    out = []
    for t in SV["tags"]:
        icd = t["icd10"]
        if icd == "Не применимо":
            continue
        assert isinstance(icd, list), f"{t['tag']}: icd10 должен быть списком или «Не применимо»"
        for code in icd:
            v = check_entry(str(code))
            if v:
                out.append(f"services:{t['tag']} → {v}")
    for n in NOS["nosologies"]:
        v = check_entry(str(n["icd10"]))
        if v:
            out.append(f"nosology:{n['name']} → {v}")
    return out


class TestIcdRanges(unittest.TestCase):
    def test_no_wide_ranges_or_whole_chapters(self):
        violations = collect_violations()
        self.assertFalse(violations, "нарушения диапазонов МКБ:\n" + "\n".join(violations))

    def test_exceptions_have_justification(self):
        for e in EXC["exceptions"]:
            self.assertTrue(e.get("justification"), e["code"])
            self.assertTrue(e.get("used_in"), e["code"])

    def test_exceptions_actually_used(self):
        used = set()
        for t in SV["tags"]:
            if isinstance(t["icd10"], list):
                used.update(str(c) for c in t["icd10"])
        for e in EXC["exceptions"]:
            self.assertIn(e["code"], used, f"исключение {e['code']} не используется — удалить из реестра")


if __name__ == "__main__":
    vs = collect_violations()
    print("Нарушения:" if vs else "Нарушений нет")
    for v in vs:
        print(" -", v)
    unittest.main(verbosity=1)
