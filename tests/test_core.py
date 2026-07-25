from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from receiver_core import (
    AgentRuntime,
    EventSpool,
    OCR_CAPTURE_SEPARATOR,
    PaymentEvent,
    ReceiptDedupe,
    ReceiptParser,
    discover_trigger_files,
    validate_config,
)


def base_config() -> dict:
    return {
        "bridge": {"url": "https://example.test/event", "token_env": "TEST_RECEIVER_TOKEN"},
        "agent": {"id": "test-agent"},
        "channel": {"id": "7821", "provider": "wxpay"},
        "parser": {"timezone": "Asia/Shanghai", "max_event_age_seconds": 180},
        "linux": {"trigger_files": ["/tmp/example.db-wal"]},
    }


class ReceiptParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ReceiptParser(base_config())
        self.trigger = int(datetime(2026, 7, 21, 22, 34, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        self.text = "经营码收款到账通知\n07月21日 22:33\n收款金额\n¥18.88"

    def test_parses_fresh_receipt(self) -> None:
        event, key = self.parser.parse(
            self.text,
            trigger_time=self.trigger,
            trigger_signature="wal:1:2",
            source="test",
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual("18.88", event.amount)
        self.assertEqual("test-agent", event.agent_id)
        self.assertEqual("wxpay|7821|18.88|1784644380", key)
        self.assertNotIn("经营码", event.raw_text or "")

    def test_rejects_stale_receipt(self) -> None:
        event, reason = self.parser.parse(
            self.text,
            trigger_time=self.trigger + 600,
            trigger_signature="wal:1:3",
            source="test",
        )
        self.assertIsNone(event)
        self.assertTrue(reason.startswith("stale"))

    def test_parses_receipt_when_ocr_drops_amount_label(self) -> None:
        text = "经营码收款到账通知\n07月22日 22:41\n¥34.80"
        event, _ = self.parser.parse(
            text,
            trigger_time=int(datetime(2026, 7, 22, 22, 42, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()),
            trigger_signature="wal:1:4",
            source="test",
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual("34.80", event.amount)

    def test_stitches_partial_adjacent_captures(self) -> None:
        text = OCR_CAPTURE_SEPARATOR.join(
            [
                "¥29.90 收款店铺 是四苦不是四喜 店铺今日收款 124.30元",
                "乱码标题 07月23日 13:10 乱码标签 ¥29.90 收款店铺",
                "经营码收款到账通知 07月23日 13:10",
            ]
        )
        event, _ = self.parser.parse(
            text,
            trigger_time=int(datetime(2026, 7, 23, 13, 10, 50, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()),
            trigger_signature="wal:1:5",
            source="test",
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual("29.90", event.amount)

    def test_event_id_changes_with_trigger_signature(self) -> None:
        one, _ = self.parser.parse(self.text, trigger_time=self.trigger, trigger_signature="a", source="test")
        two, _ = self.parser.parse(self.text, trigger_time=self.trigger, trigger_signature="b", source="test")
        assert one and two
        self.assertNotEqual(one.event_id, two.event_id)

    def test_parse_all_returns_multiple_receipts_from_one_minute(self) -> None:
        text = (
            "经营码收款到账通知 07月21日22:33 收款金额 ¥18.88 "
            "经营码收款到账通知 07月21日22:33 收款金额 ¥18.89"
        )
        events, reason = self.parser.parse_all(
            text,
            trigger_time=self.trigger,
            trigger_signature="batch",
            source="test",
        )
        self.assertEqual("", reason)
        self.assertEqual(["18.88", "18.89"], [event.amount for event, _ in events])
        self.assertEqual(2, len({key for _, key in events}))

    def test_parse_all_collapses_duplicates_across_captures(self) -> None:
        text = OCR_CAPTURE_SEPARATOR.join([self.text, self.text])
        events, _ = self.parser.parse_all(
            text,
            trigger_time=self.trigger,
            trigger_signature="batch",
            source="test",
        )
        self.assertEqual(1, len(events))

    def test_parses_merchant_assistant_time_only_receipt(self) -> None:
        trigger = int(
            datetime(
                2026,
                7,
                26,
                3,
                14,
                20,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
        )
        event, reason = self.parser.parse(
            "03:14 收款通知 ¥192.58 商品名称：店铺",
            trigger_time=trigger,
            trigger_signature="merchant",
            source="test",
        )
        self.assertEqual("wxpay|7821|192.58|1785006840", reason)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual("192.58", event.amount)
        self.assertEqual(1785006840, event.occurred_at)

    def test_merchant_receipt_uses_nearest_preceding_clock(self) -> None:
        trigger = int(
            datetime(
                2026,
                7,
                26,
                3,
                14,
                20,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
        )
        event, _ = self.parser.parse(
            "02:14 员工账号成功创建通知 03:14 收款通知 "
            "¥192.58 商品名称：店铺 03:23 入驻申请进展通知",
            trigger_time=trigger,
            trigger_signature="merchant-nearest",
            source="test",
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(1785006840, event.occurred_at)

    def test_merchant_clock_does_not_cross_capture_boundary(self) -> None:
        trigger = int(
            datetime(
                2026,
                7,
                26,
                3,
                30,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
        )
        events, reason = self.parser.parse_all(
            "03:23 入驻申请进展通知"
            + OCR_CAPTURE_SEPARATOR
            + "03:14 收款通知 ¥192.58 商品名称：店铺",
            trigger_time=trigger,
            trigger_signature="merchant-boundary",
            source="test",
            ignore_freshness=True,
        )
        self.assertEqual("", reason)
        self.assertEqual(1, len(events))
        self.assertEqual(1785006840, events[0][0].occurred_at)

    def test_merchant_same_amount_different_minutes_stays_distinct(self) -> None:
        trigger = int(
            datetime(
                2026,
                7,
                26,
                3,
                14,
                20,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            ).timestamp()
        )
        events, reason = self.parser.parse_all(
            "03:02 收款通知 ¥192.58 商品名称：店铺 "
            "03:14 收款通知 ¥192.58 商品名称：店铺",
            trigger_time=trigger,
            trigger_signature="merchant-batch",
            source="test",
            ignore_freshness=True,
        )
        self.assertEqual("", reason)
        self.assertEqual(
            [1785006120, 1785006840],
            [event.occurred_at for event, _ in events],
        )


class ConfigTests(unittest.TestCase):
    def test_requires_https_for_remote_hosts(self) -> None:
        config = base_config()
        config["bridge"]["url"] = "http://example.test/event"
        with patch.dict(os.environ, {"TEST_RECEIVER_TOKEN": "secret"}, clear=False):
            with self.assertRaises(ValueError):
                validate_config(config, "linux")

    def test_allows_explicit_loopback_http(self) -> None:
        config = base_config()
        config["bridge"].update({"url": "http://127.0.0.1:8787/event", "allow_http_localhost": True})
        with patch.dict(os.environ, {"TEST_RECEIVER_TOKEN": "secret"}, clear=False):
            validate_config(config, "linux")

    def test_inline_secret_is_opt_in(self) -> None:
        config = base_config()
        config["bridge"].update({"token_env": "MISSING_TEST_TOKEN", "token": "secret"})
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                validate_config(config, "linux")
            config["bridge"]["allow_inline_secret"] = True
            validate_config(config, "linux")


class StorageTests(unittest.TestCase):
    def event(self) -> PaymentEvent:
        return PaymentEvent(
            event_id="evt_1234567890abcdef",
            provider="wxpay",
            channel_id="7821",
            amount="18.88",
            occurred_at=1784644380,
            external_txn_id=None,
            trade_no=None,
            payer=None,
            raw_text="receipt",
            source="test",
            agent_id="test-agent",
        )

    def test_spool_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = EventSpool(Path(directory))
            spool.put(self.event())
            self.assertEqual([self.event()], spool.load())
            spool.acknowledge(self.event())
            self.assertFalse((spool.pending / f"{self.event().event_id}.json").exists())
            self.assertTrue((spool.processed / f"{self.event().event_id}.json").exists())

    def test_dedupe_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dedupe.json"
            first = ReceiptDedupe(path)
            first.add("receipt")
            second = ReceiptDedupe(path)
            self.assertTrue(second.contains("receipt"))

    def test_reject_keeps_event_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            spool = EventSpool(Path(directory))
            spool.put(self.event())
            spool.reject(self.event(), "no_candidate_expired")
            self.assertFalse((spool.pending / f"{self.event().event_id}.json").exists())
            self.assertTrue((spool.rejected / f"{self.event().event_id}.json").exists())
            reason = (
                spool.rejected / f"{self.event().event_id}.reason.txt"
            ).read_text(encoding="utf-8")
            self.assertIn("no_candidate_expired", reason)

    def test_runtime_archives_expired_no_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = base_config()
            config["_config_dir"] = directory
            config["runtime"] = {
                "spool_dir": "spool",
                "dedupe_file": "dedupe.json",
                "no_candidate_max_age_seconds": 300,
                "retry_max_age_seconds": 86400,
            }
            old_event = PaymentEvent(
                **{
                    **self.event().payload(),
                    "occurred_at": int(time.time()) - 301,
                }
            )
            with patch.dict(os.environ, {"TEST_RECEIVER_TOKEN": "secret"}, clear=False):
                runtime = AgentRuntime(config)
            runtime.queue(old_event, "old-receipt")
            runtime.pending[old_event.event_id] = (
                old_event,
                0.0,
                1,
                "no_candidate",
            )
            runtime.deliver_due(time.monotonic())
            self.assertEqual({}, runtime.pending)
            self.assertTrue(
                (runtime.spool.rejected / f"{old_event.event_id}.json").exists()
            )

    def test_trigger_glob(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "user" / "db" / "biz_message_0.db-wal"
            target.parent.mkdir(parents=True)
            target.write_text("x")
            config = {"_config_dir": str(root)}
            matches = discover_trigger_files(["**/biz_message_0.db-wal"], config)
            self.assertEqual([target.resolve()], matches)


if __name__ == "__main__":
    unittest.main()
