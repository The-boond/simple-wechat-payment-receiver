from __future__ import annotations

import tempfile
import unittest

from linux_agent import LinuxCapture, attempt_plan as linux_attempt_plan


class PlatformConfigTests(unittest.TestCase):
    def test_linux_attempts_are_sorted_and_clamped(self) -> None:
        plan = linux_attempt_plan({"capture_attempts": [
            {"delay_seconds": 9, "scroll_up_clicks": 4},
            {"delay_seconds": 1, "scroll_up_clicks": -2},
        ]})
        self.assertEqual(1.0, plan[0]["delay_seconds"])
        self.assertEqual(0, plan[0]["scroll_up_clicks"])

    def test_linux_window_regexes_preserve_configured_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = LinuxCapture(
                {
                    "window_name_regexes": [
                        "^微信支付商家助手$",
                        "^微信收款助手$",
                        "^微信支付商家助手$",
                    ],
                    "capture_dir": directory,
                },
                {"_config_dir": directory},
            )
        self.assertEqual(
            ("^微信支付商家助手$", "^微信收款助手$"),
            capture.window_name_regexes,
        )
        self.assertEqual("^微信支付商家助手$", capture.window_name_regex)

if __name__ == "__main__":
    unittest.main()
