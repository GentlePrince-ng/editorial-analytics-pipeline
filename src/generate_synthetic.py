"""Generate a synthetic newsroom whose analytics have the problems real ones do.

Everything here is FICTIONAL: the publication (Harbourline Dispatch), its
articles, domains (*.example, reserved by RFC 2606) and every number. The data
is shaped to reproduce the measurement traps the pipeline exists to handle:

* a domain migration (old ``articles.`` subdomain -> apex) part-way through,
  so every older article has two live URLs whose traffic must be SUMMED;
* a month where the new domain was live but unmeasured (property created late);
* a second analytics property in a different timezone, exported at hour grain,
  so its hours must be re-bucketed onto the newsroom's local days;
* a frozen export from a retired analytics vendor that holds LIFETIME totals
  only, overlapping the daily era, so it must NEVER be summed with daily data;
* newsletter editions that each carry several articles, so opens must be
  counted once per edition, not once per article;
* traffic to pages that are not articles (homepage, archives, removed posts),
  which must land in a named bucket rather than vanish;
* articles whose URL category disagrees with their CMS primary category.

Usage:
    python src/generate_synthetic.py                # data/raw, today's date
    python src/generate_synthetic.py --today 2026-10-18 --articles 300 --out tmp/raw
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

LOCAL_TZ = "Africa/Lagos"
NEW_PROPERTY_TZ = "America/Los_Angeles"
OLD_HOST = "articles.harbourline.example"
NEW_HOST = "harbourline.example"

LEGACY_DAILY_START = dt.date(2023, 7, 26)   # first day the old property collected
MIGRATION_DATE = dt.date(2026, 5, 11)       # new domain goes live
NEW_PROPERTY_START = dt.date(2026, 6, 11)   # ...but is only measured from here
VENDOR_WINDOW = (dt.date(2022, 2, 7), dt.date(2026, 7, 3))  # retired vendor's lifetime export

EDITORIAL = ["news", "features", "opinion", "explainers", "data-stories", "thought-leadership"]
SECONDARY = ["jobs", "press-release", "weekly-roundup"]
# Seeded vocabulary: the last two deliberately have no articles, so a
# dashboard built on SELECT DISTINCT would silently omit them.
FOCUS_AREAS = ["climate", "health", "education", "economy", "governance",
               "technology", "culture", "agriculture", "urban-mobility", "ai-policy"]

WORDS = ("river harbour council budget school clinic market drought solar policy "
         "reform data loan bridge port vote audit water grid farm wage youth tax "
         "road rail flood heat price trade court health climate ferry").split()


def _title(rng: np.random.Generator) -> str:
    w = rng.choice(WORDS, size=rng.integers(4, 8), replace=True)
    return " ".join(w).capitalize()


def build_registry(rng: np.random.Generator, n: int, today: dt.date) -> pd.DataFrame:
    start = dt.date(2016, 1, 1)
    span = (today - start).days
    # Publishing accelerates over time, as most newsrooms' archives do.
    offsets = np.sort((rng.beta(2.2, 1.0, size=n) * span).astype(int))
    rows = []
    for i, off in enumerate(offsets):
        secondary = rng.random() < 0.18
        cat = rng.choice(SECONDARY) if secondary else rng.choice(EDITORIAL)
        # ~4% of posts carry two categories and the URL shows the other one.
        url_cat = cat
        if not secondary and rng.random() < 0.04:
            url_cat = rng.choice([c for c in EDITORIAL if c != cat])
        title = _title(rng)
        slug = f"{'-'.join(title.lower().split()[:5])}-{i:04d}"
        rows.append({
            "article_id": i + 1,
            "slug": slug,
            "title": title,
            "primary_category": cat,
            "url_category": url_cat,
            "reporting_group": "secondary" if secondary else "editorial",
            "published_date": start + dt.timedelta(days=int(off)),
            "word_count": int(rng.integers(250, 2600)),
        })
    return pd.DataFrame(rows)


def _decay_views(rng, n_days: int, base: float) -> np.ndarray:
    """Launch spike then long tail, with Poisson noise."""
    t = np.arange(n_days)
    lam = base * (0.85 ** t) + base * 0.012
    return rng.poisson(lam)


def build_daily(rng, reg: pd.DataFrame, today: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Old-property daily rows (local days) and new-property HOURLY rows (LA time)."""
    legacy_rows, hourly_rows = [], []
    # Hours between local time and the new property's clock, per day (DST-aware).
    # Computed once per day, not per hour, so generation stays fast.
    shift = {}
    for d in pd.date_range(NEW_PROPERTY_START, today, freq="D"):
        lag = d.tz_localize(LOCAL_TZ).utcoffset() - d.tz_localize(NEW_PROPERTY_TZ).utcoffset()
        shift[d.date()] = dt.timedelta(seconds=lag.total_seconds())
    for a in reg.itertuples():
        base = rng.lognormal(mean=3.3 if a.reporting_group == "editorial" else 2.0, sigma=0.9)
        first = max(a.published_date, LEGACY_DAILY_START)
        if first > today:
            continue
        # Age at first measured day, so old articles start in their tail.
        age0 = (first - a.published_date).days
        n_days = (today - first).days + 1
        views = _decay_views(rng, age0 + n_days, base)[age0:]
        read_s = rng.normal(35 + a.word_count / 22, 12)  # engaged seconds per view
        for k in np.nonzero(views)[0]:
            day = first + dt.timedelta(days=int(k))
            pv = int(views[k])
            on_new = day >= MIGRATION_DATE
            # After migration most readers hit the apex URL; the old URL keeps a share.
            old_share = 0.22 if on_new else 1.0
            pv_old = int(round(pv * old_share))
            pv_new = pv - pv_old
            if pv_old:
                legacy_rows.append((day, f"/articles/{a.url_category}/{a.slug}/", pv_old,
                                    max(0.0, read_s) * pv_old))
            if pv_new and day >= NEW_PROPERTY_START:
                # Spread the day's apex views over local hours, then express
                # each hour in the new property's own timezone.
                hours = rng.multinomial(pv_new, np.full(24, 1 / 24))
                base_dt = dt.datetime.combine(day, dt.time()) - shift[day]
                for h in np.nonzero(hours)[0]:
                    la = base_dt + dt.timedelta(hours=int(h))
                    hourly_rows.append((la, f"/{a.slug}/", int(hours[h]),
                                        max(0.0, read_s) * int(hours[h])))
            # pv_new before NEW_PROPERTY_START is the unmeasured gap: dropped on purpose.
    legacy = pd.DataFrame(legacy_rows, columns=["date", "path", "pageviews", "engaged_seconds"])
    hourly = pd.DataFrame(hourly_rows, columns=["hour_property_tz", "path", "pageviews", "engaged_seconds"])

    # Non-article traffic that must be bucketed, not lost.
    days = pd.date_range(LEGACY_DAILY_START, today, freq="D").date
    extra = []
    for d in days:
        extra.append((d, "/", int(rng.poisson(140)), float(rng.normal(20, 4) * 140)))
        for c in EDITORIAL[:3]:
            extra.append((d, f"/category/{c}/", int(rng.poisson(12)), float(12 * 25)))
        if rng.random() < 0.3:
            extra.append((d, f"/articles/news/removed-post-{int(rng.integers(1, 40)):03d}/",
                          int(rng.poisson(3)) + 1, 60.0))
    legacy = pd.concat([legacy, pd.DataFrame(extra, columns=legacy.columns)], ignore_index=True)
    legacy = legacy[legacy.pageviews > 0]
    legacy = legacy.groupby(["date", "path"], as_index=False).sum()
    hourly = hourly.groupby(["hour_property_tz", "path"], as_index=False).sum()
    return legacy, hourly


def build_vendor_lifetime(rng, reg: pd.DataFrame) -> pd.DataFrame:
    """Retired vendor: one lifetime row per article it saw. No dates, by design."""
    lo, hi = VENDOR_WINDOW
    seen = reg[(reg.published_date <= hi) & (rng.random(len(reg)) < 0.72)]
    return pd.DataFrame({
        "slug": seen.slug.values,
        "lifetime_pageviews": rng.lognormal(6.0, 1.1, len(seen)).astype(int) + 1,
        "avg_read_seconds": rng.normal(95, 30, len(seen)).clip(10).round(1),
        "window_start": lo, "window_end": hi,
    })


def build_newsletter(rng, reg: pd.DataFrame, today: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Weekly editions, each carrying 3-6 recent editorial articles."""
    editions, links = [], []
    ed = reg[reg.reporting_group == "editorial"]
    d = dt.date(2023, 1, 6)
    eid = 0
    while d <= today:
        recent = ed[(ed.published_date <= d) & (ed.published_date > d - dt.timedelta(days=10))]
        if len(recent) >= 2:
            eid += 1
            k = min(len(recent), int(rng.integers(3, 7)))
            editions.append({"edition_id": eid, "sent_date": d,
                             "subscribers": 8000 + eid * 6,
                             "unique_opens": int(rng.normal(3100 + eid * 2.5, 240))})
            for s in rng.choice(recent.slug.values, size=k, replace=False):
                links.append({"edition_id": eid, "slug": s})
        d += dt.timedelta(days=7)
    return pd.DataFrame(editions), pd.DataFrame(links)


def build_tags(rng, reg: pd.DataFrame) -> pd.DataFrame:
    """The one human-edited file. ~40% of editorial articles are still untagged."""
    ed = reg[reg.reporting_group == "editorial"]
    tagged = ed[rng.random(len(ed)) < 0.6]
    used = FOCUS_AREAS[:-2]
    return pd.DataFrame({"slug": tagged.slug.values,
                         "focus_area": rng.choice(used, size=len(tagged))})


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--articles", type=int, default=900)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="data/raw")
    args = ap.parse_args(argv)

    today = dt.date.fromisoformat(args.today)
    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    reg = build_registry(rng, args.articles, today)
    legacy, hourly = build_daily(rng, reg, today)
    vendor = build_vendor_lifetime(rng, reg)
    editions, links = build_newsletter(rng, reg, today)
    tags = build_tags(rng, reg)

    reg.to_parquet(out / "cms_articles.parquet", index=False)
    legacy.to_parquet(out / "analytics_legacy_daily.parquet", index=False)
    hourly.to_parquet(out / "analytics_new_hourly.parquet", index=False)
    vendor.to_parquet(out / "vendor_lifetime_frozen.parquet", index=False)
    editions.to_parquet(out / "newsletter_editions.parquet", index=False)
    links.to_parquet(out / "newsletter_edition_articles.parquet", index=False)
    tags.to_csv(out / "tags.csv", index=False)

    manifest = {
        "generated_for": today.isoformat(), "seed": args.seed,
        "articles": len(reg), "legacy_daily_rows": len(legacy),
        "new_hourly_rows": len(hourly), "vendor_rows": len(vendor),
        "editions": len(editions), "tagged": len(tags),
        "constants": {"migration": MIGRATION_DATE.isoformat(),
                      "new_property_start": NEW_PROPERTY_START.isoformat(),
                      "legacy_daily_start": LEGACY_DAILY_START.isoformat(),
                      "local_tz": LOCAL_TZ, "new_property_tz": NEW_PROPERTY_TZ},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    main()
