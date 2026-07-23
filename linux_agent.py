#!/usr/bin/env python3
"""Linux agent: WAL-triggered X11 capture + Tesseract OCR."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from receiver_core import (
    AgentRuntime,
    OCR_CAPTURE_SEPARATOR,
    ReceiptParser,
    discover_trigger_files,
    file_signature,
    load_json,
    normalize_ocr_text,
    resolve_path,
    setup_logging,
    validate_config,
)


LOG = logging.getLogger("wechat-payment-receiver.linux")


class LinuxCapture:
    def __init__(self, config: Mapping[str, Any], full_config: Mapping[str, Any]):
        self.display = str(config.get("display") or ":88")
        self.window_name_regex = str(config.get("window_name_regex") or "^微信收款助手$")
        self.window_probe = str(config.get("window_probe") or "xdotool")
        self.capture_tool = str(config.get("capture_tool") or "import")
        self.ocr_tool = str(config.get("ocr_tool") or "tesseract")
        self.ocr_language = str(config.get("ocr_language") or "chi_sim+eng")
        self.ocr_psm = int(config.get("ocr_psm", 6))
        self.scroll_down_clicks = max(0, int(config.get("scroll_down_clicks", 40)))
        self.window_width = max(620, int(config.get("window_width", 720)))
        self.window_height = max(660, int(config.get("window_height", 860)))
        self.window_x = max(0, int(config.get("window_x", 0)))
        self.window_y = max(0, int(config.get("window_y", 0)))
        self.keep_screenshots = config.get("keep_screenshots") is True
        self.capture_dir = resolve_path(str(config.get("capture_dir") or "captures"), full_config)
        self.capture_dir.mkdir(parents=True, exist_ok=True)

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        return env

    def _run(self, args: Sequence[str], timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args), text=True, capture_output=True, timeout=timeout, check=False, env=self._environment()
        )

    def find_window(self) -> str | None:
        result = self._run([self.window_probe, "search", "--name", self.window_name_regex], timeout=8)
        ids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        return ids[-1] if ids else None

    def capture(self, scroll_up_clicks: int) -> tuple[str, str] | None:
        window_id = self.find_window()
        if not window_id:
            LOG.warning("collection_window_missing regex=%s", self.window_name_regex)
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
        screenshot = self.capture_dir / f"receipt-{stamp}.png"
        self._run([self.window_probe, "windowactivate", "--sync", window_id], timeout=10)
        self._run([
            self.window_probe, "windowsize", "--sync", window_id,
            str(self.window_width), str(self.window_height),
        ], timeout=10)
        self._run([
            self.window_probe, "windowmove", "--sync", window_id,
            str(self.window_x), str(self.window_y),
        ], timeout=10)
        self._run([self.window_probe, "mousemove", "--window", window_id, "300", "350"], timeout=10)
        if self.scroll_down_clicks:
            self._run([
                self.window_probe, "click", "--repeat", str(self.scroll_down_clicks),
                "--delay", "15", "5",
            ], timeout=15)
        if scroll_up_clicks:
            self._run([
                self.window_probe, "click", "--repeat", str(scroll_up_clicks),
                "--delay", "80", "4",
            ], timeout=15)
        time.sleep(0.5)
        capture = self._run([self.capture_tool, "-window", window_id, str(screenshot)], timeout=15)
        if capture.returncode != 0 or not screenshot.exists():
            LOG.warning("capture_failed error=%s", capture.stderr[:300])
            return None
        try:
            ocr = self._run([
                self.ocr_tool, str(screenshot), "stdout", "-l", self.ocr_language,
                "--psm", str(self.ocr_psm),
            ], timeout=30)
            if ocr.returncode != 0:
                LOG.warning("ocr_failed error=%s", ocr.stderr[:300])
                return None
            return ocr.stdout, window_id
        finally:
            if not self.keep_screenshots:
                screenshot.unlink(missing_ok=True)
            if self.scroll_down_clicks:
                self._run([
                    self.window_probe, "click", "--repeat", str(self.scroll_down_clicks),
                    "--delay", "15", "5",
                ], timeout=15)


def attempt_plan(platform: Mapping[str, Any]) -> list[dict[str, float | int]]:
    raw = platform.get("capture_attempts")
    if not isinstance(raw, list) or not raw:
        return [
            {"delay_seconds": 2.0, "scroll_up_clicks": 0},
            {"delay_seconds": 5.0, "scroll_up_clicks": 2},
            {"delay_seconds": 9.0, "scroll_up_clicks": 4},
        ]
    result: list[dict[str, float | int]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("linux.capture_attempts entries must be objects")
        result.append({
            "delay_seconds": max(0.5, float(row.get("delay_seconds", 2))),
            "scroll_up_clicks": max(0, int(row.get("scroll_up_clicks", 0))),
        })
    return sorted(result, key=lambda row: float(row["delay_seconds"]))


def run(config: Mapping[str, Any], once: bool = False) -> int:
    platform = config["linux"]
    trigger_paths = discover_trigger_files(platform["trigger_files"], config)
    signatures = {path: file_signature(path) for path in trigger_paths}
    parser = ReceiptParser(config)
    capture = LinuxCapture(platform, config)
    runtime = AgentRuntime(config)
    plan = attempt_plan(platform)
    poll_seconds = max(0.2, float(config.get("runtime", {}).get("poll_seconds", 0.5)))
    trigger_quiet_seconds = max(0.0, float(platform.get("trigger_quiet_seconds", 0.8)))
    active: dict[str, Any] | None = None
    last_heartbeat = 0.0
    LOG.info("agent_started platform=linux agent_id=%s triggers=%s restored_pending=%s window=%s",
             config["agent"]["id"], len(trigger_paths), len(runtime.pending), bool(capture.find_window()))
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
                    "texts": [],
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
                captured = capture.capture(int(row["scroll_up_clicks"]))
                reason = "capture_failed"
                if captured:
                    text, window_id = captured
                    active["texts"].append(text)
                    event, receipt_key = parser.parse(
                        OCR_CAPTURE_SEPARATOR.join(active["texts"]),
                        trigger_time=int(active["wall"]),
                        trigger_signature=str(active["signature"]),
                        source="wechat-linux-wal-ocr",
                    )
                    if event:
                        LOG.info("ocr_candidate attempt=%s window=%s amount=%s occurred_at=%s",
                                 index + 1, window_id, event.amount, event.occurred_at)
                        runtime.queue(event, receipt_key)
                        active = None
                    else:
                        reason = receipt_key
                        LOG.info("ocr_attempt_miss attempt=%s reason=%s text_sha256=%s", index + 1, reason,
                                 hashlib.sha256(normalize_ocr_text(text).encode()).hexdigest()[:16])
                if active:
                    index += 1
                    if index >= len(plan):
                        LOG.error("ocr_attempts_exhausted reason=%s", reason)
                        active = None
                    else:
                        active["index"] = index

        runtime.deliver_due(now_mono)
        if now_mono - last_heartbeat >= 60:
            LOG.info("heartbeat platform=linux window=%s pending=%s", bool(capture.find_window()), len(runtime.pending))
            last_heartbeat = now_mono
        time.sleep(poll_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="Linux WeChat payment receiver")
    cli.add_argument("--config", required=True)
    cli.add_argument("--once", action="store_true")
    cli.add_argument("--verbose", action="store_true")
    args = cli.parse_args(argv)
    config = load_json(args.config)
    validate_config(config, "linux")
    setup_logging(config, args.verbose)
    return run(config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
