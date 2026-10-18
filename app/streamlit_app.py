"""Editorial analytics dashboard over the DuckDB build.

    streamlit run app/streamlit_app.py

Reads data/analytics.duckdb read-only. If it is missing, it builds it from
the synthetic generator first, so a fresh clone (or Streamlit Cloud) works
with no setup.
"""
from __future__ import annotations

import json
import subprocess
from datetime import date
import sys
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analytics.duckdb"

st.set_page_config(page_title="Editorial Analytics", page_icon="📰", layout="wide")


def db_is_current() -> bool:
    """True if the database exists and was generated for today. On a host
    that keeps running for weeks (Streamlit Cloud), regenerating daily keeps
    the synthetic newsroom 'live' instead of slowly going stale."""
    manifest = ROOT / "data" / "raw" / "manifest.json"
    if not DB.exists() or not manifest.exists():
        return False
    return json.loads(manifest.read_text()).get("generated_for") == date.today().isoformat()


def ensure_db() -> None:
    if db_is_current():
        return
    with st.spinner("Generating today's synthetic data and rebuilding the database…"):
        for script in ("generate_synthetic.py", "build_db.py"):
            subprocess.run([sys.executable, str(ROOT / "src" / script)], check=True, cwd=ROOT)
    q.clear()  # cached query results belong to the previous build


@st.cache_data(ttl=600)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    with duckdb.connect(str(DB), read_only=True) as con:
        return con.execute(sql, list(params)).df()


def kpi_row(items: list[tuple[str, str, str | None]]) -> None:
    for col, (label, value, help_) in zip(st.columns(len(items)), items):
        col.metric(label, value, help=help_)


# ── pages ─────────────────────────────────────────────────────────────────────

def page_overview():
    st.title("Overview")
    st.caption("Harbourline Dispatch is a fictional newsroom. All data is synthetic.")
    tot = q("SELECT SUM(pageviews) pv FROM fact_page_daily").pv[0]
    art = q("SELECT SUM(daily_pageviews) pv, COUNT(*) FILTER (WHERE daily_pageviews>0) n FROM v_article_traffic "
            "WHERE reporting_group='editorial'")
    nl = q("SELECT * FROM v_newsletter_reach_total")
    kpi_row([
        ("Owned pageviews", f"{tot:,.0f}", "Every pageview from both analytics properties, all buckets"),
        ("Editorial article views", f"{art.pv[0]:,.0f}", "Old + new domain summed by slug"),
        ("Articles with traffic", f"{art.n[0]:,}", None),
        ("Newsletter opens", f"{nl.opens[0]:,.0f}", "Counted once per edition, never per article"),
    ])

    st.subheader("Monthly pageviews by analytics property")
    m = q("SELECT * FROM v_monthly_traffic")
    chart = alt.Chart(m).mark_bar().encode(
        x=alt.X("month:T", title=None),
        y=alt.Y("pageviews:Q", title="Pageviews", stack=True),
        color=alt.Color("source:N", title="Property",
                        scale=alt.Scale(domain=["legacy_property", "new_property"],
                                        range=["#8DA3C7", "#2F6FED"])),
        tooltip=["month:T", "source:N", alt.Tooltip("pageviews:Q", format=",")],
    ).properties(height=320)
    rules = alt.Chart(pd.DataFrame({
        "date": pd.to_datetime(["2026-05-11", "2026-06-11"]),
        "label": ["Domain migration", "New property starts measuring"],
    })).mark_rule(strokeDash=[4, 3], color="#B54708").encode(x="date:T", tooltip=["label:N", "date:T"])
    st.altair_chart(chart + rules, width="stretch")
    st.info("Between the domain migration (11 May) and the new property's creation (11 June) "
            "the new domain was live but **unmeasured**. That traffic cannot be recovered and is "
            "footnoted rather than estimated.")

    st.subheader("Where every pageview went")
    b = q("SELECT bucket, SUM(pageviews) pageviews FROM v_pageview_buckets GROUP BY bucket ORDER BY pageviews DESC")
    b["share"] = b.pageviews / b.pageviews.sum()
    st.dataframe(b, hide_index=True, width="stretch",
                 column_config={"pageviews": st.column_config.NumberColumn(format="%d"),
                                "share": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=1)})


def page_articles():
    st.title("Articles")
    cats = q("SELECT DISTINCT primary_category FROM dim_article ORDER BY 1").primary_category.tolist()
    c1, c2, c3 = st.columns([2, 2, 3])
    group = c1.selectbox("Reporting group", ["editorial", "secondary"])
    cat = c2.multiselect("Category", cats)
    search = c3.text_input("Search titles")
    df = q("SELECT * FROM v_article_traffic WHERE reporting_group = ?", (group,))
    if cat:
        df = df[df.primary_category.isin(cat)]
    if search:
        df = df[df.title.str.contains(search, case=False, na=False)]
    df = df.sort_values("daily_pageviews", ascending=False)
    st.caption(f"{len(df):,} articles. `vendor_lifetime_pageviews_do_not_sum` is the retired vendor's "
               "lifetime figure: it overlaps the daily era and is shown beside, never added to, daily views.")
    st.dataframe(
        df[["title", "primary_category", "url_category", "published_date", "focus_area",
            "daily_pageviews", "pageviews_old_domain", "pageviews_new_domain",
            "avg_read_minutes", "vendor_lifetime_pageviews_do_not_sum"]],
        hide_index=True, width="stretch", height=520,
        column_config={"avg_read_minutes": st.column_config.NumberColumn(format="%.2f")},
    )
    mism = (df.primary_category != df.url_category).sum()
    if mism:
        st.warning(f"{mism} of these articles have a URL category that disagrees with the CMS. "
                   "Both are stored; neither is silently chosen.")


def page_categories():
    st.title("Categories")
    r = q("SELECT * FROM v_reading_time_by_category ORDER BY pageviews DESC")
    st.subheader("Average reading time: the right way and two wrong ways")
    st.dataframe(r, hide_index=True, width="stretch", column_config={
        "weighted_avg_read_minutes": st.column_config.NumberColumn("Weighted mean (correct)", format="%.2f"),
        "mean_of_article_averages": st.column_config.NumberColumn("Mean of averages", format="%.2f"),
        "sum_of_averages_wrong": st.column_config.NumberColumn("Sum of averages (wrong)", format="%.1f"),
    })
    st.caption("A dashboard tile that sums per-article averages reports hundreds of 'minutes'. "
               "The weighted mean divides total engaged time by total views.")

    st.subheader("Focus areas, including those with no articles yet")
    f = q("SELECT * FROM v_focus_area_coverage ORDER BY articles DESC")
    st.altair_chart(alt.Chart(f).mark_bar(color="#2F6FED").encode(
        x=alt.X("articles:Q"), y=alt.Y("focus_area:N", sort="-x", title=None),
        tooltip=["focus_area", "articles", alt.Tooltip("pageviews:Q", format=",")]
    ).properties(height=320), width="stretch")
    untagged = q("SELECT COUNT(*) n FROM dim_article WHERE reporting_group='editorial' AND focus_area IS NULL").n[0]
    st.caption(f"{untagged} editorial articles are still untagged: a real backlog, shown rather than hidden.")


def page_newsletter():
    st.title("Newsletter")
    tot = q("SELECT * FROM v_newsletter_reach_total")
    kpi_row([("Editions", f"{tot.editions[0]:,}", None),
             ("Unique opens", f"{tot.opens[0]:,.0f}", None),
             ("Average open rate", f"{tot.avg_open_rate[0]:.1%}", None)])
    st.subheader("Reach by category")
    r = q("SELECT * FROM v_newsletter_reach_by_category ORDER BY opens_distinct_editions DESC")
    r["overcount"] = r.opens_naive_double_counted / r.opens_distinct_editions - 1
    st.dataframe(r, hide_index=True, width="stretch", column_config={
        "opens_distinct_editions": st.column_config.NumberColumn("Opens (each edition once)", format="%d"),
        "opens_naive_double_counted": st.column_config.NumberColumn("Naive sum per article", format="%d"),
        "overcount": st.column_config.NumberColumn("Naive overcount", format="percent"),
    })
    st.caption("One edition carries several articles. Summing opens per article counts the same open "
               "once per article. Categories also do not add up to the site total, because one "
               "edition can reach several categories.")


def page_health():
    st.title("Data health")
    rep = json.loads(q("SELECT report_json FROM build_report").report_json[0])
    st.write(f"Built **{rep['built_at']}** in {rep['seconds']}s.")
    for a in rep["assertions"]:
        (st.success if a["ok"] else st.error)(f"**{a['name']}**: {a['detail']}")
    st.subheader("Freshness")
    st.dataframe(pd.DataFrame(rep["freshness"]), hide_index=True)
    st.subheader("Row counts")
    st.json(rep["rows"])


PAGES = {"Overview": page_overview, "Articles": page_articles, "Categories": page_categories,
         "Newsletter": page_newsletter, "Data health": page_health}

ensure_db()
with st.sidebar:
    st.markdown("### Editorial Analytics")
    choice = st.radio("Page", list(PAGES), label_visibility="collapsed")
    st.caption("Synthetic demo. Pipeline: raw files → DuckDB rebuild with assertions → Streamlit.")
PAGES[choice]()
