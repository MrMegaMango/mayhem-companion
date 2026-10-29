-- Run through your private D1 dashboard or Wrangler. No public analytics API.
-- All counts refer to opted-in installations, not unique human players.

-- Daily active installations, using event occurrence rather than upload date.
SELECT date(occurred_at) AS day, COUNT(DISTINCT installation_id) AS active_installations
FROM usage_events
GROUP BY date(occurred_at)
ORDER BY day DESC;

-- Active installations during the last 7 UTC dates including today.
SELECT COUNT(DISTINCT installation_id) AS weekly_active_installations
FROM usage_events
WHERE occurred_at >= strftime('%Y-%m-%dT00:00:00.000Z', 'now', '-6 days');

-- Day-7 retention of first-use cohorts still inside the 90-day event window.
-- Exclude installations created before the retained window to avoid treating
-- their earliest surviving event as a first-ever visit. Pending/never-used
-- registrations do not enter the denominator.
WITH first_use AS (
  SELECT e.installation_id, MIN(date(e.occurred_at)) AS first_day
  FROM usage_events e JOIN installations i USING (installation_id)
  WHERE i.created_at >= strftime('%Y-%m-%dT00:00:00.000Z', 'now', '-89 days')
  GROUP BY e.installation_id
), cohorts AS (
  SELECT f.installation_id, f.first_day,
    EXISTS(SELECT 1 FROM usage_events e WHERE e.installation_id = f.installation_id
      AND date(e.occurred_at) = date(f.first_day, '+7 days')) AS returned_day_7
  FROM first_use f
  WHERE f.first_day < date('now', '-7 days')
)
SELECT first_day, COUNT(*) AS cohort_size, SUM(returned_day_7) AS returned_day_7,
       ROUND(100.0 * SUM(returned_day_7) / COUNT(*), 1) AS day_7_retention_percent
FROM cohorts GROUP BY first_day ORDER BY first_day DESC;

-- Heartbeats and session_end report cumulative minutes. Summing each event
-- would overcount; use the maximum reported duration for each session.
SELECT COUNT(*) AS sessions, ROUND(AVG(minutes), 1) AS average_visible_minutes,
       SUM(minutes) AS visible_minutes
FROM (
  SELECT installation_id, session_id, MAX(duration_minutes) AS minutes
  FROM usage_events GROUP BY installation_id, session_id
);

-- Mode adoption; one installation can use more than one mode.
SELECT mode, COUNT(DISTINCT installation_id) AS installations
FROM usage_events
WHERE occurred_at >= strftime('%Y-%m-%dT00:00:00.000Z', 'now', '-6 days')
GROUP BY mode ORDER BY installations DESC;
