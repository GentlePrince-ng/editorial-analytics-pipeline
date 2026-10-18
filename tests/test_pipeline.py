"""Tests for the rules that, broken, silently produce wrong numbers."""
import datetime as dt
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import build_db  # noqa: E402
import generate_synthetic  # noqa: E402


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pipeline")
    raw = tmp / "raw"
    generate_synthetic.main(["--today", "2026-10-18", "--articles", "150", "--out", str(raw)])
    report = build_db.build(raw, tmp / "test.duckdb")
    con = duckdb.connect(str(tmp / "test.duckdb"), read_only=True)
    yield report, con, raw
    con.close()


def test_build_passes_every_assertion(built):
    report, _, _ = built
    failed = [a for a in report["assertions"] if not a["ok"]]
    assert not failed, failed


def test_rebucketing_moves_late_la_hours_onto_next_lagos_day():
    # 22:00 in Los Angeles (PDT, UTC-7) on 12 June is 06:00 on 13 June in Lagos (UTC+1).
    hourly = pd.DataFrame({"hour_property_tz": [pd.Timestamp("2026-06-12 22:00"),
                                                pd.Timestamp("2026-06-12 10:00")],
                           "path": ["/a/", "/a/"], "pageviews": [5, 3], "engaged_seconds": [50.0, 30.0]})
    out = build_db.rebucket_hourly_to_local_days(hourly).set_index("date")
    assert out.loc[dt.date(2026, 6, 13), "pageviews"] == 5
    assert out.loc[dt.date(2026, 6, 12), "pageviews"] == 3


def test_rebucketing_preserves_totals(built):
    _, con, raw = built
    hourly = pd.read_parquet(raw / "analytics_new_hourly.parquet")
    loaded = con.execute("SELECT SUM(pageviews) FROM fact_page_daily WHERE source='new_property'").fetchone()[0]
    assert loaded == hourly.pageviews.sum()


def test_ambiguous_dst_hour_lands_on_the_one_possible_local_day():
    # 01:00 on 1 Nov 2026 happens twice in Los Angeles. Either reading is
    # 1 Nov in Lagos, so the build must place it there, not crash the daily run.
    hourly = pd.DataFrame({"hour_property_tz": [pd.Timestamp("2026-11-01 01:00")],
                           "path": ["/a/"], "pageviews": [4], "engaged_seconds": [1.0]})
    out = build_db.rebucket_hourly_to_local_days(hourly)
    assert out.date.tolist() == [dt.date(2026, 11, 1)]
    assert out.pageviews.sum() == 4


def test_generator_and_build_survive_the_dst_change(tmp_path):
    raw = tmp_path / "raw"
    generate_synthetic.main(["--today", "2026-11-03", "--articles", "60", "--out", str(raw)])
    assert build_db.build(raw, tmp_path / "dst.duckdb")["passed"]


@pytest.mark.parametrize("path,source,expected", [
    ("/", "legacy_property", (None, "homepage")),
    ("/category/news/", "legacy_property", (None, "archive")),
    ("/articles/news/known/", "legacy_property", ("known", "article")),
    ("/articles/news/gone/", "legacy_property", (None, "removed_article")),
    ("/known/", "new_property", ("known", "article")),
    ("/known/", "legacy_property", (None, "other")),
    ("/feed/rss/", "new_property", (None, "other")),
])
def test_classify_path_never_creates_articles(path, source, expected):
    assert build_db.classify_path(path, source, {"known"}) == expected


def test_old_and_new_domain_are_summed_by_slug(built):
    _, con, _ = built
    bad = con.execute("""SELECT COUNT(*) FROM v_article_traffic
                         WHERE daily_pageviews <> pageviews_old_domain + pageviews_new_domain""").fetchone()[0]
    assert bad == 0
    both = con.execute("""SELECT COUNT(*) FROM v_article_traffic
                          WHERE pageviews_old_domain > 0 AND pageviews_new_domain > 0""").fetchone()[0]
    assert both > 0, "fixture should contain articles read on both domains"


def test_vendor_lifetime_is_never_in_daily_totals(built):
    _, con, _ = built
    # The daily total must equal the sum of daily facts alone, with no vendor figure added in.
    view = con.execute("SELECT SUM(daily_pageviews) FROM v_article_traffic").fetchone()[0]
    facts = con.execute("SELECT SUM(pageviews) FROM fact_page_daily WHERE bucket='article'").fetchone()[0]
    assert view == facts


def test_weighted_reading_time_is_not_a_mean_of_means(built):
    _, con, _ = built
    r = con.execute("SELECT * FROM v_reading_time_by_category").df()
    manual = con.execute("""SELECT primary_category, SUM(engaged_seconds)/SUM(daily_pageviews)/60 w
                            FROM v_article_traffic WHERE reporting_group='editorial'
                            GROUP BY 1""").df().set_index("primary_category").w
    for row in r.itertuples():
        assert row.weighted_avg_read_minutes == pytest.approx(manual[row.primary_category])
        assert row.sum_of_averages_wrong > row.weighted_avg_read_minutes  # the bug it guards against


def test_newsletter_opens_counted_once_per_edition(built):
    _, con, _ = built
    total = con.execute("SELECT opens FROM v_newsletter_reach_total").fetchone()[0]
    editions = con.execute("SELECT SUM(unique_opens) FROM newsletter_edition").fetchone()[0]
    assert total == editions
    r = con.execute("SELECT * FROM v_newsletter_reach_by_category").df()
    assert (r.opens_naive_double_counted >= r.opens_distinct_editions).all()


def test_seeded_focus_areas_appear_with_zero(built):
    _, con, _ = built
    r = con.execute("SELECT * FROM v_focus_area_coverage").df().set_index("focus_area")
    assert r.loc["ai-policy", "articles"] == 0
    assert r.loc["urban-mobility", "articles"] == 0


def test_unmeasured_gap_is_not_filled(built):
    _, con, _ = built
    n = con.execute("""SELECT COUNT(*) FROM fact_page_daily
                       WHERE source='new_property' AND date < DATE '2026-06-11'""").fetchone()[0]
    assert n == 0


def test_failed_assertion_leaves_previous_database_untouched(tmp_path, built):
    _, _, raw = built
    db = tmp_path / "keep.duckdb"
    assert build_db.build(raw, db)["passed"]
    before = db.stat().st_mtime_ns
    # Break the raw data: a newsletter link to an article that does not exist.
    broken = tmp_path / "raw"
    broken.mkdir()
    for f in raw.iterdir():
        (broken / f.name).write_bytes(f.read_bytes())
    links = pd.read_parquet(broken / "newsletter_edition_articles.parquet")
    links.loc[0, "slug"] = "no-such-article"
    links.to_parquet(broken / "newsletter_edition_articles.parquet", index=False)
    report = build_db.build(broken, db)
    assert not report["passed"]
    assert db.stat().st_mtime_ns == before


@pytest.mark.parametrize("page", ["Overview", "Articles", "Categories", "Newsletter", "Data health"])
def test_dashboard_page_renders(page, monkeypatch):
    # The app reads data/analytics.duckdb and builds it on first run if absent,
    # so this also exercises the fresh-clone path in CI.
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("DASH_PAGE", page)
    app = AppTest.from_file(str(ROOT / "app" / "streamlit_app.py"), default_timeout=60)
    app.run()
    assert not app.exception, [e.value for e in app.exception]
