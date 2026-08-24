"""Тесты двухступенчатого маппинга (ступень 1) и нормализации названий."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.mapper import build_formulation_index, map_tier1, normalize_service_name


class TestNormalization(unittest.TestCase):
    def test_sizes_and_units_removed(self):
        a = normalize_service_name("Удаление (лазером KN TECH, ЭХВЧ Фотек) мелких элементов 0,1-0,2 см/ от 1 до 3 элементов за 1 ед.")
        b = normalize_service_name("Удаление (лазером KN TECH, ЭХВЧ Фотек) элементов св 2 см/ за 1 ед.")
        self.assertEqual(a, b)  # ценовая градация схлопывается в один ключ

    def test_category_numbers_removed(self):
        a = normalize_service_name("Эксцизионная биопсия новообразования кожи 1 кат.сложности (без стоимости)")
        b = normalize_service_name("Эксцизионная биопсия новообразования кожи 4 кат.сложности (без стоимости)")
        self.assertEqual(a, b)

    def test_parens_removed(self):
        self.assertEqual(normalize_service_name("Консультация врача дерматолога (первичная)"),
                         "консультация врача дерматолога")


class TestTier1(unittest.TestCase):
    def setUp(self):
        self.index = build_formulation_index()

    def test_exact_formulation_maps(self):
        m = map_tier1("Удаление папиллом", self.index)
        self.assertIsNotNone(m)
        self.assertEqual(m["tag"], "removal_viral")
        self.assertEqual(m["tier"], "код")

    def test_price_wording_maps_via_normalization(self):
        m = map_tier1("Консультация врача дерматолога (первичная)", self.index)
        self.assertIsNotNone(m)
        self.assertEqual(m["tag"], "derm_consult")

    def test_unknown_returns_none(self):
        self.assertIsNone(map_tier1("Криолиполиз живота аппаратом X", self.index))


if __name__ == "__main__":
    unittest.main(verbosity=2)
