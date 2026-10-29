// Purpose-built collection API. No request payloads, headers, or URLs are logged.
export const MAX_BODY_BYTES = 24 * 1024;
export const MAX_EVENTS = 20;
export const RETENTION_DAYS = 90;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const TOKEN = /^[A-Za-z0-9_-]{43}$/;
const VERSION = /^\d{1,3}\.\d{1,3}\.\d{1,3}(?:-[0-9A-Za-z.-]{1,30})?$/;
const EVENT_NAMES = new Set(["session_start", "session_heartbeat", "session_end", "mode_used"]);
const MODES = new Set(["aram", "mayhem", "arena", "tft", "unknown"]);
const EVENT_KEYS = ["event_id", "session_id", "name", "occurred_at", "app_version", "mode", "duration_minutes"];
const DAY = 86400000;

class RequestError extends Error {
  constructor(status, code) { super(code); this.status = status; }
}

function reply(status, payload, extra = {}) {
  const headers = {
    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer", ...extra,
  };
  if (status === 204) return new Response(null, { status, headers });
  headers["Content-Type"] = "application/json; charset=utf-8";
  return new Response(JSON.stringify(payload), { status, headers });
}

function exactKeys(value, keys) {
  return value && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

function matches(value, pattern) { return typeof value === "string" && pattern.test(value); }

export async function digest(value) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}

async function readJSON(request) {
  if (request.headers.get("Content-Type")?.split(";", 1)[0].trim().toLowerCase() !== "application/json") {
    throw new RequestError(415, "json_required");
  }
  if (request.headers.has("Content-Encoding") && request.headers.get("Content-Encoding") !== "identity") {
    throw new RequestError(415, "content_encoding_not_supported");
  }
  const length = request.headers.get("Content-Length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_BODY_BYTES)) {
    throw new RequestError(413, "body_too_large");
  }
  if (!request.body) throw new RequestError(400, "invalid_json");
  const reader = request.body.getReader();
  const parts = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY_BYTES) {
        await reader.cancel();
        throw new RequestError(413, "body_too_large");
      }
      parts.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) { bytes.set(part, offset); offset += part.byteLength; }
  try { return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
  catch { throw new RequestError(400, "invalid_json"); }
}

function bearer(request) {
  const value = request.headers.get("Authorization") || "";
  if (!value.startsWith("Bearer ") || !TOKEN.test(value.slice(7))) {
    throw new RequestError(401, "invalid_credentials");
  }
  return value.slice(7);
}

function validateEvents(body, now) {
  if (!exactKeys(body, ["installation_id", "events"]) || !matches(body.installation_id, UUID) ||
      !Array.isArray(body.events) || body.events.length < 1 || body.events.length > MAX_EVENTS) {
    throw new RequestError(400, "invalid_batch");
  }
  const seen = new Set();
  return body.events.map(event => {
    if (!exactKeys(event, EVENT_KEYS) || !matches(event.event_id, UUID) || !matches(event.session_id, UUID) ||
        !EVENT_NAMES.has(event.name) || !MODES.has(event.mode) || !matches(event.app_version, VERSION) ||
        !Number.isInteger(event.duration_minutes) || event.duration_minutes < 0 || event.duration_minutes > 1440 ||
        typeof event.occurred_at !== "string" ||
        !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:\.000)?Z$/.test(event.occurred_at) || seen.has(event.event_id)) {
      throw new RequestError(400, "invalid_event");
    }
    const timestamp = Date.parse(event.occurred_at);
    const canonical = event.occurred_at.replace(/:00Z$/, ":00.000Z");
    if (!Number.isFinite(timestamp) || new Date(timestamp).toISOString() !== canonical ||
        timestamp < now - RETENTION_DAYS * DAY || timestamp > now + 5 * 60000) {
      throw new RequestError(400, "invalid_event_time");
    }
    seen.add(event.event_id);
    return { ...event, occurred_at: canonical };
  });
}

async function enforceLimit(binding, key) {
  // Fail closed rather than silently turning into an unrestricted write API.
  if (!binding || typeof binding.limit !== "function") throw new RequestError(503, "not_configured");
  const { success } = await binding.limit({ key });
  if (!success) throw new RequestError(429, "rate_limited");
}

async function register(request, env, now) {
  const body = await readJSON(request);
  if (!exactKeys(body, ["installation_id", "token"]) || !matches(body.installation_id, UUID) || !matches(body.token, TOKEN)) {
    throw new RequestError(400, "invalid_installation");
  }
  const hash = await digest(body.token);
  const idHash = await digest(body.installation_id);
  const timestamp = new Date(now).toISOString();
  const results = await env.DB.batch([
    env.DB.prepare("SELECT 1 AS suppressed FROM deletion_tombstones WHERE installation_hash = ? AND expires_at > ?")
      .bind(idHash, timestamp),
    env.DB.prepare(`INSERT INTO installations (installation_id, token_hash, created_at, last_seen_at)
      SELECT ?, ?, ?, ? WHERE NOT EXISTS
        (SELECT 1 FROM deletion_tombstones WHERE installation_hash = ? AND expires_at > ?)
      ON CONFLICT(installation_id) DO NOTHING`).bind(body.installation_id, hash, timestamp, timestamp, idHash, timestamp),
    env.DB.prepare("SELECT token_hash FROM installations WHERE installation_id = ?").bind(body.installation_id),
  ]);
  if (results[0].results.length) throw new RequestError(410, "installation_deleted");
  if (results[2].results[0]?.token_hash !== hash) throw new RequestError(409, "installation_conflict");
  return reply(results[1].meta.changes ? 201 : 200, { registered: true });
}

async function ingest(request, env, now) {
  const token = bearer(request);
  const body = await readJSON(request);
  const events = validateEvents(body, now);
  const hash = await digest(token);
  const idHash = await digest(body.installation_id);
  await enforceLimit(env.INSTALLATION_LIMITER, "events:" + idHash);
  const received = new Date(now).toISOString();
  const auth = env.DB.prepare(`SELECT 1 AS authorized FROM installations WHERE installation_id = ? AND token_hash = ?
    AND NOT EXISTS (SELECT 1 FROM deletion_tombstones WHERE installation_hash = ? AND expires_at > ?)`)
    .bind(body.installation_id, hash, idHash, received);
  const statements = events.map(event => env.DB.prepare(`
    INSERT INTO usage_events
      (installation_id, event_id, session_id, name, occurred_at, received_at, app_version, mode, duration_minutes)
    SELECT installation_id, ?, ?, ?, ?, ?, ?, ?, ? FROM installations
    WHERE installation_id = ? AND token_hash = ?
      AND NOT EXISTS (SELECT 1 FROM deletion_tombstones WHERE installation_hash = ? AND expires_at > ?)
    ON CONFLICT(installation_id, event_id) DO NOTHING
  `).bind(event.event_id, event.session_id, event.name, event.occurred_at, received, event.app_version,
          event.mode, event.duration_minutes, body.installation_id, hash, idHash, received));
  // D1 batch executes atomically. Auth and inserts honor deletion suppression:
  // a deletion that wins the race cannot be followed by resurrected events.
  const results = await env.DB.batch([auth, ...statements,
    env.DB.prepare(`UPDATE installations SET last_seen_at = ? WHERE installation_id = ? AND token_hash = ?
      AND NOT EXISTS (SELECT 1 FROM deletion_tombstones WHERE installation_hash = ? AND expires_at > ?)`)
      .bind(received, body.installation_id, hash, idHash, received),
  ]);
  if (!results[0].results.length) throw new RequestError(401, "invalid_credentials");
  return reply(200, { accepted: events.length });
}

async function removeInstallation(request, env, id, now) {
  const hash = await digest(bearer(request));
  const idHash = await digest(id);
  await enforceLimit(env.INSTALLATION_LIMITER, "delete:" + idHash);
  const expires = new Date(now + DAY).toISOString();
  const results = await env.DB.batch([
    env.DB.prepare("SELECT token_hash FROM installations WHERE installation_id = ?").bind(id),
    // A not-yet-created ID may have a timed-out registration in flight. A
    // rate-limited deletion also suppresses that eventual registration.
    env.DB.prepare(`INSERT INTO deletion_tombstones (installation_hash, expires_at)
      SELECT ?, ? WHERE NOT EXISTS
        (SELECT 1 FROM installations WHERE installation_id = ? AND token_hash <> ?)
      ON CONFLICT(installation_hash) DO UPDATE SET expires_at = MAX(expires_at, excluded.expires_at)`)
      .bind(idHash, expires, id, hash),
    env.DB.prepare("DELETE FROM installations WHERE installation_id = ? AND token_hash = ?").bind(id, hash),
  ]);
  const previous = results[0].results[0];
  if (previous && previous.token_hash !== hash) throw new RequestError(401, "invalid_credentials");
  return reply(204);
}

export async function cleanup(env, now = Date.now()) {
  const cutoff = new Date(now - RETENTION_DAYS * DAY).toISOString();
  await env.DB.batch([
    env.DB.prepare("DELETE FROM usage_events WHERE occurred_at < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM installations WHERE last_seen_at < ?").bind(cutoff),
    env.DB.prepare("DELETE FROM deletion_tombstones WHERE expires_at <= ?").bind(new Date(now).toISOString()),
  ]);
}

export async function handle(request, env, now = Date.now()) {
  try {
    const url = new URL(request.url);
    if (url.pathname === "/health" && request.method === "GET") return reply(200, { service: "mayhem-usage", protocol: 1 });
    if (url.search || request.headers.has("Origin")) throw new RequestError(403, "desktop_client_only");
    if (!env.DB) throw new RequestError(503, "not_configured");
    const deletion = /^\/v1\/installations\/([0-9a-f-]+)$/.exec(url.pathname);
    const registration = url.pathname === "/v1/installations" && request.method === "POST";
    if (!registration && !(url.pathname === "/v1/events" && request.method === "POST") &&
        !(deletion && UUID.test(deletion[1]) && request.method === "DELETE")) {
      throw new RequestError(404, "not_found");
    }
    // CF supplies this header. Ignore user-controlled forwarding headers.
    // The rotating digest is an ephemeral edge limit key, never stored in D1.
    const ip = request.headers.get("CF-Connecting-IP") || "local-development";
    const rateKey = await digest(`${Math.floor(now / DAY)}:${ip}`);
    await enforceLimit(env.REQUEST_LIMITER, rateKey);
    if (registration) {
      await enforceLimit(env.REGISTER_LIMITER, rateKey);
      return await register(request, env, now);
    }
    if (deletion) return await removeInstallation(request, env, deletion[1], now);
    return await ingest(request, env, now);
  } catch (error) {
    if (error instanceof RequestError) return reply(error.status, { error: error.message },
      error.status === 429 ? { "Retry-After": "60" } : {});
    // Exceptions may contain SQL or credentials. Never reflect or log them.
    return reply(503, { error: "temporarily_unavailable" });
  }
}
