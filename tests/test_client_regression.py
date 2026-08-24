"""РЕГРЕССИЯ НА КЛИЕНТЕ — обязательный приёмочный тест (заказчик, 2026-08-24).

Классификатор прогоняется на фактическом профиле «Чистой Кожи»
(data/client_profile.yaml ← ЧК_ОСИНТ_ОБРАЗЕЦ.xlsx, «Оказывает ли клиент»).
Ожидание: Тип 1, esthetic_markers_found пуст, несмежных нет.

Тест обязан проходить после КАЖДОГО изменения classifier.yaml или
services.yaml. Провал = сломаны ПРАВИЛА, а не данные: менять правила.
"""

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.classify import classify, load_contours

ROOT = Path(__file__).resolve().parent.parent
PROFILE = yaml.safe_load((ROOT / "data/client_profile.yaml").read_text(encoding="utf-8"))


class TestClientRegression(unittest.TestCase):
    def setUp(self):
        self.contours = load_contours(ROOT / "dictionaries/services.yaml")
        self.result = classify(set(PROFILE["tags"]), nonadjacent_found=[],
                               contours=self.contours)

    def test_client_profile_tags_exist_in_services(self):
        missing = set(PROFILE["tags"]) - set(self.contours)
        self.assertFalse(missing, f"теги профиля клиента пропали из services.yaml: {missing}")

    def test_client_is_type_1(self):
        self.assertEqual(self.result["type"], "Тип 1", self.result)

    def test_client_has_zero_esthetic_markers(self):
        self.assertEqual(self.result["esthetic_markers_found"], [], self.result)

    def test_client_has_zero_nonadjacent(self):
        self.assertEqual(self.result["nonadjacent_found"], [])
        self.assertFalse(self.result["flag_single_nonadjacent"])

    def test_client_removal_is_inside_derm_contour(self):
        # у клиента удаление С гистологией и дерматологическим приёмом
        self.assertFalse(self.result["flag_removal_outside_derm"], self.result)


class TestClassifierRules(unittest.TestCase):
    """Смежные проверки движка на синтетических профилях."""

    def setUp(self):
        self.contours = load_contours(ROOT / "dictionaries/services.yaml")

    def test_esthetic_plus_derm_is_type2(self):
        r = classify({"derm_consult", "epilation"}, contours=self.contours)
        self.assertEqual(r["type"], "Тип 2")
        self.assertEqual(r["esthetic_markers_found"], ["epilation"])

    def test_vascular_is_not_esthetic_marker(self):
        # решение заказчика 2026-08-24: сосуды — дерматология, не маркер R2
        r = classify({"derm_consult", "laser_vascular"}, contours=self.contours)
        self.assertEqual(r["type"], "Тип 1")
        self.assertEqual(r["esthetic_markers_found"], [])

    def test_nonadjacent_gives_type3_with_single_flag(self):
        r = classify({"derm_consult"}, nonadjacent_found=["гинекология"],
                     contours=self.contours)
        self.assertEqual(r["type"], "Тип 3")
        self.assertTrue(r["flag_single_nonadjacent"])

    def test_informational_marker_does_not_decide(self):
        r = classify({"derm_consult"},
                     nonadjacent_found=["широкая инструментальная диагностика (УЗИ органов)"],
                     contours=self.contours)
        self.assertEqual(r["type"], "Тип 1")

    def test_cosm_only_is_type1_cosm(self):
        r = classify({"epilation", "cosm_consult"}, contours=self.contours)
        self.assertEqual(r["type"], "Тип 1 (косметологический)")

    def test_removal_without_derm_diag_sets_flag(self):
        r = classify({"epilation", "neoplasm_removal_laser"}, contours=self.contours)
        self.assertEqual(r["type"], "Тип 2")  # dermsurg ∈ medical_derm — механика R2
        self.assertTrue(r["flag_removal_outside_derm"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
