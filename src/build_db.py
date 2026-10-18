"""Rebuild the DuckDB database from raw files, then prove it is right.

The database is a pure function of data/raw/: it is rebuilt from scratch on
every run into a temporary file and only moved into place once every
assertion passes, so a failed run can never leave a half-written database
behind for the dashboard to read.

    python src/build_db.py                       # data/raw -> data/analytics.duckdb
    python src/build_db.py --raw tmp/raw --db tmp/test.duckdb

Exit code 0 = built and verified; 1 = an assertion failed (nothing replaced).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
LOCAL_TZ = "Africa/Lagos"
NEW_PROPERTY_TZ = "America/Los_Angeles"
FOCUS_AREAS = ["climate", "health", "education", "economy", "governance",
               "technology", "culture", "agriculture", "urban-mobility", "ai-policy"]


# ── transforms (pure, unit-tested) ────────────────────────────────────────────

def rebucket_hourly_to_local_days(hourly: pd.DataFrame) -> pd.DataFrame:
    """The new property reports hours on its own clock. Convert each hour to
    the newsroom's timezone and re-aggregate by LOCAL date. Changing the
    property's timezone setting would not fix history: analytics tools do
    not re-bucket data already collected."""
    if hourly.empty:
        return pd.DataFrame(columns=["date", "path", "pageviews", "engaged_seconds"])
    ts = pd.to_datetime(hourly["hour_property_tz"])
    # The repeated 01:00 when DST ends is ambiguous in the property's clock.
    # Read either way it is 09:00 or 10:00 in Lagos on the SAME local day, so
    # the date we bucket into is not in doubt; take the DST reading.
    local_dst = ts.dt.tz_localize(NEW_PROPERTY_TZ, ambiguous=True, nonexistent="shift_forward").dt.tz_convert(LOCAL_TZ)
    local_std = ts.dt.tz_localize(NEW_PROPERTY_TZ, ambiguous=False, nonexistent="shift_forward").dt.tz_convert(LOCAL_TZ)
    if (local_dst.dt.date != local_std.dt.date).any():
        raise ValueError("an ambiguous DST hour falls on different local days depending on reading")
    out = hourly.assign(date=local_dst.dt.date)
    return out.groupby(["date", "path"], as_index=False)[["pageviews", "engaged_seconds"]].sum()


def classify_path(path: str, source: str, slugs: set[str]) -> tuple[str | None, str]:
    """Resolve a path to (slug, bucket). Analytics never create articles: a
    path whose slug is not in the CMS registry is a removed article, not a
    new one."""
    if path == "/":
        return None, "homepage"
    if path.startswith(("/category/", "/tag/")):
        return None, "archive"
    parts = [p for p in path.strip("/").split("/") if p]
    if source == "legacy_property" and len(parts) == 3 and parts[0] == "articles":
        slug = parts[2]
    elif source == "new_property" and len(parts) == 1:
        slug = parts[0]
    else:
        return None, "other"
    return (slug, "article") if slug in slugs else (None, "removed_article")


# ── build ─────────────────────────────────────────────────────────────────────

def build(raw: Path, db_path: Path) -> dict:
    t0 = time.time()
    cms = pd.read_parquet(raw / "cms_articles.parquet")
    legacy = pd.read_parquet(raw / "analytics_legacy_daily.parquet")
    hourly = pd.read_parquet(raw / "analytics_new_hourly.parquet")
    vendor = pd.read_parquet(raw / "vendor_lifetime_frozen.parquet")
    editions = pd.read_parquet(raw / "newsletter_editions.parquet")
    links = pd.read_parquet(raw / "newsletter_edition_articles.parquet")
    tags = pd.read_csv(raw / "tags.csv")

    slugs = set(cms["slug"])
    new_daily = rebucket_hourly_to_local_days(hourly)
    facts = []
    for source, frame in (("legacy_property", legacy), ("new_property", new_daily)):
        f = frame.copy()
        f["date"] = pd.to_datetime(f["date"]).dt.date
        resolved = [classify_path(p, source, slugs) for p in f["path"]]
        f["slug"] = [r[0] for r in resolved]
        f["bucket"] = [r[1] for r in resolved]
        f["source"] = source
        facts.append(f)
    fact = pd.concat(facts, ignore_index=True)[
        ["source", "date", "path", "slug", "bucket", "pageviews", "engaged_seconds"]]

    article = cms.merge(tags, on="slug", how="left")

    tmp = db_path.with_suffix(".building.duckdb")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    con = duckdb.connect(str(tmp))
    con.execute((ROOT / "sql" / "schema.sql").read_text())
    for name, frame in (("article", article), ("fact", fact), ("vendor", vendor),
                        ("editions", editions), ("links", links)):
        con.register(name, frame)
    con.execute("""INSERT INTO dim_article SELECT article_id, slug, title, primary_category,
                   url_category, reporting_group, published_date, word_count, focus_area FROM article""")
    con.executemany("INSERT INTO dim_focus_area VALUES (?)", [[f] for f in FOCUS_AREAS])
    con.execute("INSERT INTO fact_page_daily SELECT * FROM fact")
    con.execute("INSERT INTO vendor_lifetime_frozen SELECT * FROM vendor")
    con.execute("INSERT INTO newsletter_edition SELECT * FROM editions")
    con.execute("INSERT INTO newsletter_edition_article SELECT * FROM links")
    con.execute((ROOT / "sql" / "views.sql").read_text())

    raw_totals = {"legacy_property": int(legacy["pageviews"].sum()),
                  "new_property": int(hourly["pageviews"].sum())}
    results = run_assertions(con, raw_totals, links, slugs)
    report = {
        "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.time() - t0, 2),
        "rows": {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("dim_article", "fact_page_daily", "vendor_lifetime_frozen",
                           "newsletter_edition", "newsletter_edition_article")},
        "freshness": freshness(con),
        "assertions": results,
        "passed": all(r["ok"] for r in results),
    }
    con.execute("CREATE TABLE build_report AS SELECT ? AS report_json", [json.dumps(report)])
    con.close()

    if report["passed"]:
        os.replace(tmp, db_path)   # atomic on the same volume
    else:
        tmp.unlink(missing_ok=True)
    return report


# ── assertions ────────────────────────────────────────────────────────────────

def run_assertions(con, raw_totals: dict, links: pd.DataFrame, slugs: set[str]) -> list[dict]:
    """Invariants the schema cannot express. Each guards a real failure mode."""
    out = []
    SOURCE_NAME = {"legacy_property": "old-domain", "new_property": "new-domain"}

    def check(name, ok, detail):
        out.append({"name": name, "ok": bool(ok), "detail": detail})

    for source, raw_total in raw_totals.items():
        loaded = con.execute("SELECT COALESCE(SUM(pageviews),0) FROM fact_page_daily WHERE source=?",
                             [source]).fetchone()[0]
        check(f"every {SOURCE_NAME[source]} pageview accounted for",
              loaded == raw_total, f"raw {raw_total:,} vs loaded {loaded:,}")

    buckets = con.execute("SELECT COALESCE(SUM(pageviews),0) FROM v_pageview_buckets").fetchone()[0]
    check("buckets sum to the owned total", buckets == sum(raw_totals.values()),
          f"{buckets:,} in named buckets")

    orphans = con.execute("""SELECT COUNT(*) FROM fact_page_daily
                             WHERE bucket='article' AND slug NOT IN (SELECT slug FROM dim_article)""").fetchone()[0]
    check("analytics never create articles", orphans == 0, f"{orphans} article rows without a registry entry")

    bad_links = sorted(set(links["slug"]) - slugs)
    check("newsletter links resolve to registry articles", not bad_links, f"{len(bad_links)} unresolved")

    double = con.execute("""SELECT COUNT(*) FROM v_article_traffic
                            WHERE daily_pageviews <> pageviews_old_domain + pageviews_new_domain""").fetchone()[0]
    check("old- and new-domain views add up for every article", double == 0, f"{double} articles where the parts don't add up")

    pre_new = con.execute("""SELECT COUNT(*) FROM fact_page_daily
                             WHERE source='new_property' AND date < DATE '2026-06-10'""").fetchone()[0]
    check("no new-domain data before measurement began", pre_new == 0,
          f"{pre_new} rows dated before the property was created (would mean bad timezone re-bucketing)")

    focus = con.execute("SELECT COUNT(*) FROM v_focus_area_coverage").fetchone()[0]
    check("every seeded focus area reported, even with no articles", focus == len(FOCUS_AREAS),
          f"{focus} of {len(FOCUS_AREAS)}")
    return out


def freshness(con) -> list[dict]:
    rows = con.execute("""SELECT source, MAX(date) FROM fact_page_daily GROUP BY source ORDER BY source""").fetchall()
    return [{"source": s, "last_date": d.isoformat()} for s, d in rows]


def main(argv=None) -> int:
    # Windows consoles default to cp1252; the digest uses emoji and dashes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--db", default=str(ROOT / "data" / "analytics.duckdb"))
    args = ap.parse_args(argv)
    report = build(Path(args.raw), Path(args.db))
    for r in report["assertions"]:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}: {r['detail']}")
    print(f"rows: {report['rows']}  ({report['seconds']}s)")
    (Path(args.db).parent / "build_report.json").write_text(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
