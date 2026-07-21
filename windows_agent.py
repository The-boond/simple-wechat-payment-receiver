#!/usr/bin/env python3
"""Windows agent: WAL-triggered, allowlisted Windows OCR helper."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from receiver_core import (
    AgentRuntime,
    ReceiptParser,
    discover_trigger_files,
    file_signature,
    load_json,
    normalize_ocr_text,
    resolve_path,
    setup_logging,
    validate_config,
)


LOG = logging.getLogger("wechat-payment-receiver.windows")


class WindowsCapture:
    def __init__(self, config: Mapping[str, Any], full_config: Mapping[str, Any]):
        self.powershell = str(config.get("powershell") or "powershell.exe")
        self.script = resolve_path(str(config.get("ocr_script") or "scripts/windows/wechat_ocr.ps1"), full_config)
        self.process_names = [str(value) for value in config.get("process_names", ["Weixin", "WeChat"])]
        self.window_title_regex = str(config.get("window_title_regex") or "收款助手|微信|Weixin|WeChat")
        self.include_notifications = config.get("include_notifications") is True
        self.notification_app_regex = str(config.get("notification_app_regex") or "微信|Weixin|WeChat")
        self.allow_window_restore = config.get("allow_window_restore") is True
        self.timeout_seconds = float(config.get("ocr_timeout_seconds", 30))

    def capture(self, request_restore: bool) -> list[dict[str, Any]]:
        args = [
            self.powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script),
            "-ProcessNames",
            ",".join(self.process_names),
            "-WindowTitlePattern",
            self.window_title_regex,
            "-NotificationAppPattern",
            self.notification_app_regex,
        ]
        if self.include_notifications:
            args.append("-IncludeNotifications")
        if request_restore and self.allow_window_restore:
            args.append("-AllowWindowRestore")
        try:
            result = subprocess.run(args, text=True, capture_output=True, timeout=self.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("ocr_helper_failed error=%s", str(exc)[:300])
            return []
        if result.returncode != 0:
            LOG.warning("ocr_helper_failed code=%s error=%s", result.returncode, result.stderr[:300])
            return []
        try:
            value = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            LOG.warning("ocr_helper_invalid_json sha256=%s", hashlib.sha256(result.stdout.encode()).hexdigest()[:16])
            return []
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def attempt_plan(platform: Mapping[str, Any]) -> list[dict[str, float | bool]]:
    raw = platform.get("capture_attempts")
    if not isinstance(raw, list) or not raw:
        return [
            {"delay_seconds": 2.0, "request_restore": False},
            {"delay_seconds": 6.0, "request_restore": True},
            {"delay_seconds": 12.0, "request_restore": True},
        ]
    result: list[dict[str, float | bool]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("windows.capture_attempts entries must be objects")
        result.append({
            "delay_seconds": max(0.5, float(row.get("delay_seconds", 2))),
            "request_restore": row.get("request_restore") is True,
        })
    return sorted(result, key=lambda row: float(row["delay_seconds"]))


def run(config: Mapping[str, Any], once: bool = False) -> int:
    platform = config["windows"]
    trigger_paths = discover_trigger_files(platform["trigger_files"], config)
    signatures = {path: file_signature(path) for path in trigger_paths}
    parser = ReceiptParser(config)
    capture = WindowsCapture(platform, config)
    runtime = AgentRuntime(config)
    plan = attempt_plan(platform)
    poll_seconds = max(0.2, float(config.get("runtime", {}).get("poll_seconds", 0.5)))
    trigger_quiet_seconds = max(0.0, float(platform.get("trigger_quiet_seconds", 0.8)))
    active: dict[str, Any] | None = None
    last_heartbeat = 0.0
    LOG.info("agent_started platform=windows agent_id=%s triggers=%s restored_pending=%s",
             config["agent"]["id"], len(trigger_paths), len(runtime.pending))
    if once:
        return 0

    while True:
        now_mono = time.monotonic()
        changed: list[tuple[Path, tuple[int, int]]] = []
        for path in trigger_paths:
            current = file_signature(path)
            prior = signatures.get(path)
            if prior is not None and current is not None and current != prior:
                changed.append((path, current))
            signatures[path] = current
        if changed:
            wall = int(time.time())
            joined = "|".join(f"{path}:{sig[0]}:{sig[1]}" for path, sig in changed)
            if active is None:
                active = {
                    "wall": wall,
                    "signature": joined,
                    "index": 0,
                    "started": now_mono,
                    "not_before": now_mono + trigger_quiet_seconds,
                }
            else:
                active["wall"] = wall
                active["signature"] = f"{active['signature']}|{joined}"
                active["not_before"] = now_mono + trigger_quiet_seconds
            LOG.info("ocr_trigger files=%s", ",".join(str(path) for path, _ in changed))

        if active:
            index = int(active["index"])
            row = plan[index]
            due = max(
                float(active["started"]) + float(row["delay_seconds"]),
                float(active["not_before"]),
            )
            if now_mono >= due:
                rows = capture.capture(bool(row["request_restore"]))
                miss_reasons: list[str] = []
                matched = False
                for captured in rows:
                    text = str(captured.get("text") or "")
                    event, receipt_key = parser.parse(
                        text,
                        trigger_time=int(active["wall"]),
                        trigger_signature=str(active["signature"]),
                        source="wechat-windows-wal-ocr",
                    )
                    if event:
                        LOG.info("ocr_candidate attempt=%s mode=%s amount=%s occurred_at=%s", index + 1,
                                 captured.get("capture_mode", "unknown"), event.amount, event.occurred_at)
                        runtime.queue(event, receipt_key)
                        matched = True
                        break
                    miss_reasons.append(receipt_key)
                    LOG.debug("ocr_row_miss reason=%s text_sha256=%s", receipt_key,
                              hashlib.sha256(normalize_ocr_text(text).encode()).hexdigest()[:16])
                if matched:
                    active = None
                else:
                    LOG.info("ocr_attempt_miss attempt=%s rows=%s reasons=%s", index + 1, len(rows),
                             ",".join(sorted(set(miss_reasons))) or "no_rows")
                    index += 1
                    if index >= len(plan):
                        LOG.error("ocr_attempts_exhausted")
                        active = None
                    else:
                        active["index"] = index

        runtime.deliver_due(now_mono)
        if now_mono - last_heartbeat >= 60:
            LOG.info("heartbeat platform=windows pending=%s", len(runtime.pending))
            last_heartbeat = now_mono
        time.sleep(poll_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="Windows WeChat payment receiver")
    cli.add_argument("--config", required=True)
    cli.add_argument("--once", action="store_true")
    cli.add_argument("--verbose", action="store_true")
    args = cli.parse_args(argv)
    config = load_json(args.config)
    validate_config(config, "windows")
    setup_logging(config, args.verbose)
    return run(config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
