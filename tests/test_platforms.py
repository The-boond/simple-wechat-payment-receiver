from __future__ import annotations

import unittest

from linux_agent import attempt_plan as linux_attempt_plan
from windows_agent import attempt_plan as windows_attempt_plan


class PlatformConfigTests(unittest.TestCase):
    def test_linux_attempts_are_sorted_and_clamped(self) -> None:
        plan = linux_attempt_plan({"capture_attempts": [
            {"delay_seconds": 9, "scroll_up_clicks": 4},
            {"delay_seconds": 1, "scroll_up_clicks": -2},
        ]})
        self.assertEqual(1.0, plan[0]["delay_seconds"])
        self.assertEqual(0, plan[0]["scroll_up_clicks"])

    def test_windows_restore_is_explicit(self) -> None:
        plan = windows_attempt_plan({"capture_attempts": [
            {"delay_seconds": 4, "request_restore": False},
            {"delay_seconds": 8, "request_restore": True},
        ]})
        self.assertFalse(plan[0]["request_restore"])
        self.assertTrue(plan[1]["request_restore"])


if __name__ == "__main__":
    unittest.main()
