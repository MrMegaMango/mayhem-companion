# Optional usage analytics service

Cloudflare Worker + D1 backend for opted-in Mayhem Companion installations. The desktop app sends requests to this API; it never receives database credentials. There is no public data-reading or administration endpoint.

## Local verification

Node 22.13+ is required for the native SQLite tests (Node 24 recommended). `npm test` exercises the real migration and SQL with transactions and foreign keys, including authorization, idempotency, deletion races, retention, validation, and rollback. The SQLite test adapter is not a replacement for a deployed D1 smoke test.

```sh
npm install
npm test
npm run migrate:local
npm run dev
```

While the local Worker is running, `npm run test:smoke -- http://127.0.0.1:8795` checks the real runtime (use the port printed by Wrangler, normally 8787 unless configured). The same command accepts an HTTPS deployed origin. It uses synthetic data, verifies authentication and deletion, then deletes its synthetic installation/events. The short deletion-suppression marker expires normally.

The local server listens only on 127.0.0.1. The normal desktop release accepts HTTPS endpoints only; use the app's explicit development transport/test fixture for local end-to-end checks.

## Deploy to your Cloudflare account

1. Run `npx wrangler login`, then `npx wrangler d1 create mayhem-usage`.
2. Replace the placeholder `database_id` in `wrangler.jsonc` with the returned ID. Choose unused rate-limit namespace IDs if this account uses the sample IDs elsewhere.
3. Run `npm run migrate:remote`, then `npm run deploy`.
4. Set the desktop build's analytics endpoint to the deployed HTTPS origin. Do not put account tokens, D1 credentials, or a management secret in the desktop package.
5. Use fresh synthetic installation credentials to check registration, ingestion, idempotency and deletion before distributing a configured build. The response to deleting that test ID must be 204 and attempts to register the deleted ID must return 410 for 24 hours.

The project service is deployed at `https://mayhem-usage.mayhem-companion-analytics.workers.dev`. The D1 ID in `wrangler.jsonc` identifies the project database and is not a credential. Forks must create their own database, replace this ID and configure their own endpoint; copying the ID does not grant access. The app remains opt-in regardless of endpoint configuration.

## Protocol

- `POST /v1/installations`: exactly `installation_id` (lowercase UUIDv4) and `token` (43-character base64url secret representing 32 random bytes). The database stores SHA-256 of the secret. Same credentials are idempotent (200), a new identity returns 201, a conflicting secret returns 409, and a recently deleted ID returns 410.
- `POST /v1/events`: `Authorization: Bearer <token>` and exactly `installation_id`, `events`. Each event has `event_id`, `session_id` (UUIDv4), `name`, `occurred_at`, `app_version`, `mode`, `duration_minutes`. Maximum 20 events and 24 KiB per request. Times must be UTC with `Z`, rounded to a minute, no older than 90 days and no more than five minutes ahead. Batch retries acknowledge already stored event IDs without double counting.
- `DELETE /v1/installations/<id>`: same bearer secret. Deletes the installation and all its events atomically. A wrong secret for an existing installation returns 401; a missing installation returns 204. It also prevents a delayed registration from recreating the ID.
- `GET /health`: identifies the service and protocol only; it does not verify D1 connectivity.

Events are `session_start`, `session_heartbeat`, `session_end`, `mode_used`; modes are `aram`, `mayhem`, `arena`, `tft`, `unknown`. Cumulative duration is an integer number of minutes, 0–1440. Example owner-only queries are in `queries.sql`.

## Data and limits

This is pseudonymous analytics, not anonymous analytics. D1 stores an installation ID, secret digest, app version, session/event IDs, mode, minute-rounded event times, cumulative visible minutes and receipt times. The service does not store Riot IDs, champions, items, screenshots, match histories, hostnames, IP addresses or user-agent headers in D1.

Raw events and inactive installation records expire after 90 days; an hourly task removes expired rows. Deletion leaves only SHA-256 of the installation ID plus an expiry timestamp for 24 hours, removed by the next hourly cleanup (normally within 25 hours of the latest deletion request). This short suppression marker prevents delayed registration/ingestion from restoring deleted data. It contains no raw ID, secret digest or event contents. Cloudflare D1 backup/Time Travel retention may temporarily retain prior database states; deletion is from the active database. Do not restore an older analytics snapshot into production, since it could restore previously deleted records; recover with a fresh database instead.

Worker request logging is disabled in the configuration, and code never logs request URLs, payloads, headers or exceptions. Cloudflare still processes network metadata such as IP addresses as the hosting provider. A daily rotating IP digest is used only by the short-window edge limiter, not written to D1. Existing-installation write limits additionally use an installation-ID digest. Shared networks can encounter aggregate IP limits; clients must back off on 429/503. Cloudflare's limit binding is approximate per edge location, not a strict global abuse or billing cap. Public registration can be abused, so treat install counts as estimates and monitor provider quotas.

Native clients do not need CORS; requests with `Origin` are rejected, and no permissive CORS headers are returned. Rate-limit bindings are required and fail closed if missing. There is no shared client API key whose secrecy is relied on for authorization.

References: [D1 batch API](https://developers.cloudflare.com/d1/worker-api/d1-database/), [D1 foreign keys](https://developers.cloudflare.com/d1/sql-api/foreign-keys/), [Workers rate-limit binding](https://developers.cloudflare.com/workers/runtime-apis/bindings/rate-limit/).
