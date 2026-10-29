// Real Worker/D1 smoke test using synthetic data; never use a player's ID.
import assert from "node:assert/strict";
import { randomBytes, randomUUID } from "node:crypto";

const endpoint = new URL(process.argv[2] || "http://127.0.0.1:8795");
if (endpoint.protocol !== "https:" && !(endpoint.protocol === "http:" && endpoint.hostname === "127.0.0.1")) {
  throw new Error("Use an HTTPS Worker origin or the explicit loopback development server.");
}
if (endpoint.username || endpoint.password || endpoint.search || endpoint.hash || endpoint.pathname !== "/") {
  throw new Error("Supply an origin without credentials, a path, query, or fragment.");
}
const installation_id = randomUUID();
const token = randomBytes(32).toString("base64url");
const registration = { installation_id, token };
const event = {
  event_id: randomUUID(), session_id: randomUUID(), name: "session_start",
  occurred_at: new Date(Math.floor(Date.now() / 60000) * 60000).toISOString(),
  app_version: "0.6.0-smoke", mode: "unknown", duration_minutes: 0,
};

async function send(path, method, body, secret) {
  return fetch(new URL(path, endpoint), {
    method, signal: AbortSignal.timeout(10000),
    headers: { ...(body ? { "Content-Type": "application/json" } : {}),
      ...(secret ? { Authorization: "Bearer " + secret } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}

let attemptedRegistration = false;
try {
  const health = await send("/health", "GET");
  assert.equal(health.status, 200, "Health status");
  assert.equal((await health.json()).protocol, 1, "Protocol version");
  attemptedRegistration = true;
  assert.equal((await send("/v1/installations", "POST", registration)).status, 201, "New registration");
  assert.equal((await send("/v1/installations", "POST", registration)).status, 200, "Registration retry");
  assert.equal((await send("/v1/events", "POST", { installation_id, events: [event] }, "x".repeat(43))).status, 401, "Wrong token rejected");
  for (let attempt = 0; attempt < 2; attempt++) {
    const result = await send("/v1/events", "POST", { installation_id, events: [event] }, token);
    assert.equal(result.status, 200, "Event accepted / retry acknowledged");
    assert.equal((await result.json()).accepted, 1);
  }
  assert.equal((await send("/v1/installations/" + installation_id, "DELETE", null, token)).status, 204, "Deletion");
  assert.equal((await send("/v1/events", "POST", { installation_id, events: [event] }, token)).status, 401, "No writes after deletion");
  assert.equal((await send("/v1/installations", "POST", registration)).status, 410, "Delayed registration suppressed");
  console.log("Worker/D1 smoke passed: register, retry, authentication, ingest, delete, and replay suppression. Synthetic usage data deleted.");
} finally {
  if (attemptedRegistration) {
    const result = await send("/v1/installations/" + installation_id, "DELETE", null, token);
    assert.equal(result.status, 204, "Synthetic data cleanup");
  }
}
