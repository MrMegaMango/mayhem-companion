"""Bounded usage analytics with saved opt-outs and one background network worker.

Installation IDs are pseudonymous, not player IDs. No game payloads, hardware
IDs, names, logs, screenshots, or free-form properties enter this module.
"""
from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import ssl
import threading
import time
from urllib import error, request
from urllib.parse import urlsplit
import uuid

MODES = frozenset(("aram", "mayhem", "arena", "tft", "unknown"))
EVENT_NAMES = frozenset(("session_start", "session_heartbeat", "session_end", "mode_used"))
EVENT_KEYS = frozenset(("event_id", "session_id", "name", "occurred_at", "app_version",
                        "mode", "duration_minutes"))
STATE_KEYS = frozenset(("schema", "endpoint", "consent", "installation_id", "token",
                       "registered", "deletion_pending", "blocked", "events"))
MAX_EVENTS = 200
MAX_AGE = 7 * 24 * 60 * 60
MAX_BATCH = 20
MAX_BODY = 24 * 1024
MAX_STATE = 128 * 1024
_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}(?:-[A-Za-z0-9.-]{1,30})?\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z\Z")


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


def normalize_endpoint(endpoint):
    """Accept a public HTTPS origin only; no credentials, paths, or redirects."""
    if not isinstance(endpoint, str) or not endpoint:
        return ""
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or parsed.port not in (None, 443) or "." not in host
                or host.endswith((".local", ".localhost", ".internal"))
                or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)):
            return ""
        try:
            ipaddress.ip_address(host)
            return ""
        except ValueError:
            pass
        return "https://" + host.lower()
    except (TypeError, ValueError):
        return ""


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


class HttpTransport:
    """The callable boundary is (method, path, JSON body or None, token or None)."""

    def __init__(self, endpoint):
        self.endpoint = normalize_endpoint(endpoint)
        if not self.endpoint:
            raise ValueError("A public HTTPS analytics origin is required")
        self.opener = request.build_opener(
            request.ProxyHandler({}), _NoRedirect(),
            request.HTTPSHandler(context=ssl.create_default_context()))

    def __call__(self, method, path, payload, token):
        if method not in ("POST", "DELETE") or not path.startswith("/v1/"):
            raise ValueError("Unexpected analytics request")
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if data is not None and len(data) > MAX_BODY:
            raise ValueError("Analytics batch is too large")
        headers = {"User-Agent": "MayhemCompanion-Usage/1", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token is not None:
            if not isinstance(token, str) or not _TOKEN.fullmatch(token):
                raise ValueError("Invalid installation credential")
            headers["Authorization"] = "Bearer " + token
        req = request.Request(self.endpoint + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=5) as response:
                # Responses contain no data needed by the client; do not read an
                # unbounded body or follow a destination supplied by the server.
                return response.status
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            return code


def _timestamp(value):
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        raise ValueError("Invalid analytics timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:00Z").replace(tzinfo=timezone.utc).timestamp()


def _valid_event(event):
    if not isinstance(event, dict) or set(event) != EVENT_KEYS:
        return False
    if (not _uuid(event["event_id"]) or not _uuid(event["session_id"])
            or event["name"] not in EVENT_NAMES or event["mode"] not in MODES
            or not isinstance(event["app_version"], str) or not _VERSION.fullmatch(event["app_version"])
            or type(event["duration_minutes"]) is not int or not 0 <= event["duration_minutes"] <= 1440):
        return False
    try:
        _timestamp(event["occurred_at"])
    except ValueError:
        return False
    return True


class Analytics:
    """Usage sharing defaults on for fresh/legacy-unset installations.

    Public mutators never perform HTTP. The two consent/data actions perform a
    small atomic local write so a crash cannot restore an obsolete preference.
    Initial registration and default setup happen on the worker, only after a
    valid endpoint and durable enabled state. Explicit opt-outs always prevail.
    ``close`` persists the final session but never waits for a network request.
    Underscored arguments are deterministic test seams, not release settings.
    """

    def __init__(self, storage: Path, endpoint: str, version: str, *, _transport=None,
                 _clock=time.monotonic, _wall_clock=time.time, _flush_interval=300.0,
                 _heartbeat_interval=900.0, _retry_base=30.0, _jitter=None):
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError("Analytics requires a semantic app version")
        self.path = Path(storage) / "analytics.json"
        self._revoked_path = Path(storage) / "analytics.revoked"
        self._delete_path = Path(storage) / "analytics.delete-pending"
        self.endpoint = normalize_endpoint(endpoint)
        self.version = version
        self._clock, self._wall_clock = _clock, _wall_clock
        self._flush_interval, self._heartbeat_interval = _flush_interval, _heartbeat_interval
        self._retry_base = _retry_base
        self._jitter = _jitter or (lambda: secrets.randbelow(31))
        self._injected_transport = _transport
        self._transport = None
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False
        self._epoch = 0
        self._dirty = False
        self._write_failed = False
        self._load_failed = False
        self._default_enable_pending = False
        self._last_error = ""
        self._attempts = 0
        self._next_due = self._clock()
        self._visible, self._mode = False, "unknown"
        self._session = None
        self._session_start = self._heartbeat_due = 0.0
        self._state = self._empty_state()
        self._load()
        # A failed atomic replacement must not resurrect the older opt-in on
        # restart. A marker contains no identity or secret; its existence revokes
        # consent even if its write was interrupted or its contents are invalid.
        if self._marker_present(self._revoked_path) or self._marker_present(self._delete_path):
            pending = self._state["deletion_pending"] or (
                self._marker_present(self._delete_path) and self._state["installation_id"] is not None)
            if self._state["consent"] != "disabled" or self._state["events"] or pending != self._state["deletion_pending"]:
                self._state.update(consent="disabled", events=[], deletion_pending=pending)
                self._dirty = True
        if self._dirty:
            self._save_locked()
        self._default_enable_pending = self._can_default_enable_locked()
        if (self._default_enable_pending or self._state["deletion_pending"]
                or (self._state["consent"] == "enabled" and self.endpoint)):
            self._start_locked()

    def _empty_state(self):
        return {"schema": 1, "endpoint": self.endpoint, "consent": "unset",
                "installation_id": None, "token": None, "registered": False,
                "deletion_pending": False, "blocked": False, "events": []}

    def _can_default_enable_locked(self):
        return bool(self.endpoint and self._state["consent"] == "unset"
                    and not self._load_failed and not self._write_failed
                    and not self._state["blocked"] and not self._state["deletion_pending"]
                    and not self._marker_present(self._revoked_path)
                    and not self._marker_present(self._delete_path))

    @staticmethod
    def _marker_present(path):
        try:
            path.stat()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return True

    def _mark_revoked_locked(self, *, delete=False):
        path = self._delete_path if delete else self._revoked_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="ascii") as stream:
                stream.write("1\n")
                stream.flush()
                os.fsync(stream.fileno())
            return True
        except OSError:
            return False

    def _persist_revocation_fallback_locked(self):
        # Never remove the state file: absence is a fresh/default-on install.
        # An in-place write can still work when creating a marker or replacing
        # a directory entry fails. A partial/truncated file also fails closed on
        # the next start. Preserve deletion credentials whenever the write works.
        try:
            with open(self.path, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._state, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            self._dirty = self._write_failed = False
            self._last_error = ""
            self._next_due = self._clock()
        except OSError:
            self._last_error = "Could not save the choice. Sharing is paused for this run; retry before restarting."

    def _load(self):
        try:
            try:
                state_size = self.path.stat().st_size
            except FileNotFoundError:
                return
            if state_size > MAX_STATE:
                raise ValueError("Oversized analytics state")
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or set(state) != STATE_KEYS or state["schema"] != 1
                    or state["consent"] not in ("unset", "enabled", "disabled")
                    or any(type(state[key]) is not bool for key in ("registered", "deletion_pending", "blocked"))
                    or not isinstance(state["events"], list) or len(state["events"]) > MAX_EVENTS
                    or not all(_valid_event(event) for event in state["events"])):
                raise ValueError("Invalid analytics state")
            has_identity = state["installation_id"] is not None or state["token"] is not None
            if has_identity and (not _uuid(state["installation_id"]) or not isinstance(state["token"], str)
                                 or not _TOKEN.fullmatch(state["token"])):
                raise ValueError("Invalid analytics identity")
            if state["endpoint"] != normalize_endpoint(state["endpoint"]):
                raise ValueError("Invalid stored destination")
            if (has_identity and not state["endpoint"]) or (not has_identity and
                    (state["registered"] or state["deletion_pending"] or state["events"] or state["consent"] == "enabled")):
                raise ValueError("Inconsistent analytics state")
            if state["consent"] == "unset" and has_identity:
                raise ValueError("Identity without consent")
            self._state = state
            if state["endpoint"] != self.endpoint and has_identity:
                self._state["consent"] = "disabled"
                self._state["blocked"] = True
                self._last_error = "Analytics destination changed. Delete/reset the old analytics ID before enabling."
                self._dirty = True
            if state["consent"] != "enabled" or state["deletion_pending"] or state["blocked"]:
                if state["deletion_pending"] or state["blocked"]:
                    self._state["consent"] = "disabled"
                if self._state["events"]:
                    self._dirty = True
                    self._state["events"] = []
            self._prune_locked()
        except (OSError, ValueError, TypeError, KeyError):
            self._state = self._empty_state()
            self._load_failed = True
            self._last_error = "Stored analytics settings could not be read. Analytics remains off."

    def _save_locked(self):
        was_failed = self._write_failed
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                json.dump(self._state, stream, separators=(",", ":"))
                stream.flush()
            os.replace(temporary, self.path)
            self._dirty = self._write_failed = False
            if was_failed:
                self._last_error = ""
            return True
        except OSError:
            self._write_failed = True
            self._last_error = "Analytics settings could not be saved. Sharing is paused."
            self._next_due = max(self._next_due, self._clock() + 30)
            return False

    def status(self):
        with self._lock:
            return {"consent": self._state["consent"], "configured": bool(self.endpoint),
                    "installation_id": self._state["installation_id"],
                    "queued": len(self._state["events"]), "deletion_pending": self._state["deletion_pending"],
                    "registered": self._state["registered"],
                    "blocked": self._state["blocked"] or self._write_failed,
                    "last_error": self._last_error}

    def _start_locked(self):
        if self._thread is None and not self._closed:
            self._thread = threading.Thread(target=self._run, name="mayhem-analytics", daemon=True)
            self._thread.start()

    def set_enabled(self, enabled: bool):
        with self._lock:
            if self._closed:
                return
            self._default_enable_pending = False
            if enabled and (not self.endpoint or self._state["deletion_pending"] or self._state["blocked"]):
                self._last_error = ("Analytics is not configured in this build." if not self.endpoint else
                                    "Wait for data deletion to finish before enabling analytics." if self._state["deletion_pending"] else
                                    "Delete/reset the analytics ID before enabling again.")
                return
            if self._state["consent"] == ("enabled" if enabled else "disabled") and not self._write_failed:
                return
            revoked = True if enabled else self._mark_revoked_locked()
            self._epoch += 1
            self._state["consent"] = "enabled" if enabled else "disabled"
            self._state["events"] = []
            self._session = None
            self._last_error = ""
            self._attempts = 0
            self._next_due = self._clock()
            if enabled and self._state["installation_id"] is None:
                self._state.update(endpoint=self.endpoint, installation_id=str(uuid.uuid4()),
                                   token=secrets.token_urlsafe(32), registered=False)
                self._transport = None
            self._dirty = True
            if not self._save_locked():
                # No registration or events may proceed with undurable settings.
                self._state["consent"] = "disabled"
                if not revoked:
                    self._persist_revocation_fallback_locked()
                self._start_locked()
                self._wake.set()
                return
            if enabled:
                try:
                    self._revoked_path.unlink(missing_ok=True)
                    self._delete_path.unlink(missing_ok=True)
                except OSError:
                    self._state["consent"] = "disabled"
                    self._dirty = True
                    self._save_locked()
                    self._last_error = "Could not clear the previous privacy choice. Sharing remains off."
                    return
                if self._visible:
                    self._begin_session_locked()
                self._start_locked()
            self._wake.set()

    def set_usage(self, visible: bool, mode: str):
        with self._lock:
            if self._closed:
                return
            old_visible, old_mode = self._visible, self._mode
            self._visible = bool(visible)
            self._mode = mode if isinstance(mode, str) and mode in MODES else "unknown"
            if old_visible == self._visible and old_mode == self._mode:
                return
            if self._state["consent"] != "enabled" or self._state["blocked"] or self._write_failed:
                return
            if self._visible and self._session is None:
                self._begin_session_locked()
            elif not self._visible and self._session is not None:
                self._event_locked("session_end")
                self._session = None
            elif self._session is not None and old_mode != self._mode:
                self._event_locked("mode_used")
            self._wake.set()

    def _begin_session_locked(self):
        self._session = str(uuid.uuid4())
        self._session_start = self._clock()
        self._heartbeat_due = self._session_start + self._heartbeat_interval
        self._event_locked("session_start")

    def _event_locked(self, name):
        now = self._wall_clock()
        event = {"event_id": str(uuid.uuid4()), "session_id": self._session, "name": name,
                 "occurred_at": datetime.fromtimestamp(now, timezone.utc).replace(second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:00Z"),
                 "app_version": self.version, "mode": self._mode,
                 "duration_minutes": max(0, min(1440, math.floor((self._clock() - self._session_start) / 60)))}
        self._state["events"].append(event)
        self._prune_locked()
        self._dirty = True

    def _prune_locked(self):
        now = self._wall_clock()
        previous = len(self._state["events"])
        self._state["events"] = [event for event in self._state["events"]
                                 if now - MAX_AGE <= _timestamp(event["occurred_at"]) <= now + 300][-MAX_EVENTS:]
        if len(self._state["events"]) != previous:
            self._dirty = True

    def delete_data(self):
        with self._lock:
            if self._closed:
                return
            self._default_enable_pending = False
            revoked = self._mark_revoked_locked(delete=True)
            self._epoch += 1
            self._state.update(consent="disabled", events=[])
            self._session = None
            self._state["deletion_pending"] = self._state["installation_id"] is not None
            self._last_error = ""
            self._attempts = 0
            self._next_due = self._clock()
            self._dirty = True
            if not self._state["deletion_pending"]:
                self._state["blocked"] = False
            if not self._save_locked():
                if not revoked:
                    self._persist_revocation_fallback_locked()
                self._start_locked()
                self._wake.set()
                return
            if self._state["deletion_pending"]:
                self._start_locked()
            self._wake.set()

    def _action_locked(self):
        if self._write_failed or self._clock() < self._next_due:
            return None
        state = self._state
        common = (self._epoch, state["installation_id"], state["token"])
        if state["deletion_pending"]:
            return ("delete", *common, None)
        if state["consent"] != "enabled" or not self.endpoint or state["blocked"]:
            return None
        if not state["registered"]:
            return ("register", *common, None)
        if state["events"]:
            return ("events", *common, [dict(event) for event in state["events"][:MAX_BATCH]])
        return None

    def _deliver(self, action):
        kind, epoch, ident, token, events = action
        with self._lock:
            # An action selected before an opt-out/delete must not begin after it.
            if self._closed or epoch != self._epoch or self._write_failed:
                return
            if kind == "delete":
                if not self._state["deletion_pending"]:
                    return
            elif self._state["consent"] != "enabled" or self._state["deletion_pending"]:
                return
            if self._transport is None:
                self._transport = self._injected_transport or HttpTransport(self._state["endpoint"])
            transport = self._transport
        try:
            if kind == "register":
                code = transport("POST", "/v1/installations", {"installation_id": ident, "token": token}, None)
            elif kind == "delete":
                code = transport("DELETE", "/v1/installations/" + ident, None, token)
            else:
                code = transport("POST", "/v1/events", {"installation_id": ident, "events": events}, token)
        except Exception:
            # Never expose tokens, request bodies, URL queries, or transport error
            # strings through logs, UI, feedback diagnostics, or exception traces.
            code = 0
        with self._lock:
            if epoch != self._epoch:
                return
            success = code == 204 if kind == "delete" else code in ((200, 201) if kind == "register" else (200, 202, 204))
            if success:
                self._attempts = 0
                self._last_error = ""
                if kind == "delete":
                    self._state = self._empty_state()
                    self._state["consent"] = "disabled"
                    self._transport = None
                elif kind == "register":
                    self._state["registered"] = True
                else:
                    delivered = {event["event_id"] for event in events}
                    self._state["events"] = [event for event in self._state["events"] if event["event_id"] not in delivered]
                self._next_due = self._clock() + self._flush_interval + self._jitter()
                self._dirty = True
                saved = self._save_locked()
                if kind == "delete" and saved:
                    try:
                        self._delete_path.unlink(missing_ok=True)
                    except OSError:
                        pass  # A stale marker keeps sharing off, never on.
            elif kind != "delete" and code in (401, 403, 404, 409, 410):
                self._state.update(consent="disabled", blocked=True, events=[])
                self._session = None
                self._last_error = "The analytics ID is no longer accepted. Delete/reset it before enabling again."
                self._dirty = True
                self._save_locked()
            else:
                self._attempts += 1
                delay = min(1800, self._retry_base * 2 ** min(self._attempts - 1, 10))
                self._next_due = self._clock() + delay + self._jitter()
                self._last_error = ("Deletion is pending; it will retry when the service is available."
                                    if kind == "delete" else "Usage upload is unavailable; queued data will retry later.")

    def _run(self):
        while True:
            with self._lock:
                if self._closed:
                    return
                if self._default_enable_pending:
                    self._default_enable_pending = False
                    if self._can_default_enable_locked():
                        self.set_enabled(True)
                if self._session is not None and self._clock() >= self._heartbeat_due:
                    self._event_locked("session_heartbeat")
                    self._heartbeat_due = self._clock() + self._heartbeat_interval
                self._prune_locked()
                if self._dirty and (not self._write_failed or self._clock() >= self._next_due):
                    self._save_locked()
                action = self._action_locked()
                pending = self._state["deletion_pending"] or (self._state["consent"] == "enabled"
                          and (not self._state["registered"] or self._state["events"]))
                wait = max(0.05, min(60.0, self._next_due - self._clock())) if pending else 60.0
                if self._session is not None:
                    wait = min(wait, max(0.05, self._heartbeat_due - self._clock()))
                self._wake.clear()
            if action is not None:
                self._deliver(action)
            else:
                self._wake.wait(wait)

    def close(self):
        with self._lock:
            if self._closed:
                return
            if self._session is not None and self._state["consent"] == "enabled" and not self._write_failed:
                self._event_locked("session_end")
                self._session = None
            self._closed = True
            self._epoch += 1
            if self._dirty:
                self._save_locked()
            self._wake.set()
        # A network request is bounded by its socket timeout and daemon lifetime;
        # UI teardown never waits for an unavailable server.
