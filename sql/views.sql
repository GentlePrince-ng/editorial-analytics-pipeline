-- Views the dashboard reads. Each one encodes a rule that, when it was done
-- by hand, produced a wrong number.

-- Daily traffic per article. Old-domain and new-domain rows are SUMMED by
-- slug: after the migration a reader hit one URL or the other, so these are
-- different visits. The vendor's lifetime figure is shown BESIDE it, never
-- added to it: both tools measured the same readers in the overlap years.
CREATE VIEW v_article_traffic AS
SELECT
    a.slug, a.title, a.primary_category, a.url_category, a.reporting_group,
    a.published_date, a.focus_area, a.word_count,
    COALESCE(SUM(f.pageviews), 0)                                   AS daily_pageviews,
    COALESCE(SUM(f.pageviews) FILTER (WHERE f.source = 'legacy_property'), 0) AS pageviews_old_domain,
    COALESCE(SUM(f.pageviews) FILTER (WHERE f.source = 'new_property'), 0)    AS pageviews_new_domain,
    COALESCE(SUM(f.engaged_seconds), 0)                             AS engaged_seconds,
    CASE WHEN SUM(f.pageviews) > 0
         THEN SUM(f.engaged_seconds) / SUM(f.pageviews) / 60 END   AS avg_read_minutes,
    v.lifetime_pageviews                                            AS vendor_lifetime_pageviews_do_not_sum,
    MIN(f.date) AS first_measured, MAX(f.date) AS last_measured
FROM dim_article a
LEFT JOIN fact_page_daily f ON f.slug = a.slug AND f.bucket = 'article'
LEFT JOIN vendor_lifetime_frozen v ON v.slug = a.slug
GROUP BY ALL;

-- Reading time by category, three ways, so the error is visible:
--   weighted mean   = total engaged time / total views          (correct)
--   mean of means   = average of per-article averages            (biased toward tiny articles)
--   sum of means    = what a dashboard tile summing averages shows (meaningless)
CREATE VIEW v_reading_time_by_category AS
SELECT
    primary_category,
    COUNT(*) FILTER (WHERE daily_pageviews > 0)       AS articles_with_traffic,
    SUM(daily_pageviews)                              AS pageviews,
    SUM(engaged_seconds) / NULLIF(SUM(daily_pageviews), 0) / 60 AS weighted_avg_read_minutes,
    AVG(avg_read_minutes)                             AS mean_of_article_averages,
    SUM(avg_read_minutes)                             AS sum_of_averages_wrong
FROM v_article_traffic
WHERE reporting_group = 'editorial'
GROUP BY primary_category;

-- Newsletter: opens belong to an EDITION. An article's reach is the opens
-- of the editions that carried it (fine per article), but category and
-- site totals must count each edition once, or one open is counted once
-- per article in it.
CREATE VIEW v_article_newsletter_reach AS
SELECT ea.slug, COUNT(*) AS editions, SUM(e.unique_opens) AS opens_of_carrying_editions
FROM newsletter_edition_article ea JOIN newsletter_edition e USING (edition_id)
GROUP BY ea.slug;

CREATE VIEW v_newsletter_reach_by_category AS
WITH links AS (
    SELECT ea.edition_id, a.primary_category, e.unique_opens
    FROM newsletter_edition_article ea
    JOIN dim_article a USING (slug)
    JOIN newsletter_edition e USING (edition_id)
),
edition_category AS (SELECT DISTINCT edition_id, primary_category, unique_opens FROM links)
SELECT ec.primary_category,
       COUNT(*)              AS editions,
       SUM(ec.unique_opens)  AS opens_distinct_editions,
       (SELECT SUM(l.unique_opens) FROM links l
         WHERE l.primary_category = ec.primary_category) AS opens_naive_double_counted
FROM edition_category ec
GROUP BY ec.primary_category;

CREATE VIEW v_newsletter_reach_total AS
SELECT COUNT(*) AS editions, SUM(unique_opens) AS opens, AVG(unique_opens * 1.0 / subscribers) AS avg_open_rate
FROM newsletter_edition;

-- Every owned pageview lands in a named bucket. Nothing is dropped.
CREATE VIEW v_pageview_buckets AS
SELECT bucket, source, SUM(pageviews) AS pageviews
FROM fact_page_daily GROUP BY ALL;

CREATE VIEW v_monthly_traffic AS
SELECT date_trunc('month', date)::DATE AS month, source, SUM(pageviews) AS pageviews
FROM fact_page_daily GROUP BY ALL ORDER BY month;

-- Focus areas from the seeded vocabulary, including those with no articles.
CREATE VIEW v_focus_area_coverage AS
SELECT fa.focus_area,
       COUNT(t.slug) AS articles,
       COALESCE(SUM(t.daily_pageviews), 0) AS pageviews
FROM dim_focus_area fa
LEFT JOIN v_article_traffic t ON t.focus_area = fa.focus_area
GROUP BY fa.focus_area;
