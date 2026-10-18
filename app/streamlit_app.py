"""Editorial analytics dashboard over the DuckDB build.

    streamlit run app/streamlit_app.py

Reads data/analytics.duckdb read-only. If it is missing or was generated for
an earlier day, it regenerates the synthetic data and rebuilds first, so a
fresh clone or a long-running host always shows a current newsroom.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "analytics.duckdb"

# Validated categorical slots (blue, orange) plus text and surface tokens.
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#8a8984"
SURFACE, GRID, BAND = "#fcfcfb", "#ebeae6", "#f2f1ee"
GOOD, CRITICAL = "#0ca30c", "#d03b3b"
SOURCE_LABEL = {"legacy_property": "Old domain", "new_property": "New domain"}


def nice(value: str) -> str:
    """Slug -> label: 'ai-policy' -> 'AI policy', 'data-stories' -> 'Data stories'."""
    words = str(value).replace("-", " ").split()
    if not words:
        return str(value)
    out = " ".join(w.upper() if w in {"ai", "ngo"} else w for w in words)
    return out[0].upper() + out[1:]

st.set_page_config(page_title="Editorial Analytics · Harbourline Dispatch", page_icon="📰", layout="wide")

st.markdown(f"""
<style>
  .block-container {{ padding-top: 4.2rem; max-width: 1180px; }}
  h1, h2, h3 {{ letter-spacing: -0.01em; }}
  .hdr {{ display:flex; justify-content:space-between; align-items:flex-end; gap:1rem;
          border-bottom:1px solid {GRID}; padding-bottom:.9rem; margin-bottom:1.4rem; flex-wrap:wrap; }}
  .hdr .eyebrow {{ color:{INK_3}; font-size:.78rem; text-transform:uppercase; letter-spacing:.08em; }}
  .hdr .title {{ font-size:1.65rem; font-weight:700; color:{INK}; line-height:1.2; }}
  .pills {{ display:flex; gap:.5rem; flex-wrap:wrap; }}
  .pill {{ border:1px solid {GRID}; background:#fff; border-radius:999px; padding:.28rem .7rem;
           font-size:.8rem; color:{INK_2}; white-space:nowrap; }}
  .pill b {{ color:{INK}; }}
  .pill.good {{ border-color:#bfe6bf; background:#f1faf1; color:#0a6d0a; }}
  .pill.bad {{ border-color:#f1c2c2; background:#fdf2f2; color:#9b1c1c; }}
  .cards {{ display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:.9rem; margin:.2rem 0 1.4rem; }}
  @media (max-width: 900px) {{ .cards {{ grid-template-columns:repeat(2, minmax(0,1fr)); }} }}
  .card {{ background:#fff; border:1px solid {GRID}; border-radius:12px; padding:1rem 1.1rem; }}
  .card .lbl {{ color:{INK_2}; font-size:.82rem; }}
  .card .val {{ color:{INK}; font-size:1.75rem; font-weight:700; margin:.15rem 0 .1rem; font-variant-numeric:tabular-nums; }}
  .card .sub {{ color:{INK_3}; font-size:.78rem; }}
  .up {{ color:#0a6d0a; }} .down {{ color:#9b1c1c; }}
  .note {{ background:{BAND}; border-radius:10px; padding:.8rem 1rem; color:{INK_2}; font-size:.88rem; margin:.4rem 0 1rem; }}
  .section {{ font-size:1.05rem; font-weight:650; color:{INK}; margin:1.3rem 0 .15rem; }}
  .caption {{ color:{INK_3}; font-size:.82rem; margin-bottom:.5rem; }}
  .check {{ display:flex; gap:.6rem; align-items:flex-start; padding:.55rem 0; border-bottom:1px solid {GRID}; }}
  .check .ic {{ font-weight:700; width:1.2rem; }}
  .check .nm {{ color:{INK}; font-weight:600; }}
  .check .dt {{ color:{INK_2}; font-size:.85rem; }}
</style>
""", unsafe_allow_html=True)


# ── data ──────────────────────────────────────────────────────────────────────

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


def report() -> dict:
    return json.loads(q("SELECT report_json FROM build_report").report_json[0])


# ── shared pieces ─────────────────────────────────────────────────────────────

def theme(chart: alt.Chart, height: int = 300) -> alt.Chart:
    return (chart.properties(height=height, background=SURFACE)
            .configure_view(stroke=None)
            .configure_axis(labelColor=INK_2, titleColor=INK_2, gridColor=GRID, domain=False,
                            tickColor=GRID, labelFontSize=11, titleFontSize=11, titleFontWeight="normal")
            .configure_legend(labelColor=INK_2, titleColor=INK_2, orient="top", title=None,
                              symbolType="square", labelFontSize=12, labelLimit=0)
            .configure_text(color=INK_2))


def header(title: str) -> None:
    rep = report()
    ok = sum(a["ok"] for a in rep["assertions"])
    n = len(rep["assertions"])
    status = (f'<span class="pill good">✓ <b>{ok}/{n}</b> integrity checks passing</span>' if ok == n
              else f'<span class="pill bad">✕ <b>{n - ok}</b> integrity check(s) failing</span>')
    last = max(f["last_date"] for f in rep["freshness"])
    st.markdown(f"""
    <div class="hdr">
      <div><div class="eyebrow">Harbourline Dispatch · editorial analytics</div>
           <div class="title">{title}</div></div>
      <div class="pills">{status}
        <span class="pill">Data through <b>{pd.Timestamp(last):%d %b %Y}</b></span>
        <span class="pill">Synthetic demo data</span></div>
    </div>""", unsafe_allow_html=True)


def cards(items: list[tuple[str, str, str]]) -> None:
    html = "".join(f'<div class="card"><div class="lbl">{l}</div><div class="val">{v}</div>'
                   f'<div class="sub">{s}</div></div>' for l, v, s in items)
    st.markdown(f'<div class="cards">{html}</div>', unsafe_allow_html=True)


def section(title: str, caption: str = "") -> None:
    st.markdown(f'<div class="section">{title}</div>' +
                (f'<div class="caption">{caption}</div>' if caption else ""), unsafe_allow_html=True)


def delta(cur: float, prev: float) -> str:
    if not prev:
        return "no prior period"
    d = cur / prev - 1
    cls, arrow = ("up", "▲") if d >= 0 else ("down", "▼")
    return f'<span class="{cls}">{arrow} {abs(d):.1%}</span> vs previous 30 days'


# ── pages ─────────────────────────────────────────────────────────────────────

def page_overview():
    header("Overview")
    last = q("SELECT MAX(date) d FROM fact_page_daily").d[0]
    win = q("""SELECT
        SUM(pageviews) FILTER (WHERE date >  ?::DATE - 30)                         cur,
        SUM(pageviews) FILTER (WHERE date <= ?::DATE - 30 AND date > ?::DATE - 60) prev,
        SUM(pageviews) FILTER (WHERE bucket='article' AND date > ?::DATE - 30)     art_cur,
        SUM(pageviews) FILTER (WHERE bucket='article' AND date <= ?::DATE - 30 AND date > ?::DATE - 60) art_prev,
        SUM(pageviews) FILTER (WHERE source='new_property' AND date > ?::DATE - 30) new_cur
        FROM fact_page_daily""", (last,) * 7)
    total = q("SELECT SUM(pageviews) pv FROM fact_page_daily").pv[0]
    nl = q("SELECT * FROM v_newsletter_reach_total")
    w = win.iloc[0]
    cards([
        ("Pageviews, last 30 days", f"{w.cur:,.0f}", delta(w.cur, w.prev)),
        ("Article views, last 30 days", f"{w.art_cur:,.0f}", delta(w.art_cur, w.art_prev)),
        ("Share on the new domain", f"{w.new_cur / w.cur:.0%}", "of last-30-day pageviews"),
        ("All-time owned pageviews", f"{total:,.0f}", f"{nl.opens[0]:,.0f} newsletter opens"),
    ])

    section("Monthly pageviews by domain",
            "Old-domain and new-domain traffic are different visits, so they stack. "
            "The shaded month is when the new domain was live but not yet measured.")
    m = q("SELECT * FROM v_monthly_traffic")
    m["domain"] = m.source.map(SOURCE_LABEL)
    gap = pd.DataFrame({"start": [pd.Timestamp("2026-05-11")], "end": [pd.Timestamp("2026-06-11")],
                        "label": ["Unmeasured"]})
    band = alt.Chart(gap).mark_rect(color=BAND).encode(x="start:T", x2="end:T")
    band_lbl = alt.Chart(gap).mark_text(dy=-8, fontSize=11, color=INK_3, align="left", dx=4).encode(
        x="start:T", y=alt.value(0), text="label:N")
    bars = alt.Chart(m).mark_bar(stroke=SURFACE, strokeWidth=1.5, cornerRadiusTopLeft=3,
                                 cornerRadiusTopRight=3).encode(
        x=alt.X("yearmonth(month):T", title=None, axis=alt.Axis(format="%b %Y", labelAngle=0, tickCount=8)),
        y=alt.Y("sum(pageviews):Q", title="Pageviews",
                axis=alt.Axis(labelExpr="datum.value == 0 ? '0' : format(datum.value / 1000, ',') + 'k'")),
        color=alt.Color("domain:N", scale=alt.Scale(domain=["Old domain", "New domain"], range=[BLUE, ORANGE])),
        order=alt.Order("domain:N", sort="descending"),
        tooltip=[alt.Tooltip("yearmonth(month):T", title="Month", format="%B %Y"),
                 alt.Tooltip("domain:N", title="Domain"),
                 alt.Tooltip("sum(pageviews):Q", title="Pageviews", format=",")])
    st.altair_chart(theme(band + band_lbl + bars, 300), width="stretch")

    left, right = st.columns([1, 1.35])
    with left:
        section("Where every pageview went", "Nothing is dropped: each view lands in a named bucket.")
        b = q("SELECT bucket, SUM(pageviews) pageviews FROM v_pageview_buckets GROUP BY bucket")
        b["label"] = b.bucket.map({"article": "Articles", "homepage": "Homepage", "archive": "Archive pages",
                                   "removed_article": "Removed articles", "other": "Other"})
        b["share"] = b.pageviews / b.pageviews.sum()
        top_b = float(b.pageviews.max()) * 1.15
        base = alt.Chart(b).encode(y=alt.Y("label:N", sort="-x", title=None, axis=alt.Axis(labelLimit=160)))
        st.altair_chart(theme(
            base.mark_bar(color=BLUE, cornerRadiusEnd=4, height=18).encode(
                x=alt.X("pageviews:Q", title=None, axis=None, scale=alt.Scale(domain=[0, top_b])),
                tooltip=[alt.Tooltip("label:N", title="Bucket"), alt.Tooltip("pageviews:Q", format=","),
                         alt.Tooltip("share:Q", format=".1%")])
            + base.mark_text(align="left", dx=6, fontSize=12, color=INK_2).encode(
                x="pageviews:Q", text=alt.Text("share:Q", format=".1%")), 190), width="stretch")
    with right:
        section("Most-read editorial articles", "Old and new domain summed per article.")
        top = q("""SELECT title AS Article, primary_category AS Category, daily_pageviews AS Views
                   FROM v_article_traffic WHERE reporting_group='editorial'
                   ORDER BY daily_pageviews DESC LIMIT 6""")
        top["Category"] = top.Category.map(nice)
        st.dataframe(top, hide_index=True, width="stretch",
                     column_config={"Article": st.column_config.TextColumn(width="large"),
                                    "Category": st.column_config.TextColumn(width="small"),
                                    "Views": st.column_config.NumberColumn(format="localized", width="small")})


def page_articles():
    header("Articles")
    cats = q("SELECT DISTINCT primary_category FROM dim_article ORDER BY 1").primary_category.tolist()
    c1, c2, c3 = st.columns([1.2, 2, 2.4])
    group = c1.segmented_control("Content", ["editorial", "secondary"], default="editorial",
                                 format_func=str.capitalize)
    cat = c2.multiselect("Category", cats, placeholder="All categories")
    search = c3.text_input("Search titles", placeholder="e.g. harbour")
    df = q("SELECT * FROM v_article_traffic WHERE reporting_group = ?", (group or "editorial",))
    if cat:
        df = df[df.primary_category.isin(cat)]
    if search:
        df = df[df.title.str.contains(search, case=False, na=False)]
    df = df.sort_values("daily_pageviews", ascending=False)
    mism = int((df.primary_category != df.url_category).sum())
    df["primary_category"] = df.primary_category.map(nice)
    df["focus_area"] = df.focus_area.fillna("Untagged").map(nice)
    cards([
        ("Articles", f"{len(df):,}", "matching the filters"),
        ("Views", f"{df.daily_pageviews.sum():,.0f}", "old + new domain"),
        ("Weighted read time", f"{df.engaged_seconds.sum() / max(df.daily_pageviews.sum(), 1) / 60:.1f} min",
         "total engaged time ÷ total views"),
        ("Category disagreements", f"{mism}", "URL and CMS disagree; both kept"),
    ])
    peak = max(int(df.daily_pageviews.max() or 1), 1)
    st.dataframe(
        df[["title", "primary_category", "published_date", "focus_area", "daily_pageviews",
            "pageviews_new_domain", "avg_read_minutes", "vendor_lifetime_pageviews_do_not_sum"]],
        hide_index=True, width="stretch", height=480,
        column_config={
            "title": st.column_config.TextColumn("Article", width="large"),
            "primary_category": "Category",
            "published_date": st.column_config.DateColumn("Published", format="D MMM YYYY"),
            "focus_area": "Focus area",
            "daily_pageviews": st.column_config.ProgressColumn("Views", format="%d", min_value=0, max_value=peak),
            "pageviews_new_domain": st.column_config.NumberColumn("of which new domain", format="localized"),
            "avg_read_minutes": st.column_config.NumberColumn("Avg read (min)", format="%.1f"),
            "vendor_lifetime_pageviews_do_not_sum": st.column_config.NumberColumn(
                "Retired vendor, lifetime", format="localized",
                help="Lifetime total from a retired analytics vendor. It overlaps the daily era, "
                     "so it is shown beside the daily views and never added to them."),
        })


def page_categories():
    header("Categories")
    r = q("SELECT * FROM v_reading_time_by_category ORDER BY weighted_avg_read_minutes DESC")
    worst = r.sort_values("sum_of_averages_wrong", ascending=False).iloc[0]
    st.markdown(f"""<div class="note"><b>Why the method matters.</b> Summing per-article averages,
        as an old dashboard tile might, would report <b>{worst.sum_of_averages_wrong:,.0f} minutes</b> for
        {worst.primary_category}. The correct figure divides total engaged time by total views:
        <b>{worst.weighted_avg_read_minutes:.1f} minutes</b>.</div>""", unsafe_allow_html=True)

    left, right = st.columns(2)
    with left:
        section("Average reading time per view", "Weighted mean (minutes)")
        r["label"] = r.primary_category.map(nice)
        r["txt"] = r.weighted_avg_read_minutes.map("{:.2f}".format)
        top_x = float(r.weighted_avg_read_minutes.max()) * 1.18
        base = alt.Chart(r).encode(y=alt.Y("label:N", sort="-x", title=None))
        st.altair_chart(theme(
            base.mark_bar(color=BLUE, cornerRadiusEnd=4, height=18).encode(
                x=alt.X("weighted_avg_read_minutes:Q", title=None, axis=None, scale=alt.Scale(domain=[0, top_x])),
                tooltip=[alt.Tooltip("label:N", title="Category"),
                         alt.Tooltip("weighted_avg_read_minutes:Q", title="Weighted mean", format=".2f"),
                         alt.Tooltip("mean_of_article_averages:Q", title="Mean of averages", format=".2f"),
                         alt.Tooltip("pageviews:Q", format=",")])
            + base.mark_text(align="left", dx=6, color=INK_2).encode(
                x="weighted_avg_read_minutes:Q", text="txt:N"),
            260), width="stretch")
    with right:
        section("Focus areas", "From a seeded list: areas with no articles still appear, at zero.")
        f = q("SELECT * FROM v_focus_area_coverage")
        f["label"] = f.focus_area.map(nice)
        f["state"] = f.articles.map(lambda n: "Has articles" if n else "No articles yet")
        top_f = float(f.articles.max()) * 1.18
        base = alt.Chart(f).encode(y=alt.Y("label:N", sort="-x", title=None))
        st.altair_chart(theme(
            base.mark_bar(cornerRadiusEnd=4, height=16).encode(
                x=alt.X("articles:Q", title=None, axis=None, scale=alt.Scale(domain=[0, top_f])),
                color=alt.Color("state:N", scale=alt.Scale(domain=["Has articles", "No articles yet"],
                                                           range=[BLUE, INK_3]), legend=None),
                tooltip=[alt.Tooltip("label:N", title="Focus area"), "articles:Q",
                         alt.Tooltip("pageviews:Q", format=",")])
            + base.mark_text(align="left", dx=6, color=INK_2).encode(
                x="articles:Q", text=alt.condition("datum.articles > 0", "articles:Q", alt.value("no articles yet"))),
            260), width="stretch")
    untagged = q("SELECT COUNT(*) n FROM dim_article WHERE reporting_group='editorial' AND focus_area IS NULL").n[0]
    st.caption(f"{untagged} editorial articles are still untagged. That backlog is shown, not hidden.")


def page_newsletter():
    header("Newsletter")
    tot = q("SELECT * FROM v_newsletter_reach_total")
    r = q("SELECT * FROM v_newsletter_reach_by_category")
    over = r.opens_naive_double_counted.sum() / r.opens_distinct_editions.sum() - 1
    cards([("Editions sent", f"{tot.editions[0]:,}", "weekly"),
           ("Unique opens", f"{tot.opens[0]:,.0f}", "each edition counted once"),
           ("Average open rate", f"{tot.avg_open_rate[0]:.1%}", "opens ÷ subscribers"),
           ("Overcount if summed per article", f"+{over:.0%}", "the error this view prevents")])

    section("Opens per edition", "Dots are single editions; the line is a 4-edition rolling average.")
    e = q("SELECT sent_date, unique_opens, unique_opens*1.0/subscribers rate FROM newsletter_edition ORDER BY 1")
    e["trend"] = e.unique_opens.rolling(4, min_periods=1).mean()
    x = alt.X("sent_date:T", title=None, axis=alt.Axis(format="%b %Y"))
    dots = alt.Chart(e).mark_circle(size=22, color=BLUE, opacity=0.28).encode(
        x=x, y=alt.Y("unique_opens:Q", title="Unique opens", scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("sent_date:T", title="Sent", format="%d %b %Y"),
                 alt.Tooltip("unique_opens:Q", title="Opens", format=","),
                 alt.Tooltip("rate:Q", title="Open rate", format=".1%")])
    line = alt.Chart(e).mark_line(color=BLUE, strokeWidth=2).encode(x=x, y="trend:Q")
    st.altair_chart(theme(dots + line, 240), width="stretch")

    section("Reach by category: counted correctly vs naively",
            "One edition carries several articles. Summing opens per article counts the same open "
            "several times. Categories also don't add up to the total, because one edition can reach several.")
    long = r.melt(id_vars="primary_category", value_vars=["opens_distinct_editions", "opens_naive_double_counted"],
                  var_name="method", value_name="opens")
    long["method"] = long.method.map({"opens_distinct_editions": "Each edition once (correct)",
                                      "opens_naive_double_counted": "Summed per article (overcounts)"})
    long["category"] = long.primary_category.map(nice)
    st.altair_chart(theme(alt.Chart(long).mark_bar(cornerRadiusEnd=3, height=10).encode(
        y=alt.Y("category:N", title=None),
        yOffset=alt.YOffset("method:N"),
        x=alt.X("opens:Q", title="Opens", axis=alt.Axis(format="~s")),
        color=alt.Color("method:N", scale=alt.Scale(range=[BLUE, ORANGE])),
        tooltip=["category:N", "method:N", alt.Tooltip("opens:Q", format=",")]), 300), width="stretch")


def page_health():
    header("Data health")
    rep = report()
    left, right = st.columns([1.4, 1])
    with left:
        section("Integrity checks", f"Run on every build. Built {pd.Timestamp(rep['built_at']):%d %b %Y, %H:%M} UTC "
                                    f"in {rep['seconds']}s. The database is only replaced if all pass.")
        rows = "".join(
            f'<div class="check"><div class="ic" style="color:{GOOD if a["ok"] else CRITICAL}">'
            f'{"✓" if a["ok"] else "✕"}</div><div><div class="nm">{a["name"].capitalize()}</div>'
            f'<div class="dt">{a["detail"]}</div></div></div>' for a in rep["assertions"])
        st.markdown(rows, unsafe_allow_html=True)
    with right:
        section("Freshness", "Last day of data per source.")
        fr = pd.DataFrame(rep["freshness"])
        fr["source"] = fr.source.map(SOURCE_LABEL)
        fr["lag"] = (pd.Timestamp(date.today()) - pd.to_datetime(fr.last_date)).dt.days
        cards_html = "".join(
            f'<div class="card" style="margin-bottom:.7rem"><div class="lbl">{s}</div>'
            f'<div class="val">{pd.Timestamp(d):%d %b}</div><div class="sub">'
            f'{"up to date" if l <= 1 else f"{l} days behind"}</div></div>'
            for s, d, l in fr[["source", "last_date", "lag"]].itertuples(index=False))
        st.markdown(cards_html, unsafe_allow_html=True)
        section("Rows loaded")
        names = {"dim_article": "Articles (CMS registry)", "fact_page_daily": "Daily page metrics",
                 "vendor_lifetime_frozen": "Retired vendor, lifetime", "newsletter_edition": "Newsletter editions",
                 "newsletter_edition_article": "Edition-article links"}
        rows = pd.DataFrame({"Table": [names.get(k, k) for k in rep["rows"]], "Rows": list(rep["rows"].values())})
        st.dataframe(rows, hide_index=True, width="stretch",
                     column_config={"Rows": st.column_config.NumberColumn(format="localized")})


ensure_db()
# DASH_PAGE lets tests (and deep links in docs) land on a given page.
LANDING = os.environ.get("DASH_PAGE", "Overview")
nav = st.navigation([
    st.Page(page_overview, title="Overview", icon=":material/insights:", default=LANDING == "Overview"),
    st.Page(page_articles, title="Articles", icon=":material/article:", url_path="articles", default=LANDING == "Articles"),
    st.Page(page_categories, title="Categories", icon=":material/category:", url_path="categories", default=LANDING == "Categories"),
    st.Page(page_newsletter, title="Newsletter", icon=":material/mail:", url_path="newsletter", default=LANDING == "Newsletter"),
    st.Page(page_health, title="Data health", icon=":material/verified:", url_path="health", default=LANDING == "Data health"),
], position="top")
nav.run()
