#!/usr/bin/env python3
"""Shared, dependency-free core for the WeChat payment receiver agents."""

from __future__ import annotations

import glob
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


LOG = logging.getLogger("wechat-payment-receiver")
OCR_CAPTURE_SEPARATOR = "\n<<<WECHAT_CAPTURE_BREAK>>>\n"
OCR_LATEST_CLOCK_PREFIX = "WECHAT_LATEST_CLOCK="


DEFAULT_RECEIPT_PATTERN = (
    r"(?:经营码)?收款到账通知.{0,200}?"
    r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2}).{0,300}?"
    r"(?:收款金额)?[￥¥YV]?"
    r"(?P<amount>[0-9Oo]+[.．。][0-9Oo]{1,2})"
)


@dataclass(frozen=True)
class PaymentEvent:
    event_id: str
    provider: str
    channel_id: str
    amount: str
    occurred_at: int
    external_txn_id: str | None
    trade_no: str | None
    payer: str | None
    raw_text: str | None
    source: str
    agent_id: str

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CaptureTrigger:
    """One durable request to scan the payment conversation."""

    signature: str
    trigger_time: int
    reason: str
    scheduled_at: int


@dataclass(frozen=True)
class VisualToken:
    """A word-level Tesseract TSV result with screen coordinates."""

    text: str
    left: int
    top: int
    width: int
    height: int
    confidence: float

    @property
    def center_y(self) -> float:
        return self.top + (self.height / 2.0)


@dataclass(frozen=True)
class VisualAmount:
    amount: str
    center_y: float
    confidence: float


@dataclass(frozen=True)
class VisualClock:
    clock: str
    center_y: float
    confidence: float


@dataclass(frozen=True)
class MerchantTextReceipt:
    clock: str
    amount: str
    raw_text: str


@dataclass(frozen=True)
class ServiceNotificationReceipt:
    """One complete 微信收款商业版 service-notification card."""

    occurred_at: int
    amount: str
    external_txn_id: str
    raw_text: str


def load_json(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).expanduser().resolve()
    value = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    value["_config_dir"] = str(config_path.parent)
    return value


def resolve_path(value: str, config: Mapping[str, Any]) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(str(value)))
    path = Path(expanded)
    if not path.is_absolute():
        path = Path(str(config.get("_config_dir") or Path.cwd())) / path
    return path.resolve()


def discover_trigger_files(patterns: Iterable[str], config: Mapping[str, Any]) -> list[Path]:
    paths: set[Path] = set()
    for raw in patterns:
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        if not os.path.isabs(expanded):
            expanded = str(Path(str(config.get("_config_dir") or Path.cwd())) / expanded)
        matches = glob.glob(expanded, recursive=True)
        if matches:
            paths.update(Path(match).resolve() for match in matches)
        elif not any(marker in expanded for marker in "*?["):
            paths.add(Path(expanded).resolve())
    return sorted(paths)


def secret_from_config(config: Mapping[str, Any]) -> str:
    bridge = config.get("bridge", {})
    if not isinstance(bridge, Mapping):
        raise ValueError("bridge must be an object")
    env_name = str(bridge.get("token_env") or "WECHAT_RECEIVER_TOKEN")
    token = os.environ.get(env_name, "")
    if token:
        return token
    inline = str(bridge.get("token") or "")
    if inline and bridge.get("allow_inline_secret") is True:
        return inline
    raise ValueError(f"set the {env_name} environment variable")


def validate_config(config: Mapping[str, Any], platform_name: str) -> None:
    bridge = config.get("bridge", {})
    if not isinstance(bridge, Mapping):
        raise ValueError("bridge is required")
    url = str(bridge.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("bridge.url must be an absolute HTTP(S) URL")
    if parsed.scheme == "http":
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if not (bridge.get("allow_http_localhost") is True and parsed.hostname in local_hosts):
            raise ValueError("plain HTTP is restricted to an explicitly enabled loopback URL")
    secret_from_config(config)
    agent = config.get("agent", {})
    if not isinstance(agent, Mapping) or not str(agent.get("id") or ""):
        raise ValueError("agent.id is required")
    channel = config.get("channel", {})
    if not isinstance(channel, Mapping) or not str(channel.get("id") or "").isdigit():
        raise ValueError("channel.id must contain digits only")
    platform = config.get(platform_name, {})
    if not isinstance(platform, Mapping):
        raise ValueError(f"{platform_name} settings are required")
    patterns = platform.get("trigger_files", [])
    if not isinstance(patterns, list) or not patterns:
        raise ValueError(f"{platform_name}.trigger_files must be a non-empty list")


def normalize_ocr_text(value: str) -> str:
    text = re.sub(r"\s+", "", str(value)).replace("\x00", "")
    return text.translate(str.maketrans({"．": ".", "。": ".", "：": ":"}))


def canonical_money(value: str) -> str:
    cleaned = value.translate(str.maketrans({"．": ".", "。": ".", "O": "0", "o": "0"}))
    if not re.fullmatch(r"(?:0|[1-9]\d*)(?:\.\d{1,2})?", cleaned):
        raise ValueError("invalid amount")
    yuan, _, fraction = cleaned.partition(".")
    cents = int(yuan) * 100 + int((fraction + "00")[:2])
    if cents <= 0:
        raise ValueError("amount must be positive")
    return f"{cents // 100}.{cents % 100:02d}"


def parse_tesseract_tsv(value: str) -> list[VisualToken]:
    """Parse Tesseract TSV without optional image/data-frame dependencies."""

    rows = value.splitlines()
    if not rows:
        return []
    header = rows[0].split("\t")
    indexes = {name: index for index, name in enumerate(header)}
    required = {"left", "top", "width", "height", "conf", "text"}
    if not required.issubset(indexes):
        return []
    result: list[VisualToken] = []
    for row in rows[1:]:
        columns = row.split("\t")
        if len(columns) < len(header):
            columns.extend([""] * (len(header) - len(columns)))
        try:
            text = columns[indexes["text"]].strip()
            if not text:
                continue
            result.append(
                VisualToken(
                    text=text,
                    left=int(columns[indexes["left"]]),
                    top=int(columns[indexes["top"]]),
                    width=int(columns[indexes["width"]]),
                    height=int(columns[indexes["height"]]),
                    confidence=float(columns[indexes["conf"]]),
                )
            )
        except (IndexError, ValueError):
            continue
    return result


def visual_amount_from_token(token: VisualToken) -> VisualAmount | None:
    compact = normalize_ocr_text(token.text)
    match = re.fullmatch(
        r"(?P<currency>[￥¥YV])?(?P<amount>[0-9Oo]{1,8}[.][0-9Oo]{2})",
        compact,
    )
    if not match or token.height < 20:
        return None
    # A currency-less decimal may belong to another service card. Require
    # stronger OCR confidence when there is no currency-shaped prefix.
    minimum_confidence = 35 if match.group("currency") else 60
    if token.confidence < minimum_confidence:
        return None
    try:
        amount = canonical_money(match.group("amount"))
    except ValueError:
        return None
    return VisualAmount(amount, token.center_y, token.confidence)


def visual_clock_from_token(
    token: VisualToken,
    *,
    resize_factor: float = 1.0,
) -> VisualClock | None:
    compact = normalize_ocr_text(token.text)
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", compact)
    if not match or token.confidence < 25:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    factor = resize_factor if resize_factor > 0 else 1.0
    return VisualClock(
        clock=f"{hour:02d}:{minute:02d}",
        center_y=token.center_y / factor,
        confidence=token.confidence,
    )


def associate_visual_receipts(
    amounts: Sequence[VisualAmount],
    clocks: Sequence[VisualClock],
    *,
    min_gap: float = 35.0,
    max_gap: float = 640.0,
) -> list[tuple[VisualClock, VisualAmount]]:
    """Pair each amount with the nearest preceding chat-group clock.

    WeChat may print one clock above a burst and omit it on the following
    cards. Reusing the nearest preceding clock preserves every locally
    confirmed receipt in that group, while a clipped top card with no visible
    preceding clock is left for an overlapping frame.
    """

    ordered_clocks = sorted(clocks, key=lambda item: item.center_y)
    pairs: list[tuple[VisualClock, VisualAmount]] = []
    for amount in sorted(amounts, key=lambda item: item.center_y):
        eligible = [
            clock
            for clock in ordered_clocks
            if min_gap <= amount.center_y - clock.center_y <= max_gap
        ]
        if eligible:
            pairs.append((max(eligible, key=lambda item: item.center_y), amount))
    return pairs


def merchant_text_receipts(
    text: str,
    *,
    fallback_clock: str | None = None,
) -> list[MerchantTextReceipt]:
    """Extract bounded merchant cards and never borrow the next card's amount."""

    normalized = normalize_ocr_text(text)
    headers = list(re.finditer(r"收款通知", normalized))
    if not headers:
        return []

    embedded_fallback = re.search(
        rf"{re.escape(OCR_LATEST_CLOCK_PREFIX)}"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})",
        normalized,
    )
    if fallback_clock is None and embedded_fallback:
        fallback_clock = (
            f"{int(embedded_fallback.group('hour')):02d}:"
            f"{int(embedded_fallback.group('minute')):02d}"
        )
    if fallback_clock is not None:
        fallback_match = re.fullmatch(r"(\d{1,2}):(\d{2})", fallback_clock)
        if fallback_match is None or not (
            0 <= int(fallback_match.group(1)) <= 23
            and 0 <= int(fallback_match.group(2)) <= 59
        ):
            fallback_clock = None
        else:
            fallback_clock = (
                f"{int(fallback_match.group(1)):02d}:"
                f"{int(fallback_match.group(2)):02d}"
            )

    clock_re = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
    labelled_amount_re = re.compile(
        r"收款金额.{0,32}?[￥¥YV]?"
        r"(?P<amount>[0-9Oo]{1,8}[.][0-9Oo]{1,2})"
    )
    currency_amount_re = re.compile(
        r"[￥¥YV](?P<amount>[0-9Oo]{1,8}[.][0-9Oo]{1,2})"
    )
    receipts: list[MerchantTextReceipt] = []
    group_clock: str | None = None
    for index, header in enumerate(headers):
        card_end = (
            headers[index + 1].start()
            if index + 1 < len(headers)
            else len(normalized)
        )
        prefix_start = max(0, header.start() - 160)
        if index:
            prefix_start = max(prefix_start, headers[index - 1].end())
        clock: str | None = None
        for clock_match in clock_re.finditer(
            normalized[prefix_start : header.start()]
        ):
            hour = int(clock_match.group(1))
            minute = int(clock_match.group(2))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                clock = f"{hour:02d}:{minute:02d}"
        if clock is not None:
            group_clock = clock
        elif group_clock is not None:
            # A service-account group may show one clock above several cards.
            clock = group_clock
        elif index == len(headers) - 1:
            clock = fallback_clock
        if clock is None:
            continue

        card_body = normalized[header.end() : card_end][:260]
        amount_match = labelled_amount_re.search(card_body)
        if amount_match is None:
            amount_match = currency_amount_re.search(card_body)
        if amount_match is None:
            continue
        try:
            amount = canonical_money(amount_match.group("amount"))
        except ValueError:
            continue
        receipts.append(
            MerchantTextReceipt(
                clock=clock,
                amount=amount,
                raw_text=normalized[header.start() : card_end],
            )
        )
    return receipts


def _amount_cents(value: str) -> int:
    yuan, fraction = value.split(".", 1)
    return int(yuan) * 100 + int(fraction)


def merchant_parser_text(
    full_ocr_text: str,
    visual_pairs: Sequence[tuple[VisualClock, VisualAmount]],
    probe_clocks: Sequence[VisualClock],
    *,
    allow_clock_fallback: bool = True,
) -> str:
    """Merge coordinate evidence card-by-card and suppress OCR conflicts."""

    latest_clock = (
        probe_clocks[-1].clock
        if allow_clock_fallback and probe_clocks
        else None
    )
    if not visual_pairs:
        text = full_ocr_text
        if latest_clock:
            text += "\n" + OCR_LATEST_CLOCK_PREFIX + latest_clock + "\n"
        return text

    merged = merchant_text_receipts(
        full_ocr_text,
        fallback_clock=latest_clock,
    )
    matched_full_indexes: set[int] = set()
    for clock, amount in sorted(visual_pairs, key=lambda pair: pair[1].center_y):
        unmatched_exact_amount = [
            index
            for index, receipt in enumerate(merged)
            if index not in matched_full_indexes and receipt.amount == amount.amount
        ]
        same_clock = [
            index
            for index, receipt in enumerate(merged)
            if index not in matched_full_indexes and receipt.clock == clock.clock
        ]
        exact = [
            index
            for index in same_clock
            if merged[index].amount == amount.amount
        ]
        matched_index: int | None = None
        if exact:
            matched_index = exact[0]
        elif len(unmatched_exact_amount) == 1:
            # Coordinate evidence may restore a clock that full OCR attached to
            # the wrong card. Re-clock a unique exact amount only.
            matched_index = unmatched_exact_amount[0]
        elif same_clock:
            # Replace only the closest same-clock amount; unrelated cards stay.
            matched_index = min(
                same_clock,
                key=lambda index: abs(
                    _amount_cents(merged[index].amount)
                    - _amount_cents(amount.amount)
                ),
            )
        if matched_index is None:
            merged.append(
                MerchantTextReceipt(clock.clock, amount.amount, "visual")
            )
            matched_full_indexes.add(len(merged) - 1)
        else:
            merged[matched_index] = MerchantTextReceipt(
                clock.clock,
                amount.amount,
                "visual",
            )
            matched_full_indexes.add(matched_index)

    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for receipt in merged:
        key = (receipt.clock, receipt.amount)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{receipt.clock} 收款通知 ¥{receipt.amount}")
    # Keep a non-digit boundary after whitespace normalization, otherwise one
    # amount's cents can hide the next card's clock.
    text = "\n<WECHAT_MERCHANT_CARD>\n".join(lines)
    if latest_clock:
        text += "\n" + OCR_LATEST_CLOCK_PREFIX + latest_clock + "\n"
    return text


def service_notification_receipts(
    text: str,
    *,
    trigger_time: int,
    timezone: ZoneInfo,
) -> list[ServiceNotificationReceipt]:
    """Extract exact-time receipts with a durable WeChat transaction ID.

    OCR can misread the large headline amount while preserving the smaller
    order-amount row. The transaction ID follows both, so the final canonical
    decimal before that ID is the strongest amount signal. Daily totals appear
    after the ID and are intentionally excluded.
    """

    normalized = normalize_ocr_text(text)
    headers = list(re.finditer(r"收款通知", normalized))
    timestamp_re = re.compile(
        r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    )
    transaction_re = re.compile(
        r"(?<![0-9Oo])(?P<transaction>[4][0-9Oo]{23,35})(?![0-9Oo])"
    )
    amount_re = re.compile(
        r"[￥¥YVxX]?(?P<amount>[0-9Oo]{1,6}[.][0-9Oo]{1,2})"
    )
    receipts: dict[str, ServiceNotificationReceipt] = {}
    current = datetime.fromtimestamp(trigger_time, timezone)

    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(normalized)
        block = normalized[header.start() : min(end, header.start() + 2600)]
        timestamp_match = timestamp_re.search(block)
        if timestamp_match is None:
            continue
        transaction_match = transaction_re.search(block, timestamp_match.end())
        if transaction_match is None:
            continue
        transaction_id = transaction_match.group("transaction").translate(
            str.maketrans({"O": "0", "o": "0"})
        )
        if not transaction_id.isdigit():
            continue

        amount_matches = list(
            amount_re.finditer(
                block,
                timestamp_match.end(),
                transaction_match.start(),
            )
        )
        if not amount_matches:
            continue
        try:
            amount = canonical_money(amount_matches[-1].group("amount"))
            values = {
                name: int(timestamp_match.group(name))
                for name in ("month", "day", "hour", "minute", "second")
            }
            stamp = int(
                datetime(
                    current.year,
                    values["month"],
                    values["day"],
                    values["hour"],
                    values["minute"],
                    values["second"],
                    tzinfo=timezone,
                ).timestamp()
            )
            if stamp > trigger_time + 86400:
                stamp = int(
                    datetime(
                        current.year - 1,
                        values["month"],
                        values["day"],
                        values["hour"],
                        values["minute"],
                        values["second"],
                        tzinfo=timezone,
                    ).timestamp()
                )
        except (OverflowError, ValueError):
            continue

        receipt = ServiceNotificationReceipt(
            occurred_at=stamp,
            amount=amount,
            external_txn_id=transaction_id,
            raw_text=block,
        )
        previous = receipts.get(transaction_id)
        if previous is None or len(receipt.raw_text) > len(previous.raw_text):
            receipts[transaction_id] = receipt

    return sorted(
        receipts.values(),
        key=lambda receipt: (receipt.occurred_at, receipt.external_txn_id),
        reverse=True,
    )


class ReceiptParser:
    def __init__(self, config: Mapping[str, Any]):
        parser = config.get("parser", {})
        if not isinstance(parser, Mapping):
            parser = {}
        self.pattern = re.compile(
            str(parser.get("receipt_pattern") or DEFAULT_RECEIPT_PATTERN),
            re.IGNORECASE,
        )
        self.max_age = int(parser.get("max_event_age_seconds", 180))
        self.max_future = int(parser.get("max_future_seconds", 60))
        self.include_raw_text = parser.get("include_raw_ocr_text") is True
        self.timezone = ZoneInfo(str(parser.get("timezone") or "Asia/Shanghai"))
        self.agent_id = str(config["agent"]["id"])
        self.channel_id = str(config["channel"]["id"])
        self.provider = str(config["channel"].get("provider") or "wxpay")

    def _timestamp(self, match: re.Match[str], trigger_time: int) -> int:
        now = datetime.fromtimestamp(trigger_time, self.timezone)
        month, day, hour, minute = [
            int(match.group(name)) for name in ("month", "day", "hour", "minute")
        ]
        stamp = int(datetime(now.year, month, day, hour, minute, tzinfo=self.timezone).timestamp())
        if stamp > trigger_time + 86400:
            stamp = int(datetime(now.year - 1, month, day, hour, minute, tzinfo=self.timezone).timestamp())
        return stamp

    def _clock_timestamp(self, hour: int, minute: int, trigger_time: int) -> int:
        """Resolve a service-account card's HH:MM clock to today or yesterday."""
        now = datetime.fromtimestamp(trigger_time, self.timezone)
        stamp = int(
            datetime(
                now.year,
                now.month,
                now.day,
                hour,
                minute,
                tzinfo=self.timezone,
            ).timestamp()
        )
        if stamp > trigger_time + self.max_future:
            stamp -= 86400
        return stamp

    def parse_all(
        self,
        text: str,
        *,
        trigger_time: int,
        trigger_signature: str,
        source: str,
        ignore_freshness: bool = False,
    ) -> tuple[list[tuple[PaymentEvent, str]], str]:
        normalized = normalize_ocr_text(text)
        candidates: list[tuple[int, str]] = []
        for match in self.pattern.finditer(normalized):
            try:
                candidates.append((self._timestamp(match, trigger_time), canonical_money(match.group("amount"))))
            except (ValueError, OverflowError):
                continue

        # Keep OCR scroll captures independent so a clock from one screenshot
        # is never joined to a receipt card in the next screenshot.
        for segment in text.split(OCR_CAPTURE_SEPARATOR):
            for receipt in merchant_text_receipts(segment):
                hour_text, minute_text = receipt.clock.split(":", 1)
                try:
                    candidates.append(
                        (
                            self._clock_timestamp(
                                int(hour_text),
                                int(minute_text),
                                trigger_time,
                            ),
                            canonical_money(receipt.amount),
                        )
                    )
                except (ValueError, OverflowError):
                    continue

        if OCR_CAPTURE_SEPARATOR in text:
            header_re = re.compile(
                r"(?:经营码)?收款到账通知.{0,160}?"
                r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
                r"(?P<hour>\d{1,2}):(?P<minute>\d{2})",
                re.IGNORECASE,
            )
            dated_amount_re = re.compile(
                r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日"
                r"(?P<hour>\d{1,2}):(?P<minute>\d{2}).{0,260}?"
                r"(?:收款金额[￥¥YV]?|[￥¥YV])"
                r"(?P<amount>[0-9Oo]+[.．。][0-9Oo]{1,2})",
                re.IGNORECASE,
            )
            header_stamps: list[tuple[int, int]] = []
            dated_amounts: list[tuple[int, int, str]] = []
            for index, segment in enumerate(text.split(OCR_CAPTURE_SEPARATOR)):
                normalized_segment = normalize_ocr_text(segment)
                for match in header_re.finditer(normalized_segment):
                    try:
                        header_stamps.append(
                            (index, self._timestamp(match, trigger_time))
                        )
                    except (ValueError, OverflowError):
                        continue
                for match in dated_amount_re.finditer(normalized_segment):
                    try:
                        dated_amounts.append(
                            (
                                index,
                                self._timestamp(match, trigger_time),
                                canonical_money(match.group("amount")),
                            )
                        )
                    except (ValueError, OverflowError):
                        continue
            for amount_index, occurred_at, amount in dated_amounts:
                if any(
                    header_time == occurred_at
                    and abs(header_index - amount_index) <= 1
                    for header_index, header_time in header_stamps
                ):
                    candidates.append((occurred_at, amount))
        if not candidates:
            return [], "pattern_not_found"

        unique_candidates = sorted(set(candidates))
        events: list[tuple[PaymentEvent, str]] = []
        stale_reasons: list[str] = []
        for occurred_at, amount in unique_candidates:
            age = trigger_time - occurred_at
            if not ignore_freshness and (age > self.max_age or age < -self.max_future):
                stale_reasons.append(f"stale age_seconds={age} amount={amount}")
                continue
            receipt_key = f"{self.provider}|{self.channel_id}|{amount}|{occurred_at}"
            identity = "|".join((receipt_key, trigger_signature, self.agent_id))
            event_id = "evt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
            shown = datetime.fromtimestamp(occurred_at, self.timezone).strftime("%m-%d %H:%M:%S")
            raw_text = normalized[:4000] if self.include_raw_text else f"微信收款到账 ￥{amount}元 时间 {shown}"
            events.append((
                PaymentEvent(
                    event_id=event_id,
                    provider=self.provider,
                    channel_id=self.channel_id,
                    amount=amount,
                    occurred_at=occurred_at,
                    external_txn_id=None,
                    trade_no=None,
                    payer=None,
                    raw_text=raw_text,
                    source=source,
                    agent_id=self.agent_id,
                ),
                receipt_key,
            ))
        return events, stale_reasons[0] if stale_reasons and not events else ""

    def parse(
        self,
        text: str,
        *,
        trigger_time: int,
        trigger_signature: str,
        source: str,
        ignore_freshness: bool = False,
    ) -> tuple[PaymentEvent | None, str]:
        """Return the receipt nearest the trigger for API compatibility."""
        events, reason = self.parse_all(
            text,
            trigger_time=trigger_time,
            trigger_signature=trigger_signature,
            source=source,
            ignore_freshness=ignore_freshness,
        )
        if not events:
            return None, reason
        return min(events, key=lambda row: abs(trigger_time - row[0].occurred_at))

    def events_from_service_notifications(
        self,
        receipts: Sequence[ServiceNotificationReceipt],
        *,
        trigger_time: int,
        source: str,
        known_transaction_ids: Sequence[str] = (),
        ignore_freshness: bool = False,
    ) -> tuple[list[tuple[PaymentEvent, str]], str]:
        """Build stable events for detailed cards not present in the cursor."""

        known = set(known_transaction_ids)
        emitted: set[str] = set()
        events: list[tuple[PaymentEvent, str]] = []
        stale_reasons: list[str] = []
        for receipt in sorted(
            receipts,
            key=lambda item: (item.occurred_at, item.external_txn_id),
        ):
            transaction_id = receipt.external_txn_id
            if transaction_id in known or transaction_id in emitted:
                continue
            emitted.add(transaction_id)
            age = trigger_time - receipt.occurred_at
            if not ignore_freshness and (
                age > self.max_age or age < -self.max_future
            ):
                stale_reasons.append(
                    f"stale age_seconds={age} transaction_sha256="
                    + hashlib.sha256(transaction_id.encode()).hexdigest()[:12]
                )
                continue

            receipt_key = (
                f"{self.provider}|{self.channel_id}|txn|{transaction_id}"
            )
            event_id = "evt_" + hashlib.sha256(
                receipt_key.encode("utf-8")
            ).hexdigest()[:32]
            shown = datetime.fromtimestamp(
                receipt.occurred_at,
                self.timezone,
            ).strftime("%m-%d %H:%M:%S")
            raw_text = (
                receipt.raw_text[:4000]
                if self.include_raw_text
                else f"微信收款通知 ￥{receipt.amount}元 时间 {shown}"
            )
            events.append(
                (
                    PaymentEvent(
                        event_id=event_id,
                        provider=self.provider,
                        channel_id=self.channel_id,
                        amount=receipt.amount,
                        occurred_at=receipt.occurred_at,
                        external_txn_id=transaction_id,
                        trade_no=None,
                        payer=None,
                        raw_text=raw_text,
                        source=source,
                        agent_id=self.agent_id,
                    ),
                    receipt_key,
                )
            )
        return events, stale_reasons[0] if stale_reasons and not events else ""


class CaptureTriggerStore:
    """Single-slot durable trigger that survives an agent process restart."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def load(self) -> CaptureTrigger | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return CaptureTrigger(
                signature=str(value["signature"]),
                trigger_time=int(value["trigger_time"]),
                reason=str(value["reason"]),
                scheduled_at=int(value["scheduled_at"]),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            LOG.error("invalid_capture_trigger file=%s error=%s", self.path, exc)
            return None

    def schedule(
        self,
        *,
        signature: str,
        trigger_time: int,
        reason: str,
    ) -> CaptureTrigger:
        trigger = CaptureTrigger(
            signature=str(signature),
            trigger_time=int(trigger_time),
            reason=str(reason),
            scheduled_at=int(time.time()),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(trigger), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        return trigger

    def clear(self, expected_signature: str | None = None) -> None:
        if expected_signature is not None:
            current = self.load()
            if current and current.signature != expected_signature:
                return
        self.path.unlink(missing_ok=True)


class EventSpool:
    def __init__(self, root: Path):
        self.root = root
        self.pending = root / "pending"
        self.processed = root / "processed"
        self.rejected = root / "rejected"
        for directory in (self.pending, self.processed, self.rejected):
            directory.mkdir(parents=True, exist_ok=True)

    def put(self, event: PaymentEvent) -> None:
        target = self.pending / f"{event.event_id}.json"
        if target.exists():
            return
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(event.payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)

    def load(self) -> list[PaymentEvent]:
        fields = set(PaymentEvent.__dataclass_fields__)
        result: list[PaymentEvent] = []
        for path in sorted(self.pending.glob("evt_*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                result.append(PaymentEvent(**{key: value.get(key) for key in fields}))
            except Exception as exc:
                LOG.error("invalid_spool file=%s error=%s", path.name, exc)
                os.replace(path, self.rejected / path.name)
        return result

    def acknowledge(self, event: PaymentEvent) -> None:
        source = self.pending / f"{event.event_id}.json"
        if source.exists():
            os.replace(source, self.processed / source.name)

    def reject(self, event: PaymentEvent, reason: str) -> None:
        source = self.pending / f"{event.event_id}.json"
        if source.exists():
            os.replace(source, self.rejected / source.name)
        reason_path = self.rejected / f"{event.event_id}.reason.txt"
        temporary = reason_path.with_suffix(".tmp")
        temporary.write_text(
            f"{int(time.time())} {reason.strip()[:500]}\n",
            encoding="utf-8",
        )
        os.replace(temporary, reason_path)


class ReceiptDedupe:
    def __init__(self, path: Path, ttl_seconds: int = 86400):
        self.path = path
        self.ttl_seconds = ttl_seconds
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.values = {str(key): int(value) for key, value in raw.items()}
        except (OSError, ValueError, TypeError):
            self.values: dict[str, int] = {}
        self._prune()

    def _prune(self) -> None:
        cutoff = int(time.time()) - self.ttl_seconds
        self.values = {key: value for key, value in self.values.items() if value >= cutoff}

    def contains(self, key: str) -> bool:
        self._prune()
        return key in self.values

    def add(self, key: str) -> None:
        self.values[key] = int(time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


class ServiceNotificationState:
    """Persistent transaction-ID anchor for height-independent chat scans."""

    def __init__(self, path: Path, max_transaction_ids: int = 512):
        self.path = path.expanduser().resolve()
        self.max_transaction_ids = max(32, int(max_transaction_ids))
        self.transaction_ids: tuple[str, ...] = ()
        self.initialized = False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            values = raw.get("transaction_ids") if isinstance(raw, dict) else None
            if isinstance(values, list):
                cleaned: list[str] = []
                for value in values:
                    transaction_id = str(value).strip()
                    if (
                        transaction_id.isdigit()
                        and transaction_id.startswith("4")
                        and 24 <= len(transaction_id) <= 36
                        and transaction_id not in cleaned
                    ):
                        cleaned.append(transaction_id)
                self.transaction_ids = tuple(
                    cleaned[: self.max_transaction_ids]
                )
                self.initialized = bool(raw.get("initialized", True))
        except (OSError, ValueError, TypeError):
            pass

    def commit(self, transaction_ids: Sequence[str]) -> None:
        combined: list[str] = []
        for value in (*transaction_ids, *self.transaction_ids):
            transaction_id = str(value).strip()
            if (
                transaction_id.isdigit()
                and transaction_id.startswith("4")
                and 24 <= len(transaction_id) <= 36
                and transaction_id not in combined
            ):
                combined.append(transaction_id)
        self.transaction_ids = tuple(
            combined[: self.max_transaction_ids]
        )
        self.initialized = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "initialized": True,
                    "transaction_ids": list(self.transaction_ids),
                    "updated_at": int(time.time()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


class BridgeClient:
    def __init__(self, config: Mapping[str, Any]):
        bridge = config["bridge"]
        self.url = str(bridge["url"])
        self.token = secret_from_config(config)
        self.timeout = float(bridge.get("timeout_seconds", 10))
        self.user_agent = str(
            bridge.get("user_agent")
            or "Simple-WeChat-Payment-Receiver/1.3"
        )

    def send(self, event: PaymentEvent) -> dict[str, Any]:
        data = json.dumps(event.payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        signed_at = str(int(time.time()))
        signature = hmac.new(
            self.token.encode("utf-8"), signed_at.encode("ascii") + b"." + data, hashlib.sha256
        ).hexdigest()
        request = urllib.request.Request(self.url, data=data, method="POST", headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "X-Bridge-Token": self.token,
            "X-Bridge-Event-Id": event.event_id,
            "X-Bridge-Timestamp": signed_at,
            "X-Bridge-Signature": signature,
            "User-Agent": self.user_agent,
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    return {
                        "ok": False,
                        "http_status": response.status,
                        "reason": "invalid_bridge_response",
                        "message": body[:300],
                    }
                if not isinstance(payload, dict):
                    return {
                        "ok": False,
                        "http_status": response.status,
                        "reason": "invalid_bridge_response",
                        "message": "response JSON root is not an object",
                    }
                payload.setdefault("http_status", response.status)
                return payload
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"message": body[:500]}
            payload["http_status"] = exc.code
            return payload
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"ok": False, "http_status": 0, "reason": "transport_error", "message": str(exc)[:300]}


class AgentRuntime:
    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        runtime = config.get("runtime", {})
        self.spool = EventSpool(resolve_path(str(runtime.get("spool_dir") or "spool"), config))
        self.dedupe = ReceiptDedupe(resolve_path(str(runtime.get("dedupe_file") or "spool/receipt-dedupe.json"), config))
        self.client = BridgeClient(config)
        self.pending: dict[str, tuple[PaymentEvent, float, int, str | None]] = {
            event.event_id: (event, 0.0, 0, None) for event in self.spool.load()
        }
        self.max_retry_seconds = float(runtime.get("max_retry_seconds", 300))
        self.no_candidate_max_age_seconds = max(
            300, int(runtime.get("no_candidate_max_age_seconds", 2100))
        )
        self.retry_max_age_seconds = max(
            self.no_candidate_max_age_seconds,
            int(runtime.get("retry_max_age_seconds", 86400)),
        )

    def queue(self, event: PaymentEvent, receipt_key: str) -> bool:
        if self.dedupe.contains(receipt_key):
            display_key = receipt_key
            if "|txn|" in receipt_key:
                display_key = "transaction-sha256:" + hashlib.sha256(
                    receipt_key.encode("utf-8")
                ).hexdigest()[:12]
            LOG.info("receipt_duplicate key=%s", display_key)
            return False
        self.spool.put(event)
        self.pending[event.event_id] = (event, 0.0, 0, None)
        self.dedupe.add(receipt_key)
        return True

    def _deliver_one(self, event_id: str, now_mono: float) -> None:
        permanent = {"channel_disabled", "channel_provider_mismatch", "invalid_event"}
        item = self.pending.get(event_id)
        if item is None:
            return
        event, next_attempt, attempts, last_reason = item
        if now_mono < next_attempt:
            return
        age = max(0, int(time.time()) - int(event.occurred_at))
        expired_reason = ""
        if age > self.retry_max_age_seconds:
            expired_reason = f"retry_expired age_seconds={age}"
        elif last_reason == "no_candidate" and age > self.no_candidate_max_age_seconds:
            expired_reason = f"no_candidate_expired age_seconds={age}"
        if expired_reason:
            self.spool.reject(event, expired_reason)
            del self.pending[event_id]
            LOG.warning("event_archived event=%s reason=%s", event_id, expired_reason)
            return

        result = self.client.send(event)
        LOG.info("event=%s agent_id=%s amount=%s result=%s", event_id, event.agent_id, event.amount,
                 json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        if result.get("ok") is True:
            self.spool.acknowledge(event)
            del self.pending[event_id]
            return
        reason = str(result.get("reason") or result.get("result") or "unknown")
        if reason in permanent:
            self.spool.reject(event, reason)
            del self.pending[event_id]
            LOG.error("event_rejected event=%s reason=%s", event_id, reason)
            return
        attempts += 1
        delay = min(self.max_retry_seconds, float(2 ** min(attempts, 8)))
        self.pending[event_id] = (event, now_mono + delay, attempts, reason)
        LOG.warning(
            "event_retry event=%s attempts=%s delay=%s reason=%s",
            event_id,
            attempts,
            int(delay),
            reason,
        )

    def deliver_ids(self, event_ids: Iterable[str], now_mono: float) -> None:
        for event_id in event_ids:
            self._deliver_one(event_id, now_mono)

    def deliver_due(self, now_mono: float) -> None:
        for event_id in list(self.pending):
            self._deliver_one(event_id, now_mono)


FileSignature = tuple[int, int, int, int]


def file_signature(path: Path) -> FileSignature | None:
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def format_file_signature(signature: FileSignature) -> str:
    return ":".join(str(value) for value in signature)


def trigger_file_change(
    previous: FileSignature | None,
    current: FileSignature | None,
) -> tuple[str, str] | None:
    """Describe a trigger-file change, including missing-to-present."""

    if current is None or current == previous:
        return None
    reason = "trigger_file_reappeared" if previous is None else "trigger_file_changed"
    return format_file_signature(current), reason


def setup_logging(config: Mapping[str, Any], verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
