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
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


LOG = logging.getLogger("wechat-payment-receiver")


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

    def parse(
        self,
        text: str,
        *,
        trigger_time: int,
        trigger_signature: str,
        source: str,
        ignore_freshness: bool = False,
    ) -> tuple[PaymentEvent | None, str]:
        normalized = normalize_ocr_text(text)
        candidates: list[tuple[int, str]] = []
        for match in self.pattern.finditer(normalized):
            try:
                candidates.append((self._timestamp(match, trigger_time), canonical_money(match.group("amount"))))
            except (ValueError, OverflowError):
                continue
        if not candidates:
            return None, "pattern_not_found"
        occurred_at, amount = min(candidates, key=lambda row: abs(trigger_time - row[0]))
        age = trigger_time - occurred_at
        if not ignore_freshness and (age > self.max_age or age < -self.max_future):
            return None, f"stale age_seconds={age} amount={amount}"
        receipt_key = f"{self.provider}|{self.channel_id}|{amount}|{occurred_at}"
        identity = "|".join((receipt_key, trigger_signature, self.agent_id))
        event_id = "evt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        shown = datetime.fromtimestamp(occurred_at, self.timezone).strftime("%m-%d %H:%M:%S")
        raw_text = normalized[:4000] if self.include_raw_text else f"微信收款到账 ￥{amount}元 时间 {shown}"
        return PaymentEvent(
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
        ), receipt_key


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


class BridgeClient:
    def __init__(self, config: Mapping[str, Any]):
        bridge = config["bridge"]
        self.url = str(bridge["url"])
        self.token = secret_from_config(config)
        self.timeout = float(bridge.get("timeout_seconds", 10))
        self.user_agent = str(bridge.get("user_agent") or "Simple-WeChat-Payment-Receiver/1.0")

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
                payload = json.loads(body) if body else {}
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
        self.pending: dict[str, tuple[PaymentEvent, float, int]] = {
            event.event_id: (event, 0.0, 0) for event in self.spool.load()
        }
        self.max_retry_seconds = float(runtime.get("max_retry_seconds", 300))

    def queue(self, event: PaymentEvent, receipt_key: str) -> bool:
        if self.dedupe.contains(receipt_key):
            LOG.info("receipt_duplicate key=%s", receipt_key)
            return False
        self.spool.put(event)
        self.pending[event.event_id] = (event, 0.0, 0)
        self.dedupe.add(receipt_key)
        return True

    def deliver_due(self, now_mono: float) -> None:
        permanent = {"channel_disabled", "channel_provider_mismatch", "invalid_event"}
        for event_id, (event, next_attempt, attempts) in list(self.pending.items()):
            if now_mono < next_attempt:
                continue
            result = self.client.send(event)
            LOG.info("event=%s agent_id=%s amount=%s result=%s", event_id, event.agent_id, event.amount,
                     json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            if result.get("ok") is True or result.get("reason") in permanent:
                self.spool.acknowledge(event)
                del self.pending[event_id]
                continue
            attempts += 1
            delay = min(self.max_retry_seconds, float(2 ** min(attempts, 8)))
            self.pending[event_id] = (event, now_mono + delay, attempts)
            LOG.warning("event_retry event=%s attempts=%s delay=%s", event_id, attempts, int(delay))


def file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return None


def setup_logging(config: Mapping[str, Any], verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
