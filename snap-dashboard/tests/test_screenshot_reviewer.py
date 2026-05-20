from __future__ import annotations

import unittest

from snap_dashboard.agents.screenshot_reviewer import _aggregate_decisions


class AggregateDecisionsTests(unittest.TestCase):
    def test_reject_overrides_other_decisions(self) -> None:
        result = _aggregate_decisions(
            [
                {"decision": "approve", "confidence": 0.92, "reasoning": "Looks good."},
                {"decision": "reject", "confidence": 0.81, "reasoning": "Crash dialog visible."},
            ]
        )

        self.assertEqual(result["decision"], "reject")
        self.assertEqual(result["confidence"], 0.81)
        self.assertIn("Crash dialog visible.", result["reasoning"])

    def test_approve_uses_most_conservative_confidence(self) -> None:
        result = _aggregate_decisions(
            [
                {"decision": "approve", "confidence": 0.91, "reasoning": "Primary view is intact."},
                {"decision": "approve", "confidence": 0.87, "reasoning": "Secondary view is intact."},
            ]
        )

        self.assertEqual(result["decision"], "approve")
        self.assertEqual(result["confidence"], 0.87)
        self.assertIn("Primary view is intact.", result["reasoning"])
        self.assertIn("Secondary view is intact.", result["reasoning"])


if __name__ == "__main__":
    unittest.main()
