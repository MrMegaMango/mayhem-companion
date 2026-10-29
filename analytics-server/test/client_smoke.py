"""Run the real desktop client against local workerd/D1 with synthetic state.

Start wrangler dev --local --ip 127.0.0.1 --port 8795 first. This test injects
the HTTP loopback boundary; it does not weaken the app's HTTPS-only transport.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from urllib import error, request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from mayhem.analytics import Analytics


class Clock:
    monotonic = 1000.0
    wall = time.time()

    def advance(self, seconds):
        self.monotonic += seconds
        self.wall += seconds


def eventually(check, label, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.02)
    raise AssertionError(label)


def database():
    for path in (ROOT / "analytics-server/.wrangler/state/v3/d1").rglob("*.sqlite"):
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        found = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_events'").fetchone()
        if found:
            return connection
        connection.close()
    raise RuntimeError("The local D1 migration has not been applied.")


def main():
    calls = []
    clock = Clock()
    opener = request.build_opener(request.ProxyHandler({}))

    def transport(method, path, payload, token):
        calls.append((method, path.split("/")[2], threading.current_thread().name))
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = request.Request("http://127.0.0.1:8795" + path, body, headers, method=method)
        try:
            with opener.open(req, timeout=5) as response:
                return response.status
        except error.HTTPError as exc:
            status = exc.code
            exc.close()
            return status

    with tempfile.TemporaryDirectory(prefix="mayhem-usage-smoke-") as directory, database() as db:
        client = Analytics(Path(directory), "https://local-analytics.example", "0.6.0-smoke",
                           _transport=transport, _clock=lambda: clock.monotonic,
                           _wall_clock=lambda: clock.wall, _flush_interval=0.05,
                           _jitter=lambda: 0)
        ident = token = None
        try:
            client.set_usage(False, "mayhem")
            assert client.status()["installation_id"] is None and calls == []
            client.set_enabled(True)
            eventually(lambda: client.status()["registered"], "Registration did not finish")
            ident = client.status()["installation_id"]
            token = json.loads(client.path.read_text())["token"]
            client.set_usage(True, "mayhem")
            clock.advance(125)
            client.set_usage(True, "arena")
            client.set_usage(False, "arena")
            clock.advance(1)
            client._wake.set()
            eventually(lambda: client.status()["queued"] == 0, "Events did not flush")
            rows = db.execute("SELECT name, mode, duration_minutes, app_version FROM usage_events WHERE installation_id = ? ORDER BY rowid", (ident,)).fetchall()
            assert rows == [("session_start", "mayhem", 0, "0.6.0-smoke"),
                            ("mode_used", "arena", 2, "0.6.0-smoke"),
                            ("session_end", "arena", 2, "0.6.0-smoke")], rows
            digest = db.execute("SELECT token_hash FROM installations WHERE installation_id = ?", (ident,)).fetchone()[0]
            assert digest == hashlib.sha256(token.encode()).hexdigest()
            assert all(call[2] == "mayhem-analytics" for call in calls)
            before = len(calls)
            client.set_enabled(False)
            client.set_usage(True, "tft")
            client.set_usage(False, "tft")
            time.sleep(0.1)
            assert len(calls) == before and client.status()["queued"] == 0
            client.delete_data()
            eventually(lambda: client.status()["installation_id"] is None and not client.status()["deletion_pending"], "Deletion did not finish")
            assert db.execute("SELECT COUNT(*) FROM usage_events WHERE installation_id = ?", (ident,)).fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM installations WHERE installation_id = ?", (ident,)).fetchone()[0] == 0
            id_hash = hashlib.sha256(ident.encode()).hexdigest()
            assert db.execute("SELECT COUNT(*) FROM deletion_tombstones WHERE installation_hash = ?", (id_hash,)).fetchone()[0] == 1
            assert transport("POST", "/v1/installations", {"installation_id": ident, "token": token}, None) == 410
            print("Real client -> Worker -> D1 passed: opt-in, visible session, mode change, 2-minute duration, batched upload, opt-out, deletion, and replay suppression. D1: 0 test events, 0 test installations; temporary hash-only deletion marker remains.")
        finally:
            client.close()
            if ident and token:
                transport("DELETE", "/v1/installations/" + ident, None, token)


if __name__ == "__main__":
    main()
