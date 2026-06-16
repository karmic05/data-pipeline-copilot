-- GH Archive (githubarchive public dataset, BigQuery): daily repo-activity rollup.
-- Real event schema: type, actor, repo, payload, created_at.
-- Deliberately riddled with anti-patterns for the copilot to surface.
--
-- ANTI-PATTERN: this scans EVERY daily shard of the `events_*` wildcard table
-- because it never filters on `_TABLE_SUFFIX` (nor a partition / created_at
-- predicate). On the real githubarchive dataset that is many terabytes per run.
CREATE OR REPLACE TABLE `myproj.analytics.repo_activity_daily` AS
SELECT
    *,                                              -- SELECT * on a huge wildcard table
    repo.name              AS repo_name,
    actor.login            AS actor_login,
    COUNT(*)               AS events,
    COUNTIF(type = 'PushEvent')         AS pushes,
    COUNTIF(type = 'PullRequestEvent')  AS pull_requests,
    COUNTIF(type = 'WatchEvent')        AS stars
FROM `githubarchive.day.events_*`
-- No _TABLE_SUFFIX filter and no created_at predicate -> every shard is scanned.
WHERE type IN ('PushEvent', 'PullRequestEvent', 'WatchEvent', 'IssuesEvent')
  AND repo.name LIKE '%kubernetes%'                 -- leading-wildcard LIKE, full scan
GROUP BY
    repo.name,
    actor.login,
    type
ORDER BY events DESC;
