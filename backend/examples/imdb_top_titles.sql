-- IMDb non-commercial datasets (open): top-rated titles join.
-- Real schema:
--   title_basics(tconst, primarytitle, startyear, genres)
--   title_ratings(tconst, averagerating, numvotes)
-- Deliberately riddled with anti-patterns for the copilot to surface.
CREATE OR REPLACE TABLE analytics.imdb_top_titles AS
SELECT
    b.*,                                        -- SELECT * ships every title column
    r.averagerating,
    r.numvotes,
    -- correlated scalar subquery in the projection: runs once per outer row
    (
        SELECT COUNT(*)
        FROM imdb.title_basics b2
        WHERE b2.startyear = b.startyear
    ) AS titles_in_year
FROM imdb.title_basics  b
JOIN imdb.title_ratings r
  ON r.tconst = b.tconst
WHERE r.numvotes > 10000
  AND r.averagerating >= 8.0
  AND b.primarytitle LIKE '%Star%'              -- leading-wildcard LIKE on primarytitle
  AND b.tconst NOT IN (                         -- NOT IN over a nullable subquery
        SELECT a.tconst
        FROM imdb.title_akas a
  )
ORDER BY r.averagerating DESC, r.numvotes DESC;
