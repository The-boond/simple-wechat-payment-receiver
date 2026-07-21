#!/usr/bin/env python3
"""Minimal HMAC-verifying event sink for integration testing.

It stores accepted events in SQLite and deliberately does not modify orders.
Connect your own order-matching code after verification and idempotency checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


HOST = os.environ.get("WECHAT_RECEIVER_HOST", "127.0.0.1")
PORT = int(os.environ.get("WECHAT_RECEIVER_PORT", "8787"))
TOKEN = os.environ.get("WECHAT_RECEIVER_TOKEN", "")
DATABASE = Path(os.environ.get("WECHAT_RECEIVER_DATABASE", "events.sqlite3")).resolve()


def database() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, received_at INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    return connection


class Handler(BaseHTTPRequestHandler):
    server_version = "WeChatReceiverExample/1.0"

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_json(200, {"ok": True, "token_configured": bool(TOKEN)})
        else:
            self.send_json(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/event":
            self.send_json(404, {"ok": False})
            return
        if not TOKEN:
            self.send_json(503, {"ok": False, "reason": "token_not_configured"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self.send_json(400, {"ok": False, "reason": "invalid_length"})
            return
        body = self.rfile.read(length)
        event_id = self.headers.get("X-Bridge-Event-Id", "")
        signed_at = self.headers.get("X-Bridge-Timestamp", "")
        signature = self.headers.get("X-Bridge-Signature", "")
        supplied_token = self.headers.get("X-Bridge-Token", "")
        if not signed_at.isdigit() or abs(int(time.time()) - int(signed_at)) > 300:
            self.send_json(401, {"ok": False, "reason": "stale_signature"})
            return
        expected = hmac.new(TOKEN.encode(), signed_at.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied_token, TOKEN) or not hmac.compare_digest(signature, expected):
            self.send_json(401, {"ok": False, "reason": "bad_signature"})
            return
        try:
            payload = json.loads(body)
            if payload.get("event_id") != event_id or not event_id.startswith("evt_"):
                raise ValueError("event id mismatch")
            if payload.get("provider") != "wxpay" or not str(payload.get("channel_id", "")).isdigit():
                raise ValueError("invalid routing")
            amount = str(payload.get("amount", ""))
            if not amount or float(amount) <= 0:
                raise ValueError("invalid amount")
        except (ValueError, TypeError, json.JSONDecodeError):
            self.send_json(422, {"ok": False, "reason": "invalid_event"})
            return
        with database() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO events(event_id, received_at, payload) VALUES (?, ?, ?)",
                (event_id, int(time.time()), body.decode("utf-8")),
            )
        result = "accepted" if cursor.rowcount else "already_processed"
        self.send_json(200, {"ok": True, "result": result, "event_id": event_id})

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("set WECHAT_RECEIVER_TOKEN before starting the receiver")
    print(f"listening on http://{HOST}:{PORT}; database={DATABASE}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
