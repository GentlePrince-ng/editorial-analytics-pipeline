"""Daily health check over the built database.

Prints a Markdown digest (and appends it to the GitHub Actions step summary
when run there). Exits non-zero when anything needs a human, so a red run
is the alarm. A silent green run is not proof of health on its own: the
digest is produced every day, so a missing digest is noticed too.

    python src/diagnostics.py [--db data/analytics.duckdb] [--today 2026-10-18] [--max-lag 3]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent


def check(db: Path, today: dt.date, max_lag: int) -> tuple[list[str], list[str]]:
    problems, lines = [], []
    if not db.exists():
        return [f"database missing at {db}"], ["**Database missing.** The build failed or never ran."]
    con = duckdb.connect(str(db), read_only=True)
    report = json.loads(con.execute("SELECT report_json FROM build_report").fetchone()[0])

    lines.append(f"Build: {report['built_at']} ({report['seconds']}s)")
    for a in report["assertions"]:
        lines.append(f"- {'✅' if a['ok'] else '❌'} {a['name']}: {a['detail']}")
        if not a["ok"]:
            problems.append(f"assertion failed: {a['name']}")

    lines.append("")
    lines.append("| Source | Last day | Days behind |")
    lines.append("|---|---|---|")
    for f in report["freshness"]:
        last = dt.date.fromisoformat(f["last_date"])
        lag = (today - last).days
        flag = " ⚠️" if lag > max_lag else ""
        lines.append(f"| {f['source']} | {last} | {lag}{flag} |")
        if lag > max_lag:
            problems.append(f"{f['source']} is {lag} days stale (limit {max_lag})")

    top = con.execute("""SELECT title, daily_pageviews FROM v_article_traffic
                         WHERE reporting_group='editorial' ORDER BY daily_pageviews DESC LIMIT 3""").fetchall()
    lines.append("")
    lines.append("Top articles: " + "; ".join(f"{t} ({v:,})" for t, v in top))
    con.close()
    return problems, lines


def main(argv=None) -> int:
    # Windows consoles default to cp1252; the digest uses emoji and dashes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "analytics.duckdb"))
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--max-lag", type=int, default=3)
    a = ap.parse_args(argv)
    problems, lines = check(Path(a.db), dt.date.fromisoformat(a.today), a.max_lag)

    status = "🟢 Healthy" if not problems else f"🔴 {len(problems)} problem(s)"
    digest = "\n".join([f"## Daily data health: {status}", ""] + lines +
                       ([""] + [f"**Needs attention:** {p}" for p in problems] if problems else []))
    print(digest)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(digest + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
