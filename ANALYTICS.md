# Usage data and privacy

Basic usage sharing is enabled by default, with no startup prompt. Every companion feature also works with sharing off. Use **Privacy & usage** in the tray or **Privacy** in the **?** window to turn it off or delete earlier reports. Closing that settings window leaves the current setting unchanged.

New installations and older installations without a saved choice start sharing automatically when the service is configured. Previously saved off choices remain off across updates. Pending deletion, unreadable settings and an invalid service configuration do not silently enable sharing.

## What is sent

Sharing creates a random installation ID and a separate random secret. A new installation registers automatically in the background; usage events begin only when the companion panel is visible. This is an installation count, not a verified count of people; reinstalling/resetting or using multiple computers can count more than once.

Reports contain only:

- Random installation, session and event IDs.
- App version and mode: ARAM, Mayhem, Arena, TFT or unknown.
- Session start, heartbeat, end and mode-use event names.
- UTC event times rounded to a minute and cumulative visible-panel minutes.

The service also stores registration/receipt times and a digest of the random secret used to authorize uploads and deletion. There is no Riot ID, summoner name, champion, item inventory, match history, screenshot, keystroke, hostname or raw log in a report. This is pseudonymous data, since the random ID links reports from one installation.

Only time while the companion panel is visible counts. Leaving its process in the tray does not count as a session. Requests run on a background thread, normally in batches every five minutes with jitter. Heartbeats are recorded every fifteen minutes and across UTC day boundaries. Unsent events are capped at 200 and seven days; retries back off, so offline use does not grow an unlimited queue. Short sessions can have zero rounded minutes. Crashes or offline shutdowns can omit an end event; cumulative heartbeat duration is an estimate rather than precise play time.

## Control and deletion

**Turn off** stops new reports and clears unsent events. A request already in progress may finish. Existing server reports remain until expiry or deletion. **Delete data & reset ID** also turns sharing off, requests deletion of the installation and all events, and keeps retrying while offline. Keep the installed app and its state until it confirms deletion. The credential is retained locally only until the server confirms, then removed; enabling later creates a new ID. If a disk-write error prevents saving your choice, the dialog shows it and asks you to retry.

Data is stored in Cloudflare D1. Raw events and inactive installation records expire after 90 days, removed by hourly cleanup. Deletion removes records from the active database and retains only a SHA-256 hash of the installation ID and expiry time for 24 hours, normally removed within 25 hours of the latest deletion request. This prevents a delayed registration from restoring deleted reports. Cloudflare backups/Time Travel may temporarily retain earlier database states until their provider retention expires.

Cloudflare processes IP addresses and other network metadata to serve requests. The app's database does not store IP addresses or user-agent headers, Worker request logging is disabled, and service code does not log payloads or credentials. An IP digest that changes daily is used transiently for short-window rate limiting. Stats providers still receive their normal data requests independently of usage sharing.

The local sharing preference, queue and installation credentials live in `%USERPROFILE%\MayhemCompanion\analytics.json`, with small revocation/deletion marker files when needed. These files are not distributed or committed. Do not share them. Uninstalling cannot finish a pending server deletion; use the deletion control first if desired.

## Owner reporting and operation

Only the account owner accesses the database through Cloudflare. The desktop app contains the public ingestion URL, never database credentials. There is no public reporting endpoint. [analytics-server/queries.sql](analytics-server/queries.sql) provides daily/weekly active installations, seven-day retention, mode use and visible-session duration. Duration uses each session's maximum cumulative minutes rather than summing heartbeats. Public registration is not proof of a real person, and counts are approximate.

[Backend setup and protocol](analytics-server/README.md) describes deployment, migration, synthetic verification and limits. Keep the service on the intended free plan and monitor its quotas; rate limits are approximate and are not a billing cap. Do not restore old analytics backups into production because that could restore deleted records; recover into a fresh database instead.
