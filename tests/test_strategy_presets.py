import unittest

from strategy_presets import active_strategy, normalize_strategy_tag


class StrategyPresetTests(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(normalize_strategy_tag("max export"), "max-export")
        self.assertEqual(normalize_strategy_tag("max_export"), "max-export")
        self.assertEqual(normalize_strategy_tag("safe"), "conservative")
        self.assertEqual(normalize_strategy_tag("eco"), "economic")

    def test_default_is_conservative(self):
        self.assertEqual(active_strategy({}).tag, "conservative")

    def test_risky_is_more_optimistic_than_conservative(self):
        self.assertEqual(active_strategy({"strategy": {"active": "risky"}}).night_mode, "floor")
        self.assertEqual(active_strategy({"strategy": {"active": "conservative"}}).night_mode, "forecast_protected")


if __name__ == "__main__":
    unittest.main()
