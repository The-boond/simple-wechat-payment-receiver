from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import linux_agent
from receiver_core import (
    ReceiptParser,
    ServiceNotificationReceipt,
    ServiceNotificationState,
    service_notification_receipts,
)


TIMEZONE = ZoneInfo("Asia/Shanghai")


def local_timestamp(hour: int, minute: int, second: int = 0) -> int:
    return int(
        datetime(
            2026,
            1,
            15,
            hour,
            minute,
            second,
            tzinfo=TIMEZONE,
        ).timestamp()
    )


def parser() -> ReceiptParser:
    return ReceiptParser(
        {
            "agent": {"id": "test-agent"},
            "channel": {"id": "7821", "provider": "wxpay"},
            "parser": {
                "timezone": "Asia/Shanghai",
                "max_event_age_seconds": 300,
            },
        }
    )


class ServiceNotificationParserTests(unittest.TestCase):
    def test_prefers_order_amount_before_transaction_id(self) -> None:
        receipts = service_notification_receipts(
            "微信收款商业版 收款通知 01月15日 10:11:12 "
            "收款金额 x12.30 订单金额 ¥12.34 顾客昵称 R*** "
            "交易单号 4200000000000000000000000001 "
            "收款汇总 该店今日第8笔收款，共¥999.99",
            trigger_time=local_timestamp(10, 12),
            timezone=TIMEZONE,
        )
        self.assertEqual(1, len(receipts))
        self.assertEqual("12.34", receipts[0].amount)
        self.assertEqual(local_timestamp(10, 11, 12), receipts[0].occurred_at)
        self.assertEqual(
            "4200000000000000000000000001",
            receipts[0].external_txn_id,
        )

    def test_same_amount_and_second_keep_distinct_transactions(self) -> None:
        receipts = service_notification_receipts(
            "收款通知 01月15日 10:11:12 收款金额 ¥12.34 "
            "订单金额 ¥12.34 交易单号 4200000000000000000000000001 "
            "收款通知 01月15日 10:11:12 收款金额 ¥12.34 "
            "订单金额 ¥12.34 交易单号 4200000000000000000000000002",
            trigger_time=local_timestamp(10, 12),
            timezone=TIMEZONE,
        )
        events, reason = parser().events_from_service_notifications(
            receipts,
            trigger_time=local_timestamp(10, 12),
            source="test-service-notification",
        )
        self.assertEqual("", reason)
        self.assertEqual(2, len(events))
        self.assertEqual(2, len({event.event_id for event, _key in events}))
        self.assertEqual(2, len({key for _event, key in events}))

    def test_incomplete_card_is_not_emitted(self) -> None:
        receipts = service_notification_receipts(
            "¥56.78 交易单号 4200000000000000000000000002 "
            "收款通知 01月15日 10:11:12 收款金额 ¥12.34",
            trigger_time=local_timestamp(10, 12),
            timezone=TIMEZONE,
        )
        self.assertEqual([], receipts)

    def test_event_identity_is_stable_and_known_cursor_is_filtered(self) -> None:
        receipt = ServiceNotificationReceipt(
            occurred_at=local_timestamp(10, 11, 12),
            amount="12.34",
            external_txn_id="4200000000000000000000000001",
            raw_text="fixture",
        )
        first, _ = parser().events_from_service_notifications(
            [receipt],
            trigger_time=local_timestamp(10, 12),
            source="first-trigger",
        )
        second, _ = parser().events_from_service_notifications(
            [receipt],
            trigger_time=local_timestamp(10, 12, 10),
            source="second-trigger",
        )
        known, _ = parser().events_from_service_notifications(
            [receipt],
            trigger_time=local_timestamp(10, 12),
            source="known-trigger",
            known_transaction_ids=[receipt.external_txn_id],
        )
        self.assertEqual(first[0][0].event_id, second[0][0].event_id)
        self.assertEqual(first[0][1], second[0][1])
        self.assertEqual([], known)


class ServiceNotificationStateTests(unittest.TestCase):
    def test_state_persists_a_bounded_transaction_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service-state.json"
            state = ServiceNotificationState(path, max_transaction_ids=32)
            values = [
                f"420000000000000000000000{i:04d}"
                for i in range(40)
            ]
            state.commit(values)
            restored = ServiceNotificationState(
                path,
                max_transaction_ids=32,
            )
            self.assertTrue(restored.initialized)
            self.assertEqual(values[:32], list(restored.transaction_ids))


class ServiceNotificationCaptureTests(unittest.TestCase):
    def test_scanner_stops_on_persistent_transaction_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = ServiceNotificationState(
                Path(directory) / "service-state.json"
            )
            known = "4200000000000000000000000002"
            new = "4200000000000000000000000001"
            state.commit([known])
            capture = linux_agent.LinuxCapture(
                {
                    "capture_dir": directory,
                    "ocr_workers": 1,
                    "service_notifications": {
                        "enabled": True,
                        "scan_batch_frames": 1,
                        "scan_max_frames": 20,
                        "settle_seconds": 0.1,
                        "tail_recheck_seconds": 0.1,
                    },
                },
                {"_config_dir": directory},
            )
            texts = {
                "a1": (
                    "收款通知 01月15日 10:11:12 收款金额 ¥12.34 "
                    f"订单金额 ¥12.34 交易单号 {new}"
                ),
                "a2": (
                    "收款通知 01月15日 10:10:01 收款金额 ¥56.78 "
                    f"订单金额 ¥56.78 交易单号 {known}"
                ),
            }

            def capture_window(path: Path) -> tuple[bool, None]:
                path.write_bytes(b"fixture")
                return True, None

            def fingerprint(path: Path) -> str:
                if "tail" in path.name:
                    return "bottom"
                return "bottom" if "-a1-" in path.name else "anchor"

            def service_ocr(
                path: Path,
            ) -> tuple[Path, str, int, None]:
                key = "a1" if "-a1-" in path.name else "a2"
                return path, texts[key], 10, None

            with (
                mock.patch.object(
                    capture,
                    "_find_window",
                    side_effect=lambda regex: (
                        "123" if regex == "^Weixin$" else None
                    ),
                ),
                mock.patch.object(
                    capture,
                    "_window_geometry",
                    return_value={
                        "X": 1,
                        "Y": 2,
                        "WIDTH": 800,
                        "HEIGHT": 700,
                    },
                ),
                mock.patch.object(
                    capture,
                    "_xdotool",
                    return_value=True,
                ) as xdotool,
                mock.patch.object(
                    capture,
                    "_capture_service_window",
                    side_effect=capture_window,
                ),
                mock.patch.object(
                    capture,
                    "_service_visual_fingerprint_file",
                    side_effect=fingerprint,
                ),
                mock.patch.object(
                    capture,
                    "_ocr_service_frame",
                    side_effect=service_ocr,
                ),
                mock.patch.object(
                    linux_agent.time,
                    "sleep",
                    return_value=None,
                ),
            ):
                result = capture.capture_service_notifications(
                    state=state,
                    trigger_time=local_timestamp(10, 12),
                    timezone=TIMEZONE,
                    baseline_only=False,
                )

            self.assertTrue(result.successful)
            self.assertTrue(result.anchor_found)
            self.assertEqual(2, result.frames)
            self.assertEqual(
                {new, known},
                {receipt.external_txn_id for receipt in result.receipts},
            )
            self.assertFalse(
                any(
                    call.args
                    and call.args[0] == "click"
                    and call.args[-1] == "1"
                    for call in xdotool.call_args_list
                )
            )


if __name__ == "__main__":
    unittest.main()
