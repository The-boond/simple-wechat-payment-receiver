from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from typing import Sequence
from unittest import mock
from zoneinfo import ZoneInfo

import linux_agent
import receiver_core
from receiver_core import (
    BridgeClient,
    CaptureTriggerStore,
    MerchantTextReceipt,
    PaymentEvent,
    VisualAmount,
    VisualClock,
    VisualToken,
    associate_visual_receipts,
    merchant_parser_text,
    merchant_text_receipts,
    parse_tesseract_tsv,
    trigger_file_change,
    visual_amount_from_token,
    visual_clock_from_token,
)


def local_timestamp(hour: int, minute: int) -> int:
    return int(
        datetime(
            2026,
            7,
            26,
            hour,
            minute,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ).timestamp()
    )


class MerchantEvidenceTests(unittest.TestCase):
    def test_group_clock_is_reused_for_following_card(self) -> None:
        receipts = merchant_text_receipts(
            "14:20 收款通知 ¥18.31 商品名称：第一张 "
            "收款通知 ¥18.32 商品名称：第二张"
        )
        self.assertEqual(
            [("14:20", "18.31"), ("14:20", "18.32")],
            [(row.clock, row.amount) for row in receipts],
        )

    def test_group_clock_survives_a_card_with_unreadable_amount(self) -> None:
        receipts = merchant_text_receipts(
            "14:20 收款通知 商品名称：第一张金额未识别 "
            "收款通知 ¥18.32 商品名称：第二张"
        )
        self.assertEqual(
            [("14:20", "18.32")],
            [(row.clock, row.amount) for row in receipts],
        )

    def test_card_does_not_borrow_amount_from_following_card(self) -> None:
        receipts = merchant_text_receipts(
            "10:01 收款通知 商品名称：第一张金额未识别 "
            "10:02 收款通知 ¥19.12 商品名称：第二张"
        )
        self.assertEqual(
            [MerchantTextReceipt("10:02", "19.12", receipts[0].raw_text)],
            receipts,
        )

    def test_clipped_top_card_waits_for_overlapping_frame(self) -> None:
        amounts = [
            VisualAmount("23.45", 185.0, 90.0),
            VisualAmount("67.89", 590.0, 92.0),
        ]
        clocks = [VisualClock("14:20", 410.0, 96.0)]
        pairs = associate_visual_receipts(amounts, clocks)
        self.assertEqual(
            [("14:20", "67.89")],
            [(clock.clock, amount.amount) for clock, amount in pairs],
        )

    def test_clipped_card_does_not_use_a_clock_below_it_as_fallback(self) -> None:
        text = merchant_parser_text(
            "收款通知 ¥18.31 商品名称：顶部截断卡",
            [],
            [VisualClock("14:20", 500.0, 96.0)],
            allow_clock_fallback=False,
        )
        self.assertEqual([], merchant_text_receipts(text))

    def test_visual_pair_replaces_conflicting_full_ocr_amount(self) -> None:
        clocks = [VisualClock("14:20", 520.0, 96.0)]
        pairs = [(clocks[0], VisualAmount("41.23", 700.0, 92.0))]
        text = merchant_parser_text(
            "14:20 收款通知 ¥41.28",
            pairs,
            clocks,
        )
        self.assertIn("41.23", text)
        self.assertNotIn("41.28", text)

    def test_visual_merge_preserves_uncovered_card(self) -> None:
        clocks = [VisualClock("14:22", 520.0, 96.0)]
        pairs = [(clocks[0], VisualAmount("12.32", 700.0, 92.0))]
        text = merchant_parser_text(
            "14:21 收款通知 ¥12.31 商品名称：第一张 "
            "14:22 收款通知 ¥12.30 商品名称：第二张",
            pairs,
            clocks,
        )
        receipts = merchant_text_receipts(text)
        self.assertEqual(
            [("14:21", "12.31"), ("14:22", "12.32")],
            [(row.clock, row.amount) for row in receipts],
        )
        self.assertNotIn("12.30", text)

    def test_visual_merge_does_not_guess_between_repeated_amounts(self) -> None:
        clocks = [VisualClock("14:20", 160.0, 96.0)]
        pairs = [(clocks[0], VisualAmount("18.31", 340.0, 92.0))]
        text = merchant_parser_text(
            "14:18 收款通知 ¥18.31；14:19 收款通知 ¥18.31",
            pairs,
            clocks,
        )
        receipts = merchant_text_receipts(text)
        self.assertEqual(
            [
                ("14:18", "18.31"),
                ("14:19", "18.31"),
                ("14:20", "18.31"),
            ],
            sorted((row.clock, row.amount) for row in receipts),
        )

    def test_visual_tokens_require_coordinate_confidence(self) -> None:
        header = (
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
            "left\ttop\twidth\theight\tconf\ttext"
        )
        tokens = parse_tesseract_tsv(
            "\n".join(
                [
                    header,
                    "5\t1\t1\t1\t1\t1\t299\t170\t121\t31\t90\t¥18.31",
                    "5\t1\t2\t1\t1\t1\t80\t1190\t90\t42\t96\t14:20",
                ]
            )
        )
        amount = visual_amount_from_token(tokens[0])
        clock = visual_clock_from_token(tokens[1], resize_factor=3.0)
        self.assertEqual("18.31", amount.amount if amount else None)
        self.assertEqual("14:20", clock.clock if clock else None)
        self.assertIsNone(
            visual_amount_from_token(VisualToken("12.34", 0, 0, 80, 20, 20))
        )


class DurableTriggerTests(unittest.TestCase):
    def test_store_survives_restart_and_writes_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture-trigger.json"
            first = CaptureTriggerStore(path)
            scheduled = first.schedule(
                signature="wal:1",
                trigger_time=100,
                reason="trigger_file_changed",
            )
            self.assertEqual(scheduled, CaptureTriggerStore(path).load())
            self.assertFalse(path.with_suffix(".tmp").exists())
            first.clear(expected_signature="different")
            self.assertTrue(path.exists())
            first.clear(expected_signature="wal:1")
            self.assertFalse(path.exists())

    def test_missing_to_present_file_is_a_capture_change(self) -> None:
        current = (101, 202, 303, 404)
        self.assertIsNone(trigger_file_change(None, None))
        self.assertIsNone(trigger_file_change(current, current))
        signature, reason = trigger_file_change(None, current)
        self.assertEqual("101:202:303:404", signature)
        self.assertEqual("trigger_file_reappeared", reason)
        self.assertEqual(
            "trigger_file_changed",
            trigger_file_change(current, (101, 202, 304, 405))[1],
        )


class LinuxCaptureTests(unittest.TestCase):
    def _capture(self, directory: str, **overrides: object) -> linux_agent.LinuxCapture:
        platform = {
            "window_name_regexes": ["^微信收款助手$"],
            "capture_dir": directory,
            "adaptive_scroll_enabled": False,
            "scroll_up_attempts": [0, 4],
            **overrides,
        }
        return linux_agent.LinuxCapture(
            platform,
            {"_config_dir": directory},
        )

    def test_ocr_workers_default_to_one_and_clamp_to_two(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(1, self._capture(directory).ocr_workers)
            self.assertEqual(2, self._capture(directory, ocr_workers=8).ocr_workers)

    def test_all_screenshots_are_taken_before_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self._capture(directory)
            operations: list[str] = []

            def fake_run(args: Sequence[str], **_kwargs: object):
                if args[0] == "import":
                    operations.append("capture")
                    Path(args[-1]).write_bytes(b"fixture")
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[0] == "tesseract":
                    operations.append("ocr")
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "10:30 收款通知 ¥19.11",
                        "",
                    )
                raise AssertionError(args)

            with (
                mock.patch.object(
                    capture,
                    "find_windows",
                    return_value=[("^微信收款助手$", "123")],
                ),
                mock.patch.object(capture, "_prepare_window", return_value=True),
                mock.patch.object(capture, "_return_to_bottom", return_value=True),
                mock.patch.object(capture, "_xdotool", return_value=True),
                mock.patch.object(capture, "_run", side_effect=fake_run),
                mock.patch.object(linux_agent.time, "sleep", return_value=None),
            ):
                segments = list(capture.capture_segments())

        self.assertEqual(["capture", "capture", "ocr", "ocr"], operations)
        self.assertEqual(2, len(segments))
        self.assertTrue(capture.last_capture_successful)

    def test_adaptive_scroll_stops_after_repeated_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self._capture(
                directory,
                adaptive_scroll_enabled=True,
                adaptive_scroll_clicks=4,
                adaptive_scroll_max_frames=6,
            )

            def fake_run(args: Sequence[str], **_kwargs: object):
                if args[0] == "import":
                    Path(args[-1]).write_bytes(b"fixture")
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[0] == "tesseract":
                    return subprocess.CompletedProcess(args, 0, "frame", "")
                raise AssertionError(args)

            with (
                mock.patch.object(
                    capture,
                    "find_windows",
                    return_value=[("^微信收款助手$", "123")],
                ),
                mock.patch.object(capture, "_prepare_window", return_value=True),
                mock.patch.object(capture, "_return_to_bottom", return_value=True),
                mock.patch.object(capture, "_xdotool", return_value=True),
                mock.patch.object(
                    capture,
                    "_visual_fingerprint_file",
                    side_effect=["frame-a", "frame-b", "frame-b"],
                ),
                mock.patch.object(capture, "_run", side_effect=fake_run),
                mock.patch.object(linux_agent.time, "sleep", return_value=None),
            ):
                segments = list(capture.capture_segments())

        self.assertEqual(2, len(segments))
        self.assertTrue(capture.last_capture_successful)

    def test_capture_timeout_keeps_cycle_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self._capture(directory, scroll_up_attempts=[0])
            timeout_result = subprocess.CompletedProcess(
                ["import"],
                124,
                "",
                "command timed out",
            )
            with (
                mock.patch.object(
                    capture,
                    "find_windows",
                    return_value=[("^微信收款助手$", "123")],
                ),
                mock.patch.object(capture, "_prepare_window", return_value=True),
                mock.patch.object(capture, "_return_to_bottom", return_value=True),
                mock.patch.object(capture, "_xdotool", return_value=True),
                mock.patch.object(capture, "_run", return_value=timeout_result),
                mock.patch.object(linux_agent.time, "sleep", return_value=None),
            ):
                self.assertEqual([], list(capture.capture_segments()))
        self.assertFalse(capture.last_capture_successful)
        self.assertFalse(linux_agent.capture_scan_can_clear(capture, None))
        self.assertEqual("no_ocr_frames", capture.last_capture_failure_reason)

    def test_timeout_is_a_controlled_result(self) -> None:
        timeout = subprocess.TimeoutExpired(["tesseract"], 3)
        with mock.patch.object(subprocess, "run", side_effect=timeout):
            result = linux_agent.command(["tesseract", "fixture.png"], timeout=3)
        self.assertEqual(124, result.returncode)
        self.assertIn("timed out", result.stderr)

    def test_screen_probe_hamming_threshold_ignores_small_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture = self._capture(
                directory,
                screen_probe_hamming_threshold=3,
            )
        self.assertTrue(
            capture.screen_fingerprints_equal(
                "0000000000000000",
                "0000000000000007",
            )
        )
        self.assertFalse(
            capture.screen_fingerprints_equal(
                "0000000000000000",
                "000000000000000f",
            )
        )

    def test_screenshot_retention_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture = self._capture(
                directory,
                screenshot_retention_days=7,
                screenshot_max_files=2,
            )
            now = time.time()
            old = root / "receipt-old.png"
            active = root / "receipt-active.png"
            recent = [
                root / "receipt-recent-1.png",
                root / "receipt-recent-2.png",
                root / "receipt-recent-3.png",
            ]
            evidence_dir = root / "evidence"
            evidence_dir.mkdir()
            evidence = evidence_dir / "receipt-evidence.png"
            for path in [old, active, *recent, evidence]:
                path.write_bytes(b"fixture")
            os.utime(old, (now - 8 * 86400, now - 8 * 86400))
            os.utime(active, (now - 8 * 86400, now - 8 * 86400))
            for index, path in enumerate(recent, start=1):
                os.utime(path, (now - index, now - index))

            removed = capture.prune_capture_screenshots(exclude=[active])

            self.assertEqual((1, 1), removed)
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(recent[0].exists())
            self.assertTrue(recent[1].exists())
            self.assertFalse(recent[2].exists())
            self.assertTrue(evidence.exists())


class BridgeResponseTests(unittest.TestCase):
    def test_non_json_success_response_is_retryable(self) -> None:
        event = PaymentEvent(
            event_id="evt_fixture",
            provider="wxpay",
            channel_id="7821",
            amount="19.11",
            occurred_at=local_timestamp(10, 30),
            external_txn_id=None,
            trade_no=None,
            payer=None,
            raw_text=None,
            source="test",
            agent_id="test-agent",
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            @staticmethod
            def read() -> bytes:
                return b"<html>upstream transition</html>"

        config = {
            "bridge": {
                "url": "https://example.test/event",
                "token_env": "TEST_RECEIVER_TOKEN",
            }
        }
        with (
            mock.patch.dict(
                os.environ,
                {"TEST_RECEIVER_TOKEN": "fixture-secret"},
                clear=False,
            ),
            mock.patch.object(
                receiver_core.urllib.request,
                "urlopen",
                return_value=FakeResponse(),
            ),
        ):
            result = BridgeClient(config).send(event)
        self.assertFalse(result["ok"])
        self.assertEqual("invalid_bridge_response", result["reason"])
        self.assertEqual(200, result["http_status"])


if __name__ == "__main__":
    unittest.main()
