-- Tables. Constraints encode the design rules; build_db.py adds the
-- invariants a schema cannot express (see ASSERTIONS there).

-- The CMS owns article identity. Analytics attach to articles; they never
-- create them, so an article with no traffic still has a row.
CREATE TABLE dim_article (
    article_id        INTEGER PRIMARY KEY,
    slug              VARCHAR NOT NULL UNIQUE,
    title             VARCHAR NOT NULL,
    primary_category  VARCHAR NOT NULL,   -- what the CMS says
    url_category      VARCHAR NOT NULL,   -- what the URL says; disagreements kept, not resolved
    reporting_group   VARCHAR NOT NULL CHECK (reporting_group IN ('editorial', 'secondary')),
    published_date    DATE    NOT NULL,
    word_count        INTEGER NOT NULL,
    focus_area        VARCHAR             -- from the human-edited tags file; NULL = untagged backlog
);

-- Seeded vocabulary, not SELECT DISTINCT: a focus area with no articles
-- must still appear (with zero) rather than silently vanish.
CREATE TABLE dim_focus_area (focus_area VARCHAR PRIMARY KEY);

-- One row per (source, local day, path). Grain is explicit: daily only.
CREATE TABLE fact_page_daily (
    source           VARCHAR NOT NULL CHECK (source IN ('legacy_property', 'new_property')),
    date             DATE    NOT NULL,
    path             VARCHAR NOT NULL,
    slug             VARCHAR,            -- NULL unless the path resolves to a registry article
    bucket           VARCHAR NOT NULL CHECK (bucket IN ('article', 'removed_article', 'homepage', 'archive', 'other')),
    pageviews        BIGINT  NOT NULL DEFAULT 0 CHECK (pageviews >= 0),
    engaged_seconds  DOUBLE  NOT NULL DEFAULT 0 CHECK (engaged_seconds >= 0),
    UNIQUE (source, date, path)
);

-- Retired vendor. LIFETIME grain only: no dates exist and none are invented.
CREATE TABLE vendor_lifetime_frozen (
    slug               VARCHAR NOT NULL UNIQUE,
    lifetime_pageviews BIGINT  NOT NULL CHECK (lifetime_pageviews >= 0),
    avg_read_seconds   DOUBLE  NOT NULL,
    window_start       DATE    NOT NULL,
    window_end         DATE    NOT NULL
);

CREATE TABLE newsletter_edition (
    edition_id    INTEGER PRIMARY KEY,
    sent_date     DATE    NOT NULL,
    subscribers   INTEGER NOT NULL,
    unique_opens  INTEGER NOT NULL CHECK (unique_opens >= 0)
);

CREATE TABLE newsletter_edition_article (
    edition_id INTEGER NOT NULL,
    slug       VARCHAR NOT NULL,
    UNIQUE (edition_id, slug)
);
