"""Тесты бюджетного счётчика и критерия насыщения (решения заказчика 2026-08-24)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.budget import BudgetTracker
from src.errors import BudgetExceededError
from src.saturation import check_saturation

PARAMS = {"min_list_len": 200, "window_min": 50, "window_share": 0.25,
          "earliest_trigger_share": 0.60, "new_candidates_threshold": 3}


class TestBudget(unittest.TestCase):
    def _tracker(self, tmp_name, ceiling=100.0, cost=10.0):
        import tempfile, pathlib, textwrap
        d = pathlib.Path(tempfile.mkdtemp())
        th = d / "thresholds.yaml"
        th.write_text(textwrap.dedent(f"""
            budget_rub: {ceiling}
            budget_warn_share: 0.8
            cost_per_request_rub:
              yandex_search_api: {cost}
              dadata: 0.0
        """), encoding="utf-8")
        return BudgetTracker(thresholds_path=th, state_path=d / "budget.json"), d

    def test_counter_persists_across_restarts(self):
        tr, d = self._tracker("t1")
        tr.charge("yandex_search_api", 3)
        tr2 = BudgetTracker(thresholds_path=d / "thresholds.yaml",
                            state_path=d / "budget.json")
        self.assertEqual(tr2.spent, 30.0)
        self.assertEqual(tr2.state["requests"]["yandex_search_api"], 3)

    def test_hard_stop_at_100_percent_before_request(self):
        tr, _ = self._tracker("t2", ceiling=100.0, cost=10.0)
        tr.charge("yandex_search_api", 10)  # ровно потолок — допустимо
        with self.assertRaises(BudgetExceededError):
            tr.charge("yandex_search_api", 1)  # 11-й не уходит

    def test_warning_at_80_percent(self):
        import io, contextlib
        tr, _ = self._tracker("t3", ceiling=100.0, cost=10.0)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tr.charge("yandex_search_api", 8)
        self.assertIn("ПРЕДУПРЕЖДЕНИЕ", buf.getvalue())

    def test_free_service_not_charged(self):
        tr, _ = self._tracker("t4")
        tr.charge("dadata", 100)
        self.assertEqual(tr.spent, 0.0)


class TestSaturation(unittest.TestCase):
    def test_not_applied_below_200(self):
        r = check_saturation([0] * 150, total_list_len=199, params=PARAMS)
        self.assertFalse(r["stopped"])
        self.assertIn("не применяется", r["reason"])

    def test_window_is_max_50_or_quarter(self):
        r = check_saturation([1] * 300, total_list_len=400, params=PARAMS)
        self.assertEqual(r["window"], 100)  # 25% от 400 > 50
        r2 = check_saturation([1] * 150, total_list_len=200, params=PARAMS)
        self.assertEqual(r2["window"], 50)  # 25% от 200 = 50

    def test_no_trigger_before_60_percent(self):
        # 200 запросов: порог с 120-го; на 119 выполненных — рано, даже при нулях
        r = check_saturation([0] * 119, total_list_len=200, params=PARAMS)
        self.assertFalse(r["stopped"])

    def test_trigger_after_60_percent_with_dry_window(self):
        # 120 выполнено из 200, последние 50 — сухие
        history = [5] * 70 + [0] * 50
        r = check_saturation(history, total_list_len=200, params=PARAMS)
        self.assertTrue(r["stopped"])
        self.assertEqual(r["at_query"], 120)

    def test_no_trigger_when_window_wet(self):
        history = [5] * 70 + [0] * 49 + [3]
        r = check_saturation(history, total_list_len=200, params=PARAMS)
        self.assertFalse(r["stopped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
