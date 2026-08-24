"""Целостность справочника: «одна формулировка — один тег»
(правило заказчика 2026-08-25, введено после инцидента с куперозом).

Прогоняется при каждом изменении services.yaml вместе с регрессией на клиенте.
"""

import sys
import unittest
from collections import defaultdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SV = yaml.safe_load((ROOT / "dictionaries/services.yaml").read_text(encoding="utf-8"))


class TestOneFormulationOneTag(unittest.TestCase):
    def test_no_formulation_in_two_tags(self):
        seen = defaultdict(list)
        for t in SV["tags"]:
            for f in t.get("formulations_site", []) + t.get("formulations_wordstat", []):
                seen[f.lower().strip()].append(t["tag"])
        dups = {f: tags for f, tags in seen.items() if len(set(tags)) > 1}
        self.assertFalse(dups, f"формулировка более чем в одном теге: {dups}")

    def test_no_formulation_equals_foreign_tag_name(self):
        names = {t["name_ru"].lower().strip(): t["tag"] for t in SV["tags"]}
        bad = {}
        for t in SV["tags"]:
            for f in t.get("formulations_site", []):
                key = f.lower().strip()
                if key in names and names[key] != t["tag"]:
                    bad[f] = (t["tag"], names[key])
        self.assertFalse(bad, f"формулировка совпадает с name_ru чужого тега: {bad}")

    def test_consult_tags_have_no_icd(self):
        # правка заказчика 2026-08-25: у тегов-приёмов МКБ = «Не применимо»
        for tid in ("derm_consult", "derm_consult_child", "dermsurg_consult",
                    "onco_consult", "cosm_consult", "trich_diag"):
            tag = next(t for t in SV["tags"] if t["tag"] == tid)
            self.assertEqual(tag["icd10"], "Не применимо", tid)

    def test_unique_tag_ids_and_names(self):
        ids = [t["tag"] for t in SV["tags"]]
        self.assertEqual(len(ids), len(set(ids)))
        names = [t["name_ru"].lower().strip() for t in SV["tags"]]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
