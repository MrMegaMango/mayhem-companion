"""Privacy, offline delivery, and teardown contracts for optional analytics."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib import error

from mayhem.analytics import Analytics, HttpTransport, MAX_EVENTS

ENDPOINT = "https://mayhem-usage.example.com"


def eventually(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected asynchronous result was not observed")


class Clock:
    def __init__(self):
        self.monotonic = 1000.0
        self.wall = datetime(2026, 10, 29, 1, 1, 35, tzinfo=timezone.utc).timestamp()

    def advance(self, seconds, *, wall=True):
        self.monotonic += seconds
        if wall:
            self.wall += seconds


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name)
        self.clock = Clock()
        self.calls = []
        self.transport = Mock(side_effect=self.deliver)
        self.instances = []
        self.addCleanup(lambda: [instance.close() for instance in self.instances])

    def deliver(self, method, path, payload, token):
        self.calls.append((method, path, payload, token, threading.current_thread().name))
        return 204 if method == "DELETE" else 201 if path == "/v1/installations" else 202

    def make(self, **kwargs):
        defaults = dict(_transport=self.transport, _clock=lambda: self.clock.monotonic,
                        _wall_clock=lambda: self.clock.wall, _flush_interval=300,
                        _jitter=lambda: 0)
        defaults.update(kwargs)
        app = Analytics(self.storage, defaults.pop("endpoint", ENDPOINT), "0.6.0", **defaults)
        self.instances.append(app)
        return app

    def register(self, client):
        client.set_enabled(True)
        eventually(lambda: len(self.calls) >= 1)
        eventually(lambda: client.status()["registered"] and not client.status()["blocked"])

    def test_unset_and_disabled_collect_no_identity_or_events_and_make_no_requests(self):
        client = self.make()
        client.set_usage(True, "mayhem")
        self.assertEqual(client.status()["consent"], "unset")
        self.assertIsNone(client.status()["installation_id"])
        self.assertFalse(client.path.exists())
        self.assertIsNone(client._thread)
        client.set_enabled(False)
        self.assertIsNone(client.status()["installation_id"])
        self.assertEqual(client.status()["queued"], 0)
        self.transport.assert_not_called()

    def test_missing_or_invalid_endpoint_cannot_enable(self):
        for endpoint in ("", "http://example.com", "https://localhost", "https://127.0.0.1",
                         "https://example.com/path", "https://user:pass@example.com",
                         "https://example.com?token=secret", "https://example.com#fragment"):
            with self.subTest(endpoint=endpoint):
                client = self.make(endpoint=endpoint)
                client.set_enabled(True)
                self.assertFalse(client.status()["configured"])
                self.assertIsNone(client.status()["installation_id"])
        self.transport.assert_not_called()

    def test_explicit_enable_creates_persistent_random_identity_and_worker_only_http(self):
        client = self.make()
        self.register(client)
        state = json.loads(client.path.read_text())
        self.assertEqual(len(state["token"]), 43)
        self.assertNotIn("token", client.status())
        self.assertEqual(self.calls[0][4], "mayhem-analytics")
        ident = client.status()["installation_id"]
        client.close()
        again = self.make()
        self.assertEqual(again.status()["installation_id"], ident)
        self.assertEqual(json.loads(again.path.read_text())["token"], state["token"])

    def test_visible_time_only_duration_uses_monotonic_and_unknown_modes_are_sanitized(self):
        client = self.make()
        self.register(client)
        self.clock.advance(120)
        self.assertEqual(client.status()["queued"], 0)
        client.set_usage(True, "mayhem")
        self.clock.advance(125, wall=False)
        self.clock.wall -= 120
        client.set_usage(True, "Riot ID / private payload")
        client.set_usage(False, "unknown")
        with client._lock:
            events = list(client._state["events"])
        self.assertEqual([event["name"] for event in events], ["session_start", "mode_used", "session_end"])
        self.assertEqual(events[-1]["duration_minutes"], 2)
        self.assertEqual(events[-1]["mode"], "unknown")
        self.assertTrue(events[0]["occurred_at"].endswith(":00Z"))
        self.assertNotIn("Riot ID", str(events))

    def test_opt_out_immediately_discards_unsent_data_but_retains_id_for_explicit_reenable(self):
        client = self.make()
        self.register(client)
        ident = client.status()["installation_id"]
        client.set_usage(True, "arena")
        client.set_enabled(False)
        self.clock.advance(1000)
        client._wake.set()
        time.sleep(0.05)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(client.status()["queued"], 0)
        saved = json.loads(client.path.read_text())
        self.assertEqual(saved["consent"], "disabled")
        self.assertEqual(saved["events"], [])
        self.assertEqual(saved["installation_id"], ident)

    def test_failed_opt_out_replacement_cannot_restore_old_consent_on_restart(self):
        client = self.make()
        self.register(client)
        with patch("mayhem.analytics.os.replace", side_effect=OSError):
            client.set_enabled(False)
            client.close()
            self.assertEqual(json.loads(client.path.read_text())["consent"], "enabled")
            before = len(self.calls)
            again = self.make()
            self.assertEqual(again.status()["consent"], "disabled")
            self.assertEqual(again.status()["queued"], 0)
            self.assertEqual(len(self.calls), before)
        again.set_enabled(True)
        self.assertEqual(again.status()["consent"], "enabled")
        self.assertFalse((self.storage / "analytics.revoked").exists())

    def test_delete_request_survives_failed_state_replacement_and_restart(self):
        client = self.make()
        self.register(client)
        client.set_enabled(False)
        client.close()
        disabled = self.make()
        self.assertIsNone(disabled._thread)
        with patch("mayhem.analytics.os.replace", side_effect=OSError):
            disabled.delete_data()
            self.assertIsNotNone(disabled._thread)
            disabled.close()
            self.assertFalse(json.loads(disabled.path.read_text())["deletion_pending"])
            again = self.make()
            self.assertTrue(again.status()["deletion_pending"])
        self.clock.advance(31)
        again._wake.set()
        eventually(lambda: not again.status()["deletion_pending"])
        self.assertEqual(self.calls[-1][0], "DELETE")

    def test_queue_is_bounded_and_old_data_expires(self):
        client = self.make()
        self.register(client)
        client.set_usage(True, "aram")
        for index in range(MAX_EVENTS + 30):
            client.set_usage(True, "arena" if index % 2 else "tft")
        self.assertLessEqual(client.status()["queued"], MAX_EVENTS)
        self.clock.advance(8 * 86400)
        client.set_usage(False, "tft")
        with client._lock:
            events = list(client._state["events"])
        self.assertLessEqual(len(events), 2)
        self.assertTrue(all(event["duration_minutes"] <= 1440 for event in events))

    def test_heartbeat_records_continued_use_after_utc_midnight(self):
        client = self.make()
        self.register(client)
        self.clock.wall = datetime(2026, 10, 29, 23, 55, tzinfo=timezone.utc).timestamp()
        client.set_usage(True, "mayhem")
        self.clock.advance(900)
        client._wake.set()
        eventually(lambda: any(call[1] == "/v1/events" for call in self.calls))
        events = next(call[2]["events"] for call in self.calls if call[1] == "/v1/events")
        heartbeat = next(event for event in events if event["name"] == "session_heartbeat")
        self.assertEqual(heartbeat["occurred_at"], "2026-10-30T00:10:00Z")
        self.assertEqual(heartbeat["duration_minutes"], 15)

    def test_persistence_failure_never_sends_and_can_recover_without_an_opt_in(self):
        client = self.make()
        with patch("mayhem.analytics.os.replace", side_effect=OSError("private path")):
            client.set_enabled(True)
            self.assertEqual(client.status()["consent"], "disabled")
            self.assertTrue(client.status()["blocked"])
            self.assertNotIn("private path", client.status()["last_error"])
            self.transport.assert_not_called()
        self.clock.advance(31)
        client._wake.set()
        eventually(lambda: not client.status()["blocked"])
        self.assertEqual(json.loads(client.path.read_text())["consent"], "disabled")
        self.transport.assert_not_called()

    def test_failed_upload_retries_same_event_ids_without_losing_queue(self):
        client = self.make()
        self.register(client)
        client.set_usage(True, "arena")
        self.transport.side_effect = lambda *args: self.deliver(*args) if args[1] != "/v1/events" else 503
        self.clock.advance(301)
        client._wake.set()
        eventually(lambda: "unavailable" in client.status()["last_error"])
        with client._lock:
            expected = [event["event_id"] for event in client._state["events"]]
        self.transport.side_effect = self.deliver
        self.clock.advance(31)
        client._wake.set()
        eventually(lambda: client.status()["queued"] == 0)
        delivered = next(call[2]["events"] for call in self.calls if call[1] == "/v1/events")
        self.assertEqual([event["event_id"] for event in delivered], expected)

    def test_delete_disables_drops_queue_and_clears_identity_only_after_acknowledgment(self):
        client = self.make()
        self.register(client)
        ident = client.status()["installation_id"]
        client.set_usage(True, "mayhem")
        self.transport.side_effect = lambda *args: 503 if args[0] == "DELETE" else self.deliver(*args)
        client.delete_data()
        eventually(lambda: "Deletion is pending" in client.status()["last_error"])
        self.assertEqual(client.status()["consent"], "disabled")
        self.assertEqual(client.status()["queued"], 0)
        self.assertEqual(client.status()["installation_id"], ident)
        self.assertTrue(json.loads(client.path.read_text())["deletion_pending"])
        client.set_enabled(True)
        self.assertEqual(client.status()["consent"], "disabled")
        self.transport.side_effect = self.deliver
        self.clock.advance(31)
        client._wake.set()
        eventually(lambda: not client.status()["deletion_pending"])
        self.assertIsNone(client.status()["installation_id"])
        self.assertIsNone(json.loads(client.path.read_text())["token"])
        client.set_enabled(True)
        self.assertNotEqual(client.status()["installation_id"], ident)

    def test_pending_deletion_survives_restart_while_consent_stays_off(self):
        client = self.make()
        self.register(client)
        self.transport.side_effect = lambda *args: 503
        client.delete_data()
        eventually(lambda: "Deletion is pending" in client.status()["last_error"])
        client.close()
        self.transport.side_effect = self.deliver
        again = self.make()
        eventually(lambda: not again.status()["deletion_pending"])
        self.assertEqual(again.status()["consent"], "disabled")
        self.assertEqual(self.calls[-1][0], "DELETE")

    def test_delete_waits_for_inflight_registration_then_sends_no_usage(self):
        entered, release = threading.Event(), threading.Event()
        def block(method, path, payload, token):
            if path == "/v1/installations":
                entered.set()
                release.wait(2)
            return self.deliver(method, path, payload, token)
        client = self.make(_transport=block)
        client.set_enabled(True)
        self.assertTrue(entered.wait(1))
        client.set_usage(True, "aram")
        client.delete_data()
        self.assertTrue(client.status()["deletion_pending"])
        self.assertEqual(client.status()["queued"], 0)
        release.set()
        eventually(lambda: not client.status()["deletion_pending"])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1], "/v1/installations")
        self.assertEqual(self.calls[-1][0], "DELETE")

    def test_rejected_identity_pauses_without_silently_registering_new_one(self):
        client = self.make(_transport=lambda *args: 410)
        client.set_enabled(True)
        ident = client.status()["installation_id"]
        eventually(lambda: client.status()["blocked"])
        self.assertEqual(client.status()["consent"], "disabled")
        client.set_enabled(True)
        self.assertEqual(client.status()["installation_id"], ident)
        self.assertEqual(client.status()["consent"], "disabled")

    def test_corrupt_or_extra_state_fields_fail_closed(self):
        client = self.make()
        self.register(client)
        client.close()
        state = json.loads(client.path.read_text())
        state["summoner"] = "do not share"
        client.path.write_text(json.dumps(state))
        self.calls.clear()
        again = self.make()
        self.assertEqual(again.status()["consent"], "unset")
        self.assertIsNone(again.status()["installation_id"])
        self.assertIsNone(again._thread)

    def test_destination_change_pauses_and_only_deletes_at_original_destination(self):
        client = self.make()
        self.register(client)
        client.close()
        again = self.make(endpoint="https://new-usage.example.com")
        self.assertTrue(again.status()["blocked"])
        self.assertEqual(again.status()["consent"], "disabled")
        with patch("mayhem.analytics.HttpTransport") as factory:
            again._injected_transport = None
            factory.return_value.return_value = 204
            again.delete_data()
            eventually(lambda: not again.status()["deletion_pending"])
            factory.assert_called_once_with(ENDPOINT)

    def test_close_returns_promptly_and_persists_final_session_without_waiting_for_http(self):
        client = self.make()
        self.register(client)
        client.set_usage(True, "tft")
        self.clock.advance(65)
        before = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - before, 0.2)
        saved = json.loads(client.path.read_text())
        self.assertEqual(saved["events"][-1]["name"], "session_end")
        self.assertEqual(saved["events"][-1]["duration_minutes"], 1)


class TransportTests(unittest.TestCase):
    def test_verified_tls_no_environment_proxy_and_no_redirect_handler(self):
        with patch("mayhem.analytics.request.build_opener") as build, \
                patch("mayhem.analytics.ssl.create_default_context") as context:
            HttpTransport(ENDPOINT)
        handlers = build.call_args.args
        self.assertTrue(any(getattr(handler, "proxies", None) == {} for handler in handlers))
        self.assertTrue(context.called)
        redirect = next(handler for handler in handlers if type(handler).__name__ == "_NoRedirect")
        with self.assertRaises(error.HTTPError):
            redirect.redirect_request(Mock(full_url=ENDPOINT), None, 302, "", {}, "https://elsewhere.example/")

    def test_token_only_in_header_and_bounded_timeout(self):
        transport = HttpTransport(ENDPOINT)
        response = Mock(status=202)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        transport.opener = Mock()
        transport.opener.open.return_value = response
        token = "A" * 43
        self.assertEqual(transport("POST", "/v1/events", {"events": []}, token), 202)
        req = transport.opener.open.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer " + token)
        self.assertNotIn(token, req.full_url)
        self.assertNotIn(token, req.data.decode())
        self.assertEqual(transport.opener.open.call_args.kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
