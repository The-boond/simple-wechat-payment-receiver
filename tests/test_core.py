from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from receiver_core import (
    EventSpool,
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

    def test_event_id_changes_with_trigger_signature(self) -> None:
        one, _ = self.parser.parse(self.text, trigger_time=self.trigger, trigger_signature="a", source="test")
        two, _ = self.parser.parse(self.text, trigger_time=self.trigger, trigger_signature="b", source="test")
        assert one and two
        self.assertNotEqual(one.event_id, two.event_id)


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
