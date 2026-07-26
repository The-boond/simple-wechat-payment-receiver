#!/usr/bin/env python3
"""Linux agent: durable WAL triggers, fast X11 capture and bounded OCR."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo

from receiver_core import (
    AgentRuntime,
    CaptureTriggerStore,
    FileSignature,
    OCR_CAPTURE_SEPARATOR,
    PaymentEvent,
    ReceiptParser,
    ServiceNotificationReceipt,
    ServiceNotificationState,
    VisualAmount,
    VisualClock,
    VisualToken,
    associate_visual_receipts,
    discover_trigger_files,
    file_signature,
    format_file_signature,
    load_json,
    merchant_parser_text,
    normalize_ocr_text,
    parse_tesseract_tsv,
    resolve_path,
    service_notification_receipts,
    setup_logging,
    trigger_file_change,
    validate_config,
    visual_amount_from_token,
    visual_clock_from_token,
)


LOG = logging.getLogger("wechat-payment-receiver.linux")


@dataclass(frozen=True)
class CapturedFrame:
    window_label: str
    window_id: str
    screenshot: Path
    attempt: int
    scroll_up_clicks: int


@dataclass(frozen=True)
class OcrFrameResult:
    frame: CapturedFrame
    text: str | None
    elapsed_ms: int
    error: str | None


@dataclass(frozen=True)
class ServiceNotificationScanResult:
    """Outcome of one overlap scan of the Service Notifications chat."""

    available: bool
    successful: bool
    receipts: tuple[ServiceNotificationReceipt, ...]
    frames: int
    anchor_found: bool
    coverage_reason: str


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def command(
    args: Sequence[str],
    *,
    timeout: float = 20.0,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a text subprocess and turn timeouts/start failures into results."""

    command_args = list(args)
    try:
        return subprocess.run(
            command_args,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        LOG.warning(
            "subprocess_timeout command=%s timeout_seconds=%s",
            command_args[0] if command_args else "",
            timeout,
        )
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(
            command_args,
            124,
            stdout,
            (stderr + f"\ncommand timed out after {timeout} seconds").strip(),
        )
    except OSError as exc:
        LOG.warning(
            "subprocess_start_failed command=%s error=%s",
            command_args[0] if command_args else "",
            exc,
        )
        return subprocess.CompletedProcess(command_args, 127, "", str(exc))


def attempt_plan(platform: Mapping[str, Any]) -> list[dict[str, float | int]]:
    """Return the legacy retry plan used by older configurations/tools."""

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
        result.append(
            {
                "delay_seconds": max(0.5, float(row.get("delay_seconds", 2))),
                "scroll_up_clicks": max(
                    0,
                    int(row.get("scroll_up_clicks", 0)),
                ),
            }
        )
    return sorted(result, key=lambda row: float(row["delay_seconds"]))


class LinuxCapture:
    """Capture every scroll frame first, then OCR immutable screenshots."""

    def __init__(self, config: Mapping[str, Any], full_config: Mapping[str, Any]):
        self.display = str(config.get("display") or ":88")
        raw_window_regexes = config.get("window_name_regexes")
        if isinstance(raw_window_regexes, list):
            window_regexes = [
                str(value).strip()
                for value in raw_window_regexes
                if str(value).strip()
            ]
        else:
            window_regexes = []
        if not window_regexes:
            window_regexes = [
                str(config.get("window_name_regex") or "^微信收款助手$").strip()
            ]
        self.window_name_regexes = tuple(dict.fromkeys(window_regexes))
        self.window_name_regex = self.window_name_regexes[0]

        self.window_probe = str(config.get("window_probe") or "xdotool")
        self.capture_tool = str(config.get("capture_tool") or "import")
        self.convert_tool = str(config.get("convert_tool") or "convert")
        self.ocr_tool = str(config.get("ocr_tool") or "tesseract")
        self.ocr_language = str(config.get("ocr_language") or "chi_sim+eng")
        self.ocr_psm = max(3, min(13, int(config.get("ocr_psm", 6))))
        self.ocr_workers = max(1, min(2, int(config.get("ocr_workers", 1))))

        self.window_width = max(620, int(config.get("window_width", 720)))
        self.window_height = max(660, int(config.get("window_height", 860)))
        self.window_x = max(0, int(config.get("window_x", 0)))
        self.window_y = max(0, int(config.get("window_y", 0)))
        self.scroll_down_clicks = max(
            0,
            int(config.get("scroll_down_clicks", 40)),
        )
        raw_scroll_attempts = config.get("scroll_up_attempts")
        if isinstance(raw_scroll_attempts, list) and raw_scroll_attempts:
            scroll_attempts = [max(0, int(value)) for value in raw_scroll_attempts]
        else:
            scroll_attempts = [
                int(row["scroll_up_clicks"]) for row in attempt_plan(config)
            ]
        self.scroll_up_attempts = tuple(dict.fromkeys(scroll_attempts))
        self.adaptive_scroll_enabled = _as_bool(
            config.get("adaptive_scroll_enabled"),
            default=True,
        )
        self.adaptive_scroll_clicks = max(
            1,
            int(config.get("adaptive_scroll_clicks", 4)),
        )
        self.adaptive_scroll_max_frames = max(
            3,
            min(12, int(config.get("adaptive_scroll_max_frames", 6))),
        )

        self.capture_timeout = max(
            3.0,
            min(60.0, float(config.get("capture_timeout_seconds", 15))),
        )
        self.ocr_timeout = max(
            5.0,
            min(120.0, float(config.get("ocr_timeout_seconds", 30))),
        )
        self.probe_timeout = max(
            3.0,
            min(60.0, float(config.get("probe_timeout_seconds", 15))),
        )

        raw_thresholds = config.get("clock_probe_thresholds", [82, 76, 88])
        if not isinstance(raw_thresholds, list):
            raw_thresholds = [raw_thresholds]
        thresholds: list[int] = []
        for value in raw_thresholds:
            try:
                thresholds.append(max(55, min(95, int(value))))
            except (TypeError, ValueError):
                continue
        self.clock_probe_thresholds = tuple(dict.fromkeys(thresholds or [82]))
        self.screen_probe_hamming_threshold = max(
            0,
            min(16, int(config.get("screen_probe_hamming_threshold", 3))),
        )

        self.keep_screenshots = _as_bool(config.get("keep_screenshots"), False)
        self.capture_dir = resolve_path(
            str(config.get("capture_dir") or "captures"),
            full_config,
        )
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_retention_seconds = max(
            3600,
            int(float(config.get("screenshot_retention_days", 7)) * 86400),
        )
        self.screenshot_max_files = max(
            1,
            int(config.get("screenshot_max_files", 2000)),
        )
        service = config.get("service_notifications", {})
        if not isinstance(service, Mapping):
            service = {}
        self.service_notifications_enabled = _as_bool(
            service.get("enabled"),
            default=False,
        )
        self.service_window_regex = str(
            service.get("window_name_regex") or "^Weixin$"
        ).strip()
        self.service_overlay_regex = str(
            service.get("close_overlay_name_regex") or "^微信收款商业版$"
        ).strip()
        self.service_window_width = max(
            760,
            int(service.get("window_width", 1020)),
        )
        self.service_window_height = max(
            700,
            int(service.get("window_height", 860)),
        )
        self.service_window_x = max(0, int(service.get("window_x", 420)))
        self.service_window_y = max(0, int(service.get("window_y", 20)))
        self.service_pointer_x = max(0, int(service.get("pointer_x", 760)))
        self.service_pointer_y = max(0, int(service.get("pointer_y", 430)))
        self.service_ocr_crop_x = max(0, int(service.get("ocr_crop_x", 400)))
        self.service_ocr_crop_y = max(0, int(service.get("ocr_crop_y", 40)))
        self.service_ocr_crop_width = max(
            320,
            int(service.get("ocr_crop_width", 580)),
        )
        self.service_ocr_crop_height = max(
            480,
            int(service.get("ocr_crop_height", 810)),
        )
        self.service_scroll_up_clicks = max(
            1,
            int(service.get("scroll_up_clicks", 6)),
        )
        self.service_scroll_down_clicks = max(
            30,
            int(service.get("scroll_down_clicks", 100)),
        )
        self.service_scan_batch_frames = max(
            1,
            min(8, int(service.get("scan_batch_frames", 4))),
        )
        self.service_baseline_frames = max(
            2,
            min(32, int(service.get("baseline_frames", 8))),
        )
        self.service_scan_max_frames = max(
            self.service_baseline_frames,
            min(256, int(service.get("scan_max_frames", 160))),
        )
        self.service_settle_seconds = max(
            0.1,
            float(service.get("settle_seconds", 0.30)),
        )
        self.service_tail_recheck_seconds = max(
            0.1,
            float(service.get("tail_recheck_seconds", 0.45)),
        )

        self.last_capture_successful = False
        self.last_capture_frames = 0
        self.last_capture_failure_reason: str | None = None
        self.last_bottom_fingerprint: str | None = None

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        return env

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float = 20.0,
    ) -> subprocess.CompletedProcess[str]:
        return command(args, timeout=timeout, env=self._environment())

    def _find_window(self, regex: str) -> str | None:
        result = self._run(
            [self.window_probe, "search", "--name", regex],
            timeout=8,
        )
        ids = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().isdigit()
        ]
        return ids[-1] if ids else None

    def find_windows(self) -> list[tuple[str, str]]:
        windows: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for regex in self.window_name_regexes:
            window_id = self._find_window(regex)
            if window_id and window_id not in seen_ids:
                seen_ids.add(window_id)
                windows.append((regex, window_id))
        return windows

    def find_window(self) -> str | None:
        windows = self.find_windows()
        return windows[0][1] if windows else None

    def _xdotool(self, *args: str) -> bool:
        result = self._run([self.window_probe, *args], timeout=10)
        if result.returncode == 0:
            return True
        LOG.warning(
            "wechat_xdotool_failed args=%s error=%s",
            args,
            result.stderr[:500],
        )
        return False

    def _window_geometry(self, window_id: str) -> dict[str, int] | None:
        result = self._run(
            [
                self.window_probe,
                "getwindowgeometry",
                "--shell",
                window_id,
            ],
            timeout=8,
        )
        if result.returncode != 0:
            return None
        values: dict[str, int] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"X", "Y", "WIDTH", "HEIGHT"}:
                try:
                    values[key] = int(value)
                except ValueError:
                    return None
        return values if len(values) == 4 else None

    def _capture_service_window(
        self,
        screenshot: Path,
    ) -> tuple[bool, str | None]:
        """Capture the compositor root, then crop the positioned Weixin window."""

        root = screenshot.with_name("." + screenshot.stem + "-root.png")
        try:
            captured = self._run(
                [self.capture_tool, "-window", "root", str(root)],
                timeout=self.capture_timeout,
            )
            if captured.returncode != 0 or not root.exists():
                return False, captured.stderr[:500]
            cropped = self._run(
                [
                    self.convert_tool,
                    str(root),
                    "-crop",
                    (
                        f"{self.service_window_width}x{self.service_window_height}"
                        f"+{self.service_window_x}+{self.service_window_y}"
                    ),
                    "+repage",
                    str(screenshot),
                ],
                timeout=max(self.capture_timeout, 20),
            )
            if cropped.returncode != 0 or not screenshot.exists():
                return False, cropped.stderr[:500]
            return True, None
        finally:
            root.unlink(missing_ok=True)

    def _service_visual_fingerprint_file(
        self,
        screenshot: Path,
    ) -> str | None:
        result = self._run_binary(
            [
                self.convert_tool,
                str(screenshot),
                "-crop",
                (
                    f"{self.service_ocr_crop_width}x"
                    f"{self.service_ocr_crop_height}"
                    f"+{self.service_ocr_crop_x}+{self.service_ocr_crop_y}"
                ),
                "+repage",
                "-resize",
                "64x64!",
                "-colorspace",
                "Gray",
                "-depth",
                "8",
                "gray:-",
            ],
            timeout=self.probe_timeout,
        )
        if result is None or result.returncode != 0 or not result.stdout:
            return None
        return hashlib.sha256(result.stdout).hexdigest()

    def _ocr_service_frame(
        self,
        screenshot: Path,
    ) -> tuple[Path, str | None, int, str | None]:
        """OCR only the detailed receipt-card pane of an immutable screenshot."""

        enhanced = screenshot.with_name(
            screenshot.stem + "-service-ocr.png"
        )
        started = time.monotonic()
        try:
            prepared = self._run(
                [
                    self.convert_tool,
                    str(screenshot),
                    "-crop",
                    (
                        f"{self.service_ocr_crop_width}x"
                        f"{self.service_ocr_crop_height}"
                        f"+{self.service_ocr_crop_x}+{self.service_ocr_crop_y}"
                    ),
                    "+repage",
                    "-resize",
                    "150%",
                    str(enhanced),
                ],
                timeout=max(self.capture_timeout, 20),
            )
            if prepared.returncode != 0 or not enhanced.exists():
                return (
                    screenshot,
                    None,
                    int((time.monotonic() - started) * 1000),
                    prepared.stderr[:500],
                )
            result = self._run(
                [
                    self.ocr_tool,
                    str(enhanced),
                    "stdout",
                    "-l",
                    self.ocr_language,
                    "--psm",
                    str(self.ocr_psm),
                ],
                timeout=self.ocr_timeout,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.returncode != 0:
                return screenshot, None, elapsed_ms, result.stderr[:500]
            return screenshot, result.stdout, elapsed_ms, None
        finally:
            enhanced.unlink(missing_ok=True)

    def capture_service_notifications(
        self,
        *,
        state: ServiceNotificationState,
        trigger_time: int,
        timezone: ZoneInfo,
        baseline_only: bool,
    ) -> ServiceNotificationScanResult:
        """Scan overlapping cards until a persistent transaction anchor."""

        if not self.service_notifications_enabled:
            return ServiceNotificationScanResult(
                available=False,
                successful=False,
                receipts=(),
                frames=0,
                anchor_found=False,
                coverage_reason="disabled",
            )
        window_id = self._find_window(self.service_window_regex)
        if not window_id:
            LOG.warning(
                "wechat_service_window_missing regex=%s",
                self.service_window_regex,
            )
            return ServiceNotificationScanResult(
                available=False,
                successful=False,
                receipts=(),
                frames=0,
                anchor_found=False,
                coverage_reason="window_missing",
            )

        geometry = self._window_geometry(window_id)
        receipts: dict[str, ServiceNotificationReceipt] = {}
        seen_fingerprints: set[str] = set()
        known_ids = set(state.transaction_ids)
        captured_paths: list[Path] = []
        first_bottom_fingerprint: str | None = None
        frames = 0
        scrolled_clicks = 0
        anchor_found = False
        repeated_frame = False
        capture_error = False
        ocr_error = False
        bottom_restored = False
        coverage_reason = "incomplete"
        stamp = time.strftime("%Y%m%d-%H%M%S")

        overlay_id = (
            self._find_window(self.service_overlay_regex)
            if self.service_overlay_regex
            else None
        )
        if overlay_id and overlay_id != window_id:
            self._xdotool("windowclose", overlay_id)
            time.sleep(0.2)

        prepared = all(
            (
                self._xdotool("windowactivate", "--sync", window_id),
                self._xdotool(
                    "windowsize",
                    "--sync",
                    window_id,
                    str(self.service_window_width),
                    str(self.service_window_height),
                ),
                self._xdotool(
                    "windowmove",
                    "--sync",
                    window_id,
                    str(self.service_window_x),
                    str(self.service_window_y),
                ),
                self._xdotool(
                    "mousemove",
                    "--window",
                    window_id,
                    str(self.service_pointer_x),
                    str(self.service_pointer_y),
                ),
            )
        )
        if not prepared:
            capture_error = True

        try:
            # Pointer-directed wheel scrolling avoids clicking the receipt card.
            self._xdotool("key", "--window", window_id, "End")
            if not self._xdotool(
                "click",
                "--repeat",
                str(self.service_scroll_down_clicks),
                "--delay",
                "20",
                "5",
            ):
                capture_error = True
            time.sleep(self.service_settle_seconds)

            stop_scan = False
            while not stop_scan and frames < self.service_scan_max_frames:
                batch: list[Path] = []
                for _ in range(self.service_scan_batch_frames):
                    if frames > 0:
                        if not self._xdotool(
                            "click",
                            "--repeat",
                            str(self.service_scroll_up_clicks),
                            "--delay",
                            "70",
                            "4",
                        ):
                            capture_error = True
                            stop_scan = True
                            break
                        scrolled_clicks += self.service_scroll_up_clicks
                        time.sleep(self.service_settle_seconds)

                    screenshot = self.capture_dir / (
                        f"receipt-service-{stamp}-a{frames + 1}"
                        f"-up{scrolled_clicks}.png"
                    )
                    captured, error = self._capture_service_window(screenshot)
                    if not captured:
                        capture_error = True
                        stop_scan = True
                        LOG.warning(
                            "wechat_service_capture_failed attempt=%s error=%s",
                            frames + 1,
                            error,
                        )
                        break

                    fingerprint = self._service_visual_fingerprint_file(
                        screenshot
                    )
                    if frames == 0:
                        first_bottom_fingerprint = fingerprint
                    if fingerprint and fingerprint in seen_fingerprints:
                        screenshot.unlink(missing_ok=True)
                        repeated_frame = True
                        coverage_reason = "repeated_frame"
                        stop_scan = True
                        break
                    if fingerprint:
                        seen_fingerprints.add(fingerprint)
                    frames += 1
                    batch.append(screenshot)
                    captured_paths.append(screenshot)
                    LOG.info(
                        "wechat_service_capture attempt=%s scroll_up=%s "
                        "screenshot=%s",
                        frames,
                        scrolled_clicks,
                        screenshot,
                    )
                    if baseline_only and frames >= self.service_baseline_frames:
                        stop_scan = True
                        coverage_reason = "baseline_recent_frames"
                        break
                    if frames >= self.service_scan_max_frames:
                        stop_scan = True
                        coverage_reason = "safety_fuse"
                        break

                if batch:
                    with ThreadPoolExecutor(
                        max_workers=self.ocr_workers,
                        thread_name_prefix="wechat-service-ocr",
                    ) as executor:
                        results = list(
                            executor.map(self._ocr_service_frame, batch)
                        )
                    for screenshot, text, elapsed_ms, error in results:
                        if error is not None or text is None:
                            ocr_error = True
                            LOG.warning(
                                "wechat_service_ocr_failed screenshot=%s "
                                "error=%s",
                                screenshot,
                                error,
                            )
                            continue
                        parsed = service_notification_receipts(
                            text,
                            trigger_time=trigger_time,
                            timezone=timezone,
                        )
                        for receipt in parsed:
                            receipts.setdefault(
                                receipt.external_txn_id,
                                receipt,
                            )
                        LOG.info(
                            "wechat_service_ocr screenshot=%s elapsed_ms=%s "
                            "receipts=%s",
                            screenshot,
                            elapsed_ms,
                            len(parsed),
                        )

                if state.initialized and any(
                    transaction_id in known_ids for transaction_id in receipts
                ):
                    anchor_found = True
                    coverage_reason = "known_transaction_anchor"
                    stop_scan = True
                elif repeated_frame:
                    stop_scan = True

            if not baseline_only and not anchor_found and not repeated_frame:
                if frames >= self.service_scan_max_frames:
                    coverage_reason = "safety_fuse"
                elif coverage_reason == "incomplete":
                    coverage_reason = "anchor_not_found"
        finally:
            self._xdotool(
                "mousemove",
                "--window",
                window_id,
                str(self.service_pointer_x),
                str(self.service_pointer_y),
            )
            self._xdotool("key", "--window", window_id, "End")
            restore_clicks = max(
                self.service_scroll_down_clicks,
                scrolled_clicks + self.service_scroll_up_clicks * 3,
            )
            bottom_restored = self._xdotool(
                "click",
                "--repeat",
                str(restore_clicks),
                "--delay",
                "20",
                "5",
            )
            if bottom_restored:
                time.sleep(self.service_tail_recheck_seconds)
                tail = self.capture_dir / (
                    f"receipt-service-{stamp}-tail-up0.png"
                )
                captured, error = self._capture_service_window(tail)
                if not captured:
                    capture_error = True
                    LOG.warning(
                        "wechat_service_tail_capture_failed error=%s",
                        error,
                    )
                else:
                    tail_fingerprint = self._service_visual_fingerprint_file(
                        tail
                    )
                    if (
                        first_bottom_fingerprint
                        and tail_fingerprint == first_bottom_fingerprint
                    ):
                        tail.unlink(missing_ok=True)
                    else:
                        captured_paths.append(tail)
                        (
                            _screenshot,
                            tail_text,
                            elapsed_ms,
                            tail_error,
                        ) = self._ocr_service_frame(tail)
                        if tail_error is not None or tail_text is None:
                            ocr_error = True
                            LOG.warning(
                                "wechat_service_tail_ocr_failed error=%s",
                                tail_error,
                            )
                        else:
                            frames += 1
                            parsed = service_notification_receipts(
                                tail_text,
                                trigger_time=trigger_time,
                                timezone=timezone,
                            )
                            for receipt in parsed:
                                receipts.setdefault(
                                    receipt.external_txn_id,
                                    receipt,
                                )
                            LOG.info(
                                "wechat_service_tail_ocr elapsed_ms=%s "
                                "receipts=%s",
                                elapsed_ms,
                                len(parsed),
                            )
            else:
                capture_error = True

            if geometry is not None:
                restored_geometry = all(
                    (
                        self._xdotool(
                            "windowsize",
                            "--sync",
                            window_id,
                            str(geometry["WIDTH"]),
                            str(geometry["HEIGHT"]),
                        ),
                        self._xdotool(
                            "windowmove",
                            "--sync",
                            window_id,
                            str(geometry["X"]),
                            str(geometry["Y"]),
                        ),
                    )
                )
                if not restored_geometry:
                    capture_error = True
            if not self.keep_screenshots:
                for screenshot in captured_paths:
                    screenshot.unlink(missing_ok=True)

        ordered_receipts = tuple(
            sorted(
                receipts.values(),
                key=lambda item: (item.occurred_at, item.external_txn_id),
                reverse=True,
            )
        )
        coverage_complete = (
            (baseline_only and coverage_reason == "baseline_recent_frames")
            or anchor_found
            or repeated_frame
        )
        if baseline_only and not ordered_receipts:
            coverage_complete = False
            coverage_reason = "baseline_no_complete_receipt"
        successful = bool(
            frames > 0
            and coverage_complete
            and bottom_restored
            and not capture_error
            and not ocr_error
        )
        LOG.info(
            "wechat_service_scan_complete success=%s frames=%s receipts=%s "
            "anchor=%s reason=%s",
            successful,
            frames,
            len(ordered_receipts),
            anchor_found,
            coverage_reason,
        )
        return ServiceNotificationScanResult(
            available=True,
            successful=successful,
            receipts=ordered_receipts,
            frames=frames,
            anchor_found=anchor_found,
            coverage_reason=coverage_reason,
        )

    def _prepare_window(self, window_id: str) -> bool:
        return all(
            (
                self._xdotool("windowactivate", "--sync", window_id),
                self._xdotool(
                    "windowsize",
                    "--sync",
                    window_id,
                    str(self.window_width),
                    str(self.window_height),
                ),
                self._xdotool(
                    "windowmove",
                    "--sync",
                    window_id,
                    str(self.window_x),
                    str(self.window_y),
                ),
                self._xdotool(
                    "mousemove",
                    "--window",
                    window_id,
                    "300",
                    "350",
                ),
            )
        )

    def _return_to_bottom(self, window_id: str, window_label: str) -> bool:
        moved = self._xdotool(
            "mousemove",
            "--window",
            window_id,
            "300",
            "350",
        )
        end_sent = self._xdotool("key", "--window", window_id, "End")
        restore_clicks = max(
            self.scroll_down_clicks,
            self.adaptive_scroll_clicks
            * (self.adaptive_scroll_max_frames + 1),
            max(self.scroll_up_attempts, default=0)
            + self.adaptive_scroll_clicks,
            8,
        )
        scrolled = self._xdotool(
            "click",
            "--repeat",
            str(restore_clicks),
            "--delay",
            "35",
            "5",
        )
        restored = bool(moved and scrolled)
        LOG.info(
            "wechat_scroll_bottom_restored window=%s clicks=%s "
            "end_sent=%s ok=%s",
            window_label,
            restore_clicks,
            end_sent,
            restored,
        )
        return restored

    def _run_binary(
        self,
        args: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes] | None:
        try:
            return subprocess.run(
                list(args),
                capture_output=True,
                timeout=timeout,
                check=False,
                env=self._environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.debug("binary_command_failed command=%s error=%s", args[0], exc)
            return None

    def _visual_fingerprint_file(self, screenshot: Path) -> str | None:
        """Hash stable chat pixels for repeated-frame detection."""

        chat_x = max(0, int(self.window_width * 0.20))
        chat_y = min(100, max(0, self.window_height // 10))
        chat_width = max(320, int(self.window_width * 0.60))
        chat_height = max(480, self.window_height - chat_y - 60)
        result = self._run_binary(
            [
                self.convert_tool,
                str(screenshot),
                "-crop",
                f"{chat_width}x{chat_height}+{chat_x}+{chat_y}",
                "+repage",
                "-resize",
                "64x64!",
                "-colorspace",
                "Gray",
                "-depth",
                "8",
                "gray:-",
            ],
            timeout=self.probe_timeout,
        )
        if result is None or result.returncode != 0 or not result.stdout:
            return None
        return hashlib.sha256(result.stdout).hexdigest()

    def _screen_probe_fingerprint_file(self, screenshot: Path) -> str | None:
        """Return a noise-resistant 64-bit dHash for periodic recovery."""

        chat_x = max(0, int(self.window_width * 0.20))
        chat_y = min(100, max(0, self.window_height // 10))
        chat_width = max(320, int(self.window_width * 0.60))
        chat_height = max(480, self.window_height - chat_y - 60)
        result = self._run_binary(
            [
                self.convert_tool,
                str(screenshot),
                "-crop",
                f"{chat_width}x{chat_height}+{chat_x}+{chat_y}",
                "+repage",
                "-resize",
                "9x8!",
                "-colorspace",
                "Gray",
                "-blur",
                "0x0.8",
                "-depth",
                "8",
                "gray:-",
            ],
            timeout=self.probe_timeout,
        )
        if result is None or result.returncode != 0 or len(result.stdout) < 72:
            return None
        pixels = result.stdout
        value = 0
        for row in range(8):
            offset = row * 9
            for column in range(8):
                value = (value << 1) | int(
                    pixels[offset + column] > pixels[offset + column + 1]
                )
        return f"{value:016x}"

    def screen_fingerprints_equal(
        self,
        first: str | None,
        second: str | None,
    ) -> bool:
        if not first or not second:
            return first == second
        try:
            distance = (int(first, 16) ^ int(second, 16)).bit_count()
        except ValueError:
            return first == second
        return distance <= self.screen_probe_hamming_threshold

    def probe_fingerprint(self) -> str | None:
        """Take one cheap screenshot to recover from a missed WAL trigger."""

        window_id = self.find_window()
        if not window_id:
            return None
        probe = self.capture_dir / f".screen-probe-{os.getpid()}.png"
        try:
            captured = self._run(
                [self.capture_tool, "-window", window_id, str(probe)],
                timeout=self.capture_timeout,
            )
            if captured.returncode != 0 or not probe.exists():
                return None
            return self._screen_probe_fingerprint_file(probe)
        finally:
            probe.unlink(missing_ok=True)

    def prune_capture_screenshots(
        self,
        *,
        exclude: Sequence[Path] = (),
    ) -> tuple[int, int]:
        """Bound top-level receipt screenshots without touching evidence dirs."""

        excluded = {path.resolve() for path in exclude}
        cutoff = time.time() - self.screenshot_retention_seconds
        candidates: list[tuple[Path, float]] = []
        removed_expired = 0
        removed_overflow = 0
        try:
            paths = list(self.capture_dir.glob("receipt-*.png"))
        except OSError as exc:
            LOG.warning("wechat_screenshot_prune_failed error=%s", exc)
            return 0, 0
        for path in paths:
            try:
                if path.resolve() in excluded or not path.is_file():
                    continue
                modified = path.stat().st_mtime
                if modified < cutoff:
                    path.unlink(missing_ok=True)
                    removed_expired += 1
                else:
                    candidates.append((path, modified))
            except OSError as exc:
                LOG.warning(
                    "wechat_screenshot_prune_file_failed file=%s error=%s",
                    path,
                    exc,
                )
        candidates.sort(key=lambda item: item[1], reverse=True)
        for path, _modified in candidates[self.screenshot_max_files :]:
            try:
                path.unlink(missing_ok=True)
                removed_overflow += 1
            except OSError as exc:
                LOG.warning(
                    "wechat_screenshot_prune_file_failed file=%s error=%s",
                    path,
                    exc,
                )
        if removed_expired or removed_overflow:
            LOG.info(
                "wechat_screenshot_pruned expired=%s overflow=%s retained_limit=%s",
                removed_expired,
                removed_overflow,
                self.screenshot_max_files,
            )
        return removed_expired, removed_overflow

    def _tesseract_tsv(self, image: Path) -> list[VisualToken]:
        result = self._run(
            [
                self.ocr_tool,
                str(image),
                "stdout",
                "-l",
                "eng",
                "--psm",
                "11",
                "tsv",
            ],
            timeout=self.ocr_timeout,
        )
        return parse_tesseract_tsv(result.stdout) if result.returncode == 0 else []

    def _merchant_amount_candidates(
        self,
        screenshot: Path,
    ) -> list[VisualAmount]:
        candidates: list[VisualAmount] = []
        for token in self._tesseract_tsv(screenshot):
            if token.left < int(self.window_width * 0.17):
                continue
            if token.left + token.width > int(self.window_width * 0.83):
                continue
            amount = visual_amount_from_token(token)
            if amount is None:
                continue
            if any(
                existing.amount == amount.amount
                and abs(existing.center_y - amount.center_y) < 10
                for existing in candidates
            ):
                continue
            candidates.append(amount)
        return sorted(candidates, key=lambda item: item.center_y)

    def _clock_probe_candidates(
        self,
        screenshot: Path,
        *,
        desired_count: int = 1,
    ) -> list[VisualClock]:
        del desired_count  # Every threshold is bounded; retain all visible clocks.
        crop_width = max(180, min(320, int(self.window_width * 0.31)))
        crop_x = max(0, (self.window_width - crop_width) // 2)
        resize_factor = 3.0
        started = time.monotonic()
        candidates: list[VisualClock] = []
        for threshold in self.clock_probe_thresholds:
            strip = screenshot.with_name(
                screenshot.stem + f"-clock-strip-{threshold}.png"
            )
            try:
                enhanced = self._run(
                    [
                        self.convert_tool,
                        str(screenshot),
                        "-crop",
                        f"{crop_width}x{self.window_height}+{crop_x}+0",
                        "+repage",
                        "-resize",
                        "300%",
                        "-colorspace",
                        "Gray",
                        "-threshold",
                        f"{threshold}%",
                        str(strip),
                    ],
                    timeout=self.probe_timeout,
                )
                if enhanced.returncode != 0 or not strip.exists():
                    continue
                for token in self._tesseract_tsv(strip):
                    clock = visual_clock_from_token(
                        token,
                        resize_factor=resize_factor,
                    )
                    if clock is None:
                        continue
                    duplicate = next(
                        (
                            existing
                            for existing in candidates
                            if existing.clock == clock.clock
                            and abs(existing.center_y - clock.center_y) < 12
                        ),
                        None,
                    )
                    if duplicate is None:
                        candidates.append(clock)
                    elif clock.confidence > duplicate.confidence:
                        candidates.remove(duplicate)
                        candidates.append(clock)
            finally:
                strip.unlink(missing_ok=True)
        candidates.sort(key=lambda item: item.center_y)
        if candidates:
            LOG.info(
                "wechat_clock_probe clocks=%s elapsed_ms=%s",
                ",".join(clock.clock for clock in candidates),
                int((time.monotonic() - started) * 1000),
            )
        return candidates

    def _confirm_merchant_card(
        self,
        screenshot: Path,
        clock: VisualClock,
        amount: VisualAmount,
        *,
        index: int,
    ) -> bool:
        """Confirm each coordinate pair inside its own local card crop."""

        crop_x = max(0, int(self.window_width * 0.20))
        crop_width = min(
            self.window_width - crop_x,
            int(self.window_width * 0.60),
        )
        crop_y = max(
            0,
            int(clock.center_y + 10),
            int(amount.center_y - 220),
        )
        crop_bottom = min(self.window_height, int(amount.center_y + 170))
        crop_height = max(120, crop_bottom - crop_y)
        card = screenshot.with_name(
            screenshot.stem + f"-card-confirm-{index}.png"
        )
        try:
            cropped = self._run(
                [
                    self.convert_tool,
                    str(screenshot),
                    "-crop",
                    f"{crop_width}x{crop_height}+{crop_x}+{crop_y}",
                    "+repage",
                    "-resize",
                    "160%",
                    str(card),
                ],
                timeout=self.probe_timeout,
            )
            if cropped.returncode != 0 or not card.exists():
                return False
            result = self._run(
                [
                    self.ocr_tool,
                    str(card),
                    "stdout",
                    "-l",
                    self.ocr_language,
                    "--psm",
                    "11",
                ],
                timeout=self.ocr_timeout,
            )
            if result.returncode != 0:
                return False
            normalized = normalize_ocr_text(result.stdout)
            confirmed = (
                "收款通知" in normalized
                or (
                    "收款金额" in normalized
                    and ("商品名称" in normalized or "收款成功" in normalized)
                )
                or ("商品名称" in normalized and "收款成功" in normalized)
            )
            LOG.info(
                "wechat_card_confirmation amount=%s clock=%s confirmed=%s",
                amount.amount,
                clock.clock,
                confirmed,
            )
            return confirmed
        finally:
            card.unlink(missing_ok=True)

    def _merchant_visual_candidates(
        self,
        screenshot: Path,
    ) -> tuple[
        list[tuple[VisualClock, VisualAmount]],
        list[VisualClock],
        bool,
    ]:
        amounts = self._merchant_amount_candidates(screenshot)
        if not amounts:
            return [], [], True
        clocks = self._clock_probe_candidates(
            screenshot,
            desired_count=max(1, len(amounts)),
        )
        candidate_pairs = associate_visual_receipts(amounts, clocks)
        pairs = [
            pair
            for index, pair in enumerate(candidate_pairs, start=1)
            if self._confirm_merchant_card(
                screenshot,
                pair[0],
                pair[1],
                index=index,
            )
        ]
        if pairs:
            LOG.info(
                "wechat_visual_receipts candidates=%s",
                ",".join(
                    f"{clock.clock}/{amount.amount}" for clock, amount in pairs
                ),
            )
        return pairs, clocks, len(pairs) == len(amounts)

    @staticmethod
    def _is_merchant_window(window_label: str) -> bool:
        if "微信支付商家助手" in window_label:
            return True
        try:
            return re.search(window_label, "微信支付商家助手") is not None
        except re.error:
            return False

    def _ocr_captured_frame(self, frame: CapturedFrame) -> OcrFrameResult:
        started = time.monotonic()
        result = self._run(
            [
                self.ocr_tool,
                str(frame.screenshot),
                "stdout",
                "-l",
                self.ocr_language,
                "--psm",
                str(self.ocr_psm),
            ],
            timeout=self.ocr_timeout,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if result.returncode != 0:
            return OcrFrameResult(
                frame,
                None,
                elapsed_ms,
                result.stderr[:500],
            )
        ocr_text = result.stdout
        if self._is_merchant_window(frame.window_label):
            pairs, clocks, allow_fallback = self._merchant_visual_candidates(
                frame.screenshot
            )
            ocr_text = merchant_parser_text(
                result.stdout,
                pairs,
                clocks,
                allow_clock_fallback=allow_fallback,
            )
            if pairs:
                LOG.info(
                    "wechat_visual_evidence_selected pairs=%s "
                    "full_text_sha256=%s",
                    len(pairs),
                    hashlib.sha256(
                        normalize_ocr_text(result.stdout).encode("utf-8")
                    ).hexdigest()[:16],
                )
        return OcrFrameResult(frame, ocr_text, elapsed_ms, None)

    def _capture_screenshot(
        self,
        *,
        window_label: str,
        window_id: str,
        window_index: int,
        attempt: int,
        scroll_up_clicks: int,
        stamp: str,
    ) -> CapturedFrame | None:
        time.sleep(0.35)
        screenshot = self.capture_dir / (
            f"receipt-{stamp}-w{window_index}-a{attempt}"
            f"-up{scroll_up_clicks}.png"
        )
        result = self._run(
            [self.capture_tool, "-window", window_id, str(screenshot)],
            timeout=self.capture_timeout,
        )
        if result.returncode != 0 or not screenshot.exists():
            LOG.warning(
                "wechat_capture_failed window=%s attempt=%s scroll_up=%s "
                "error=%s",
                window_label,
                attempt,
                scroll_up_clicks,
                result.stderr[:500],
            )
            return None
        LOG.info(
            "wechat_capture_attempt window=%s attempt=%s scroll_up=%s "
            "screenshot=%s",
            window_label,
            attempt,
            scroll_up_clicks,
            screenshot,
        )
        return CapturedFrame(
            window_label,
            window_id,
            screenshot,
            attempt,
            scroll_up_clicks,
        )

    def capture_segments(
        self,
    ) -> Iterator[tuple[str, str, str, int, int]]:
        self.last_capture_successful = False
        self.last_capture_frames = 0
        self.last_capture_failure_reason = None
        self.last_bottom_fingerprint = None
        windows = self.find_windows()
        if not windows:
            self.last_capture_failure_reason = "window_missing"
            LOG.warning(
                "collection_window_missing regexes=%s",
                ",".join(self.window_name_regexes),
            )
            return

        stamp = (
            time.strftime("%Y%m%d-%H%M%S")
            + f"-{time.time_ns() % 1_000_000:06d}"
        )
        captured_frames: list[CapturedFrame] = []
        scan_had_error = False
        coverage_complete = True
        bottom_restored = True

        # Phase 1: capture all overlapping frames while the live UI is stable.
        for window_index, (window_label, window_id) in enumerate(
            windows,
            start=1,
        ):
            if not self._prepare_window(window_id):
                scan_had_error = True
            window_complete = not self.adaptive_scroll_enabled
            seen_fingerprints: set[str] = set()
            try:
                if self.adaptive_scroll_enabled:
                    positions = tuple(
                        index * self.adaptive_scroll_clicks
                        for index in range(self.adaptive_scroll_max_frames)
                    )
                    if not self._return_to_bottom(window_id, window_label):
                        scan_had_error = True
                else:
                    positions = self.scroll_up_attempts

                for attempt, scroll_up_clicks in enumerate(positions, start=1):
                    if self.adaptive_scroll_enabled:
                        if attempt > 1 and not self._xdotool(
                            "click",
                            "--repeat",
                            str(self.adaptive_scroll_clicks),
                            "--delay",
                            "100",
                            "4",
                        ):
                            scan_had_error = True
                    else:
                        if not self._return_to_bottom(window_id, window_label):
                            scan_had_error = True
                        if scroll_up_clicks and not self._xdotool(
                            "click",
                            "--repeat",
                            str(scroll_up_clicks),
                            "--delay",
                            "100",
                            "4",
                        ):
                            scan_had_error = True

                    frame = self._capture_screenshot(
                        window_label=window_label,
                        window_id=window_id,
                        window_index=window_index,
                        attempt=attempt,
                        scroll_up_clicks=scroll_up_clicks,
                        stamp=stamp,
                    )
                    if frame is None:
                        scan_had_error = True
                        continue
                    if (
                        window_index == 1
                        and attempt == 1
                        and scroll_up_clicks == 0
                    ):
                        self.last_bottom_fingerprint = (
                            self._screen_probe_fingerprint_file(
                                frame.screenshot
                            )
                        )
                    if self.adaptive_scroll_enabled:
                        fingerprint = self._visual_fingerprint_file(
                            frame.screenshot
                        )
                        if fingerprint and fingerprint in seen_fingerprints:
                            window_complete = True
                            LOG.info(
                                "wechat_adaptive_scroll_complete window=%s "
                                "attempt=%s scroll_up=%s reason=repeated_frame",
                                window_label,
                                attempt,
                                scroll_up_clicks,
                            )
                            frame.screenshot.unlink(missing_ok=True)
                            break
                        if fingerprint:
                            seen_fingerprints.add(fingerprint)
                    captured_frames.append(frame)
                else:
                    if self.adaptive_scroll_enabled:
                        window_complete = True
                        LOG.info(
                            "wechat_adaptive_scroll_complete window=%s "
                            "frames=%s reason=bounded_max_frames "
                            "coverage_complete=true",
                            window_label,
                            len(positions),
                        )
                coverage_complete = coverage_complete and window_complete
            finally:
                restored = self._return_to_bottom(window_id, window_label)
                bottom_restored = bottom_restored and restored
                if not restored:
                    scan_had_error = True

        # Phase 2: OCR immutable files after every window is back at the bottom.
        executor: ThreadPoolExecutor | None = None
        if self.ocr_workers == 1:
            processed_frames = map(self._ocr_captured_frame, captured_frames)
        else:
            executor = ThreadPoolExecutor(
                max_workers=self.ocr_workers,
                thread_name_prefix="wechat-ocr",
            )
            processed_frames = executor.map(
                self._ocr_captured_frame,
                captured_frames,
            )
        LOG.info(
            "wechat_ocr_pool workers=%s frames=%s",
            self.ocr_workers,
            len(captured_frames),
        )
        try:
            for result in processed_frames:
                frame = result.frame
                if result.error is not None:
                    scan_had_error = True
                    LOG.warning(
                        "wechat_ocr_failed window=%s attempt=%s "
                        "scroll_up=%s error=%s",
                        frame.window_label,
                        frame.attempt,
                        frame.scroll_up_clicks,
                        result.error,
                    )
                    continue
                self.last_capture_frames += 1
                LOG.info(
                    "wechat_ocr_attempt window=%s attempt=%s scroll_up=%s "
                    "elapsed_ms=%s screenshot=%s",
                    frame.window_label,
                    frame.attempt,
                    frame.scroll_up_clicks,
                    result.elapsed_ms,
                    frame.screenshot,
                )
                yield (
                    result.text or "",
                    frame.window_id,
                    str(frame.screenshot),
                    frame.attempt,
                    result.elapsed_ms,
                )
        finally:
            if executor is not None:
                executor.shutdown(wait=True)
            if not self.keep_screenshots:
                for frame in captured_frames:
                    frame.screenshot.unlink(missing_ok=True)

        self.last_capture_successful = bool(
            self.last_capture_frames > 0
            and not scan_had_error
            and coverage_complete
            and bottom_restored
        )
        if not self.last_capture_successful:
            if self.last_capture_frames == 0:
                self.last_capture_failure_reason = "no_ocr_frames"
            elif scan_had_error:
                self.last_capture_failure_reason = "capture_error"
            elif not coverage_complete:
                self.last_capture_failure_reason = "coverage_incomplete"
            elif not bottom_restored:
                self.last_capture_failure_reason = "bottom_restore_failed"
            LOG.warning(
                "wechat_scan_incomplete frames=%s coverage_complete=%s "
                "bottom_restored=%s reason=%s",
                self.last_capture_frames,
                coverage_complete,
                bottom_restored,
                self.last_capture_failure_reason,
            )

    def capture_all(self, scroll_up_clicks: int) -> list[tuple[str, str]]:
        """Compatibility helper for one explicitly selected scroll position."""

        prior_adaptive = self.adaptive_scroll_enabled
        prior_attempts = self.scroll_up_attempts
        try:
            self.adaptive_scroll_enabled = False
            self.scroll_up_attempts = (max(0, int(scroll_up_clicks)),)
            return [
                (text, window_id)
                for text, window_id, _screenshot, _attempt, _elapsed
                in self.capture_segments()
            ]
        finally:
            self.adaptive_scroll_enabled = prior_adaptive
            self.scroll_up_attempts = prior_attempts

    def capture(self, scroll_up_clicks: int) -> tuple[str, str] | None:
        rows = self.capture_all(scroll_up_clicks)
        return rows[0] if rows else None


def capture_scan_can_clear(
    capture: LinuxCapture,
    capture_exception: Exception | None,
) -> bool:
    return bool(capture_exception is None and capture.last_capture_successful)


def process_capture_segments(
    *,
    capture: LinuxCapture,
    parser: ReceiptParser,
    trigger_time: int,
    trigger_signature: str,
    ignore_freshness: bool,
) -> Iterator[tuple[PaymentEvent, str]]:
    texts: list[str] = []
    emitted_keys: set[str] = set()
    for text, window_id, screenshot, attempt, _elapsed in capture.capture_segments():
        texts.append(text)
        combined = OCR_CAPTURE_SEPARATOR.join(texts)
        events, reason = parser.parse_all(
            combined,
            trigger_time=trigger_time,
            trigger_signature=trigger_signature,
            source="wechat-linux-wal-ocr",
            ignore_freshness=ignore_freshness,
        )
        fresh = [
            (event, receipt_key)
            for event, receipt_key in events
            if receipt_key not in emitted_keys
        ]
        if fresh:
            LOG.info(
                "ocr_batch attempt=%s window=%s candidates=%s screenshot=%s",
                attempt,
                window_id,
                len(fresh),
                screenshot,
            )
            for event, receipt_key in fresh:
                emitted_keys.add(receipt_key)
                LOG.info(
                    "ocr_candidate attempt=%s window=%s amount=%s "
                    "occurred_at=%s screenshot=%s",
                    attempt,
                    window_id,
                    event.amount,
                    event.occurred_at,
                    screenshot,
                )
                yield event, receipt_key
        elif not emitted_keys:
            LOG.info(
                "ocr_attempt_miss attempt=%s reason=%s text_sha256=%s",
                attempt,
                reason or "pattern_not_found",
                hashlib.sha256(
                    normalize_ocr_text(combined).encode("utf-8")
                ).hexdigest()[:16],
            )


def _combined_change_signature(
    changed: Sequence[tuple[Path, FileSignature, str]],
) -> tuple[str, str]:
    signature = "|".join(
        f"{path}:{format_file_signature(current)}"
        for path, current, _reason in changed
    )
    reason = (
        "trigger_file_reappeared"
        if any(row[2] == "trigger_file_reappeared" for row in changed)
        else "trigger_file_changed"
    )
    return signature, reason


def _parser_for_age(
    config: Mapping[str, Any],
    max_event_age_seconds: int,
) -> ReceiptParser:
    parser_config = config.get("parser", {})
    if not isinstance(parser_config, Mapping):
        parser_config = {}
    return ReceiptParser(
        {
            **config,
            "parser": {
                **parser_config,
                "max_event_age_seconds": max_event_age_seconds,
            },
        }
    )


def run(config: Mapping[str, Any], once: bool = False) -> int:
    platform = config["linux"]
    runtime_config = config.get("runtime", {})
    if not isinstance(runtime_config, Mapping):
        runtime_config = {}
    trigger_patterns = platform["trigger_files"]
    trigger_paths = discover_trigger_files(trigger_patterns, config)
    signatures = {path: file_signature(path) for path in trigger_paths}
    capture = LinuxCapture(platform, config)
    runtime = AgentRuntime(config)
    service_state_value = runtime_config.get(
        "service_notification_state_file"
    )
    service_state_path = (
        resolve_path(str(service_state_value), config)
        if service_state_value
        else runtime.spool.root / "service-notification-state.json"
    )
    service_state = ServiceNotificationState(
        service_state_path,
        int(runtime_config.get("service_notification_state_max_ids", 512)),
    )
    if not service_state.initialized:
        provider = str(config["channel"].get("provider") or "wxpay")
        channel_id = str(config["channel"]["id"])
        prefix = f"{provider}|{channel_id}|txn|"
        recovered_ids = [
            key.removeprefix(prefix)
            for key in runtime.dedupe.values
            if key.startswith(prefix)
        ]
        if recovered_ids:
            service_state.commit(recovered_ids)
            LOG.info(
                "wechat_service_state_recovered transaction_ids=%s",
                len(recovered_ids),
            )

    LOG.info(
        "agent_started platform=linux agent_id=%s triggers=%s "
        "restored_pending=%s window=%s",
        config["agent"]["id"],
        len(trigger_paths),
        len(runtime.pending),
        bool(capture.find_window()),
    )
    if once:
        return 0

    trigger_store_value = runtime_config.get("capture_trigger_file")
    if trigger_store_value:
        trigger_store_path = resolve_path(str(trigger_store_value), config)
    else:
        trigger_store_path = (
            resolve_path(
                str(runtime_config.get("spool_dir") or "spool"),
                config,
            )
            / "capture-trigger.json"
        )
    trigger_store = CaptureTriggerStore(trigger_store_path)
    poll_seconds = max(
        0.2,
        float(runtime_config.get("poll_seconds", 0.5)),
    )
    quiet_seconds = max(
        0.0,
        float(platform.get("trigger_quiet_seconds", 0.8)),
    )
    legacy_first_delay = float(attempt_plan(platform)[0]["delay_seconds"])
    render_delay_seconds = max(
        0.5,
        float(
            platform.get(
                "render_delay_seconds",
                legacy_first_delay,
            )
        ),
    )
    max_debounce_seconds = max(
        quiet_seconds,
        render_delay_seconds,
        float(platform.get("max_debounce_seconds", 5.0)),
    )
    capture_retry_seconds = max(
        2.0,
        float(platform.get("capture_retry_seconds", 15.0)),
    )
    parser_config = config.get("parser", {})
    if not isinstance(parser_config, Mapping):
        parser_config = {}
    normal_event_age = max(
        1,
        int(parser_config.get("max_event_age_seconds", 180)),
    )
    recovery_event_age = max(
        normal_event_age,
        int(
            parser_config.get(
                "recovery_max_event_age_seconds",
                600,
            )
        ),
    )
    screen_probe_interval = max(
        0.0,
        float(platform.get("screen_probe_interval_seconds", 15.0)),
    )

    now_mono = time.monotonic()
    restored_trigger = trigger_store.load()
    if restored_trigger is not None:
        source_reason = restored_trigger.reason
        restored_trigger = trigger_store.schedule(
            signature=restored_trigger.signature,
            trigger_time=restored_trigger.trigger_time,
            reason="restored_trigger",
        )
        LOG.info(
            "capture_trigger_restored source_reason=%s signature=%s",
            source_reason,
            restored_trigger.signature,
        )
    elif _as_bool(platform.get("startup_recovery_enabled"), default=True):
        baseline_text = "|".join(
            f"{path}:{format_file_signature(signature)}"
            for path, signature in signatures.items()
            if signature is not None
        )
        restored_trigger = trigger_store.schedule(
            signature=f"startup:{baseline_text or 'missing'}",
            trigger_time=int(time.time()),
            reason="startup_recovery",
        )
        LOG.info(
            "capture_trigger_scheduled reason=startup_recovery signature=%s",
            restored_trigger.signature,
        )

    trigger_first_seen = now_mono if restored_trigger is not None else 0.0
    trigger_due = now_mono + 0.5 if restored_trigger is not None else 0.0
    last_heartbeat = 0.0
    last_screen_probe = now_mono
    last_screen_fingerprint = capture.probe_fingerprint()

    while True:
        now_mono = time.monotonic()
        discovered = discover_trigger_files(trigger_patterns, config)
        all_paths = sorted(set(signatures) | set(discovered))
        changed: list[tuple[Path, FileSignature, str]] = []
        for path in all_paths:
            current = file_signature(path)
            prior = signatures.get(path)
            change = trigger_file_change(prior, current)
            if change is not None:
                _signature_text, reason = change
                assert current is not None
                changed.append((path, current, reason))
            signatures[path] = current

        if changed:
            signature_text, reason = _combined_change_signature(changed)
            if trigger_store.load() is None:
                trigger_first_seen = now_mono
            trigger_store.schedule(
                signature=signature_text,
                trigger_time=int(time.time()),
                reason=reason,
            )
            trigger_due = max(
                trigger_first_seen + render_delay_seconds,
                min(
                    now_mono + quiet_seconds,
                    trigger_first_seen + max_debounce_seconds,
                ),
            )
            LOG.info(
                "capture_trigger_scheduled reason=%s files=%s",
                reason,
                ",".join(str(path) for path, _current, _reason in changed),
            )

        active_trigger = trigger_store.load()
        if active_trigger is not None and now_mono >= trigger_due:
            started = time.monotonic()
            capture_exception: Exception | None = None
            fallback_exception: Exception | None = None
            service_result: ServiceNotificationScanResult | None = None
            service_baseline = not service_state.initialized
            service_discovered_ids: list[str] = []
            service_candidate_count = 0
            fallback_attempted = False
            recovery_reasons = {
                "startup_recovery",
                "restored_trigger",
                "window_recovered",
            }
            max_event_age = (
                recovery_event_age
                if active_trigger.reason in recovery_reasons
                else normal_event_age
            )
            candidates: list[tuple[PaymentEvent, str]] = []
            try:
                parser = _parser_for_age(config, max_event_age)
                service_result = capture.capture_service_notifications(
                    state=service_state,
                    trigger_time=active_trigger.trigger_time,
                    timezone=parser.timezone,
                    baseline_only=service_baseline,
                )
                service_discovered_ids = [
                    receipt.external_txn_id
                    for receipt in service_result.receipts
                ]
                if service_result.available:
                    if service_baseline:
                        LOG.info(
                            "wechat_service_baseline receipts=%s frames=%s",
                            len(service_discovered_ids),
                            service_result.frames,
                        )
                    else:
                        service_events, service_reason = (
                            parser.events_from_service_notifications(
                                service_result.receipts,
                                trigger_time=active_trigger.trigger_time,
                                source=(
                                    "wechat-linux-service-notification-ocr"
                                ),
                                known_transaction_ids=(
                                    service_state.transaction_ids
                                ),
                            )
                        )
                        service_candidate_count = len(service_events)
                        candidates.extend(service_events)
                        if not service_events:
                            LOG.info(
                                "wechat_service_no_new_receipt reason=%s",
                                service_reason or "known_or_not_found",
                            )
            except Exception as exc:
                capture_exception = exc
                LOG.exception(
                    "wechat_service_capture_cycle_failed reason=%s",
                    active_trigger.reason,
                )

            run_fallback = bool(
                service_baseline
                or service_result is None
                or not service_result.available
                or not service_result.successful
                or service_candidate_count == 0
            )
            if run_fallback:
                fallback_attempted = True
                try:
                    parser = _parser_for_age(config, max_event_age)
                    candidates.extend(
                        process_capture_segments(
                            capture=capture,
                            parser=parser,
                            trigger_time=active_trigger.trigger_time,
                            trigger_signature=active_trigger.signature,
                            ignore_freshness=False,
                        )
                    )
                except Exception as exc:
                    fallback_exception = exc
                    if capture_exception is None:
                        capture_exception = exc
                    LOG.exception(
                        "fallback_capture_cycle_failed reason=%s",
                        active_trigger.reason,
                    )

            capture.prune_capture_screenshots()

            queued_ids: list[str] = []
            for event, receipt_key in candidates:
                if runtime.queue(event, receipt_key):
                    queued_ids.append(event.event_id)

            if (
                service_result is not None
                and service_result.available
                and service_result.successful
            ):
                # Queue persistence precedes cursor advancement, so a restart
                # cannot lose a captured event that has not reached the bridge.
                service_state.commit(service_discovered_ids)

            service_scan_successful = bool(
                service_result is None
                or not service_result.available
                or service_result.successful
            )
            fallback_scan_successful = bool(
                not fallback_attempted
                or capture_scan_can_clear(capture, fallback_exception)
            )
            scan_successful = bool(
                capture_exception is None
                and service_scan_successful
                and fallback_scan_successful
            )
            if scan_successful:
                trigger_store.clear(active_trigger.signature)
                trigger_due = 0.0
                trigger_first_seen = 0.0
                refreshed = capture.probe_fingerprint()
                if refreshed:
                    if (
                        fallback_attempted
                        and capture.last_bottom_fingerprint is not None
                        and not capture.screen_fingerprints_equal(
                            refreshed,
                            capture.last_bottom_fingerprint,
                        )
                    ):
                        followup = trigger_store.schedule(
                            signature=f"screen:{refreshed[:32]}",
                            trigger_time=int(time.time()),
                            reason="post_capture_screen_changed",
                        )
                        trigger_first_seen = time.monotonic()
                        trigger_due = trigger_first_seen + 0.5
                        LOG.info(
                            "capture_trigger_scheduled reason=%s signature=%s",
                            followup.reason,
                            followup.signature,
                        )
                    last_screen_fingerprint = refreshed
                last_screen_probe = time.monotonic()
            else:
                trigger_due = time.monotonic() + capture_retry_seconds
                LOG.warning(
                    "capture_trigger_retained reason=%s signature=%s "
                    "retry_seconds=%s scan_reason=%s",
                    active_trigger.reason,
                    active_trigger.signature,
                    int(capture_retry_seconds),
                    (
                        service_result.coverage_reason
                        if service_result is not None
                        and service_result.available
                        and not service_result.successful
                        else None
                    )
                    or capture.last_capture_failure_reason
                    or (
                        type(capture_exception).__name__
                        if capture_exception is not None
                        else "incomplete"
                    ),
                )

            # Network delivery starts after the UI is restored and the entire
            # candidate batch is persisted locally.
            runtime.deliver_ids(queued_ids, time.monotonic())
            LOG.info(
                "ocr_cycle_complete reason=%s candidates=%s "
                "scan_successful=%s elapsed_ms=%s",
                active_trigger.reason,
                len(candidates),
                scan_successful,
                int((time.monotonic() - started) * 1000),
            )

        now_mono = time.monotonic()
        if (
            screen_probe_interval > 0
            and trigger_store.load() is None
            and now_mono - last_screen_probe >= screen_probe_interval
        ):
            last_screen_probe = now_mono
            fingerprint = capture.probe_fingerprint()
            if fingerprint:
                reason: str | None = None
                if last_screen_fingerprint is None:
                    reason = "window_recovered"
                elif not capture.screen_fingerprints_equal(
                    fingerprint,
                    last_screen_fingerprint,
                ):
                    reason = "periodic_screen_changed"
                if reason is not None:
                    trigger = trigger_store.schedule(
                        signature=f"screen:{fingerprint[:32]}",
                        trigger_time=int(time.time()),
                        reason=reason,
                    )
                    trigger_first_seen = now_mono
                    trigger_due = now_mono + 0.5
                    LOG.info(
                        "capture_trigger_scheduled reason=%s signature=%s",
                        reason,
                        trigger.signature,
                    )
                last_screen_fingerprint = fingerprint

        now_mono = time.monotonic()
        runtime.deliver_due(now_mono)
        if now_mono - last_heartbeat >= 60:
            LOG.info(
                "heartbeat platform=linux window=%s pending=%s "
                "capture_pending=%s",
                bool(capture.find_window()),
                len(runtime.pending),
                trigger_store.load() is not None,
            )
            last_heartbeat = now_mono
        time.sleep(poll_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="Linux WeChat payment receiver")
    cli.add_argument("--config", required=True)
    cli.add_argument(
        "--once",
        action="store_true",
        help="validate configuration and probe the window, then exit",
    )
    cli.add_argument("--verbose", action="store_true")
    args = cli.parse_args(argv)
    config = load_json(args.config)
    validate_config(config, "linux")
    setup_logging(config, args.verbose)
    return run(config, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
