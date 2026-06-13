-- Flink SQL clickstream rollup.
-- Deliberately exhibits streaming anti-patterns: a source table with NO
-- WATERMARK, a very wide 2 HOUR tumbling window, and an ORDER BY over an
-- unbounded stream.
CREATE TABLE clicks (
    user_id    BIGINT,
    page_id    STRING,
    event_time TIMESTAMP(3)
    -- no WATERMARK FOR event_time: event-time windows can never fire cleanly
) WITH (
    'connector' = 'kafka',
    'topic' = 'clicks',
    'properties.bootstrap.servers' = 'broker:9092',
    'format' = 'json'
);

CREATE TABLE clicks_rollup (
    window_start TIMESTAMP(3),
    page_id      STRING,
    clicks       BIGINT
) WITH (
    'connector' = 'kafka',
    'topic' = 'clicks_rollup',
    'format' = 'json'
);

INSERT INTO clicks_rollup
SELECT
    TUMBLE_START(event_time, INTERVAL '2' HOUR) AS window_start,
    page_id,
    COUNT(*) AS clicks
FROM clicks
GROUP BY TUMBLE(event_time, INTERVAL '2' HOUR), page_id
ORDER BY clicks DESC;   -- ORDER BY over an unbounded stream
