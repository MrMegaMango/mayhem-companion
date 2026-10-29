CREATE TABLE installations (
    installation_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE usage_events (
    installation_id TEXT NOT NULL REFERENCES installations(installation_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK(name IN ('session_start', 'session_heartbeat', 'session_end', 'mode_used')),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    app_version TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('aram', 'mayhem', 'arena', 'tft', 'unknown')),
    duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 0 AND 1440),
    PRIMARY KEY (installation_id, event_id)
);

CREATE INDEX usage_events_time ON usage_events(occurred_at, installation_id);
CREATE INDEX usage_events_session ON usage_events(installation_id, session_id, duration_minutes);
CREATE INDEX installations_seen ON installations(last_seen_at);

-- A deletion marker prevents delayed requests from recreating a deleted ID.
-- It contains no raw installation ID, token hash, or event data.
CREATE TABLE deletion_tombstones (
    installation_hash TEXT PRIMARY KEY,
    expires_at TEXT NOT NULL
);
CREATE INDEX deletion_tombstones_expiry ON deletion_tombstones(expires_at);
