from __future__ import annotations

import unittest

from linux_agent import attempt_plan as linux_attempt_plan


class PlatformConfigTests(unittest.TestCase):
    def test_linux_attempts_are_sorted_and_clamped(self) -> None:
        plan = linux_attempt_plan({"capture_attempts": [
            {"delay_seconds": 9, "scroll_up_clicks": 4},
            {"delay_seconds": 1, "scroll_up_clicks": -2},
        ]})
        self.assertEqual(1.0, plan[0]["delay_seconds"])
        self.assertEqual(0, plan[0]["scroll_up_clicks"])

if __name__ == "__main__":
    unittest.main()
