import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";
import { handle, cleanup, digest, MAX_BODY_BYTES } from "../src/service.mjs";

const NOW = Date.parse("2026-10-29T06:00:00Z");
const DAY = 86400000;
const ID = "26bc895b-3e75-4082-aa39-8ba12056acb7";
const OTHER_ID = "50d83cc4-397b-4790-a1c8-e9202d8d25f1";
const TOKEN = "a".repeat(43);
const OTHER_TOKEN = "b".repeat(43);
const EVENT_ID = "e6ac6980-a4cb-4277-9d2a-105dc1f8d78c";
const SESSION_ID = "498a4427-45ce-4ff6-8613-96d66a5f8b31";

// Runs the actual migration and Worker SQL against SQLite, including foreign
// keys and transaction rollback, rather than mocks that accept any SQL string.
class SQLiteD1 {
  constructor() {
    this.sqlite = new DatabaseSync(":memory:");
    this.sqlite.exec("PRAGMA foreign_keys = ON");
    this.sqlite.exec(readFileSync(new URL("../migrations/0001_usage.sql", import.meta.url), "utf8"));
  }
  prepare(sql) {
    return { sql, args: [], bind(...args) { this.args = args; return this; } };
  }
  async batch(statements) {
    this.sqlite.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map(({ sql, args }) => ({
        results: this.sqlite.prepare(sql).all(...args), success: true,
        meta: { changes: this.sqlite.prepare("SELECT changes() AS count").get().count },
      }));
      this.sqlite.exec("COMMIT");
      return results;
    } catch (error) {
      this.sqlite.exec("ROLLBACK");
      throw error;
    }
  }
  rows(table) { return this.sqlite.prepare(`SELECT * FROM ${table}`).all(); }
}

function limiter() {
  return { keys: [], blocked: false, async limit({ key }) { this.keys.push(key); return { success: !this.blocked }; } };
}

function environment(t) {
  const env = { DB: new SQLiteD1(), REGISTER_LIMITER: limiter(), REQUEST_LIMITER: limiter(), INSTALLATION_LIMITER: limiter() };
  t.after(() => env.DB.sqlite.close());
  return env;
}

function request(path, body, { token, method = "POST", headers = {} } = {}) {
  return new Request("https://analytics.example" + path, {
    method,
    headers: { "Content-Type": "application/json", "CF-Connecting-IP": "192.0.2.50",
      ...(token ? { Authorization: "Bearer " + token } : {}), ...headers },
    ...(body !== undefined ? { body: typeof body === "string" ? body : JSON.stringify(body) } : {}),
  });
}

function event(overrides = {}) {
  return { event_id: EVENT_ID, session_id: SESSION_ID, name: "session_start",
    occurred_at: "2026-10-29T05:58:00Z", app_version: "0.6.0", mode: "mayhem", duration_minutes: 0, ...overrides };
}

async function register(env, id = ID, token = TOKEN, now = NOW) {
  return handle(request("/v1/installations", { installation_id: id, token }), env, now);
}

async function ingest(env, events = [event()], token = TOKEN, now = NOW) {
  return handle(request("/v1/events", { installation_id: ID, events }, { token }), env, now);
}

async function remove(env, id = ID, token = TOKEN, now = NOW) {
  return handle(request("/v1/installations/" + id, undefined, { token, method: "DELETE" }), env, now);
}

test("register stores only a token digest and is safely idempotent", async t => {
  const env = environment(t);
  assert.equal((await register(env)).status, 201);
  assert.equal((await register(env)).status, 200);
  assert.equal((await register(env, ID, OTHER_TOKEN)).status, 409);
  const rows = env.DB.rows("installations");
  assert.equal(rows.length, 1);
  assert.equal(rows[0].token_hash, await digest(TOKEN));
  assert.ok(!JSON.stringify(rows).includes(TOKEN));
});

test("events require matching credentials and remain idempotent across retries", async t => {
  const env = environment(t);
  assert.equal((await ingest(env)).status, 401);
  await register(env);
  assert.equal((await ingest(env, [event()], OTHER_TOKEN)).status, 401);
  assert.equal((await ingest(env)).status, 200);
  assert.equal((await ingest(env)).status, 200);
  assert.equal(env.DB.rows("usage_events").length, 1);
  assert.equal(env.DB.rows("usage_events")[0].occurred_at, "2026-10-29T05:58:00.000Z");
});

test("strict schema rejects identity fields and fails an entire mixed batch", async t => {
  const env = environment(t);
  await register(env);
  const response = await ingest(env, [event(), event({ event_id: OTHER_ID, riot_id: "private-name" })]);
  assert.equal(response.status, 400);
  assert.equal(env.DB.rows("usage_events").length, 0);
  assert.equal((await handle(request("/v1/installations", { installation_id: OTHER_ID, token: TOKEN, email: "private" }), env, NOW)).status, 400);
  assert.equal(env.DB.rows("installations").length, 1);
});

test("rejects malformed versions, modes, IDs, duration and time without writes", async t => {
  const env = environment(t);
  await register(env);
  for (const change of [
    { event_id: "not-a-uuid" }, { event_id: [EVENT_ID] }, { session_id: null }, { name: "keystroke" },
    { mode: "summoners-rift" }, { app_version: "user supplied metadata" }, { app_version: ["0.6.0"] },
    { duration_minutes: 1.5 }, { duration_minutes: true }, { duration_minutes: -1 }, { duration_minutes: 1441 },
    { occurred_at: "2026-02-30T05:58:00Z" }, { occurred_at: "2026-10-29T05:58:31Z" },
    { occurred_at: new Date(NOW + 6 * 60000).toISOString() },
    { occurred_at: new Date(NOW - 91 * DAY).toISOString() },
  ]) assert.equal((await ingest(env, [event(change)])).status, 400, JSON.stringify(change));
  assert.equal(env.DB.rows("usage_events").length, 0);
});

test("rejects oversized bodies even when Content-Length is absent or dishonest", async t => {
  const env = environment(t);
  const large = JSON.stringify({ installation_id: ID, token: "a".repeat(MAX_BODY_BYTES) });
  for (const headers of [{}, { "Content-Length": "3" }, { "Content-Length": String(MAX_BODY_BYTES + 1) }]) {
    assert.equal((await handle(request("/v1/installations", large, { headers }), env, NOW)).status, 413);
  }
  assert.equal(env.DB.rows("installations").length, 0);
});

test("rejects invalid JSON, media types, empty batches and duplicate IDs", async t => {
  const env = environment(t);
  await register(env);
  assert.equal((await handle(request("/v1/installations", "{"), env, NOW)).status, 400);
  assert.equal((await handle(request("/v1/installations", "{}", { headers: { "Content-Type": "text/plain" } }), env, NOW)).status, 415);
  assert.equal((await ingest(env, [])).status, 400);
  assert.equal((await ingest(env, [event(), event()])).status, 400);
  const tooMany = Array.from({ length: 21 }, () => event({ event_id: crypto.randomUUID() }));
  assert.equal((await ingest(env, tooMany)).status, 400);
});

test("deletion rejects a wrong secret and atomically deletes identity and events", async t => {
  const env = environment(t);
  await register(env);
  await ingest(env);
  assert.equal((await remove(env, ID, OTHER_TOKEN)).status, 401);
  assert.equal(env.DB.rows("deletion_tombstones").length, 0);
  assert.equal(env.DB.rows("usage_events").length, 1);
  assert.equal((await remove(env)).status, 204);
  assert.equal(env.DB.rows("usage_events").length, 0);
  assert.equal(env.DB.rows("installations").length, 0);
  const marker = env.DB.rows("deletion_tombstones")[0];
  assert.deepEqual(Object.keys(marker), ["installation_hash", "expires_at"]);
  assert.equal(marker.installation_hash, await digest(ID));
  assert.ok(!JSON.stringify(marker).includes(ID));
  assert.ok(!JSON.stringify(marker).includes(await digest(TOKEN)));
  assert.equal((await remove(env)).status, 204);
});

test("deletion before a delayed first registration suppresses recreation", async t => {
  const env = environment(t);
  assert.equal((await remove(env)).status, 204);
  assert.equal((await register(env)).status, 410);
  assert.equal((await ingest(env)).status, 401);
  assert.equal(env.DB.rows("installations").length, 0);
  assert.equal(env.DB.rows("usage_events").length, 0);
  // A new ID is unaffected by deletion of the previous installation.
  assert.equal((await register(env, OTHER_ID)).status, 201);
});

test("90-day cleanup removes old events/identities and expired deletion markers", async t => {
  const env = environment(t);
  await register(env);
  await ingest(env);
  await remove(env, OTHER_ID);
  await cleanup(env, NOW + DAY + 60000);
  assert.equal(env.DB.rows("deletion_tombstones").length, 0);
  assert.equal(env.DB.rows("usage_events").length, 1);
  await cleanup(env, NOW + 91 * DAY);
  assert.equal(env.DB.rows("usage_events").length, 0);
  assert.equal(env.DB.rows("installations").length, 0);
});

test("database failures roll back an entire batch without exposing internals", async t => {
  const env = environment(t);
  await register(env);
  env.DB.sqlite.exec(`CREATE TRIGGER force_failure BEFORE INSERT ON usage_events
    WHEN NEW.name = 'mode_used' BEGIN SELECT RAISE(ABORT, 'private-token-and-SQL'); END`);
  const response = await ingest(env, [event(), event({ event_id: OTHER_ID, name: "mode_used" })]);
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "temporarily_unavailable" });
  assert.equal(env.DB.rows("usage_events").length, 0);
});

test("rate limits fail closed and IP addresses do not enter D1 or limiter keys", async t => {
  const env = environment(t);
  env.REGISTER_LIMITER.blocked = true;
  const blocked = await register(env);
  assert.equal(blocked.status, 429);
  assert.equal(blocked.headers.get("Retry-After"), "60");
  assert.equal(env.DB.rows("installations").length, 0);
  assert.match(env.REQUEST_LIMITER.keys[0], /^[0-9a-f]{64}$/);
  assert.ok(!JSON.stringify(env.REGISTER_LIMITER.keys).includes("192.0.2.50"));
  delete env.REGISTER_LIMITER;
  assert.equal((await register(env)).status, 503);
});

test("browser origins and public reads are refused; health has no database data", async t => {
  const env = environment(t);
  const browser = request("/v1/installations", { installation_id: ID, token: TOKEN }, { headers: { Origin: "https://example.com" } });
  const response = await handle(browser, env, NOW);
  assert.equal(response.status, 403);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
  assert.equal((await handle(request("/v1/events", undefined, { method: "GET" }), env, NOW)).status, 404);
  const health = await handle(new Request("https://analytics.example/health"), {}, NOW);
  assert.deepEqual(await health.json(), { service: "mayhem-usage", protocol: 1 });
});
