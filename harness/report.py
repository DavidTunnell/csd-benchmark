"""Aggregate one or more results CSVs into a markdown report.

Decision (recorded in README): a deterministic Python script is the canonical
report generator, not a notebook. Scott reads markdown. The CLI is:

    python -m harness.report results/run-20260504T*.csv \\
        --out results/report-2026-05-04.md

Aggregations per (tool, scenario, cache_state):
  - count of runs
  - median time_to_result_sec
  - p95 time_to_result_sec
  - median click_count
  - median http_request_count
  - median network_bytes
  - completion rate (within 5-minute cap)
  - non-technical-could-complete rate
  - result_correct rate

Discarded runs (notes containing 'discarded' case-insensitive) are excluded
from aggregation but listed at the bottom of the report for transparency.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .common import get_logger
from .results import REPORT_COLUMNS

log = get_logger("report")


def _parse_float(s: str) -> Optional[float]:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(s: str) -> Optional[int]:
    if s is None or s == "":
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _parse_bool(s: str) -> Optional[bool]:
    if s is None or s == "":
        return None
    return s.lower() == "true"


def _percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[int(k)]
    return s[f] + (s[c] - s[f]) * (k - f)


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        if v >= 100:
            return f"{v:.0f}"
        if v >= 10:
            return f"{v:.1f}"
        return f"{v:.2f}"
    return str(v)


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a"
    pct = 100 * numerator / denominator
    return f"{numerator}/{denominator} ({pct:.0f}%)"


def load_rows(paths: list[Path]) -> list[dict]:
    rows: list[dict] = []
    for p in paths:
        if not p.is_file():
            log.warning("not a file, skipping: %s", p)
            continue
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for raw in reader:
                rows.append(raw)
    log.info("loaded %d rows from %d files", len(rows), len(paths))
    return rows


def aggregate(rows: list[dict]) -> dict:
    """Group rows by (tool, scenario_id, cache_state) and compute stats."""
    groups: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
    discarded: list[dict] = []

    for row in rows:
        if "discarded" in (row.get("notes") or "").lower():
            discarded.append(row)
            continue
        tool = row.get("tool") or "?"
        sid_raw = row.get("scenario_id") or "0"
        try:
            sid = int(sid_raw)
        except ValueError:
            continue
        cache = row.get("cache_state") or "?"
        groups[(tool, sid, cache)].append(row)

    return {"groups": groups, "discarded": discarded}


def render_markdown(agg: dict, out_path: Path, sources: list[Path]) -> None:
    groups = agg["groups"]
    discarded = agg["discarded"]
    lines: list[str] = []
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append("# csd-benchmark report")
    lines.append("")
    lines.append(f"Generated {now}")
    lines.append("")
    lines.append("Source files:")
    for s in sources:
        lines.append(f"- `{s}`")
    lines.append("")
    lines.append("Run conditions: see [csd-benchmark/RUN_CONDITIONS.md](https://bitbucket.org/cloudsee-drive/csd-benchmark/src/main/RUN_CONDITIONS.md).")
    lines.append("")
    lines.append("## Headline metric")
    lines.append("")
    lines.append("Scenario 3 median time-to-result, cold cache. Spec:")
    lines.append("> Find files by partial filename across the whole bucket. Substring `test`. ~32,763 matches.")
    lines.append("")

    headline_rows = []
    for (tool, sid, cache), runs in groups.items():
        if sid == 3 and cache == "cold":
            times = [t for t in (_parse_float(r.get("time_to_result_sec", "")) for r in runs) if t is not None]
            headline_rows.append((tool, len(runs), _percentile(times, 50), _percentile(times, 95)))
    if headline_rows:
        lines.append("| Tool | N | Median (s) | p95 (s) |")
        lines.append("| :---- | ----: | ----: | ----: |")
        for tool, n, med, p95 in sorted(headline_rows):
            lines.append(f"| {tool} | {n} | {_fmt(med)} | {_fmt(p95)} |")
    else:
        lines.append("_No scenario-3 cold-cache runs in the supplied data._")
    lines.append("")

    lines.append("## Per-scenario detail")
    lines.append("")
    for sid in sorted({k[1] for k in groups.keys()}):
        lines.append(f"### Scenario {sid}")
        lines.append("")
        lines.append("| Tool | Cache | N | Median t (s) | p95 t (s) | Median clicks | Median HTTP | Within cap | Non-tech ok | Correct |")
        lines.append("| :---- | :---- | ----: | ----: | ----: | ----: | ----: | :---- | :---- | :---- |")
        for (tool, group_sid, cache), runs in sorted(groups.items()):
            if group_sid != sid:
                continue
            times = [t for t in (_parse_float(r.get("time_to_result_sec", "")) for r in runs) if t is not None]
            clicks = [c for c in (_parse_int(r.get("click_count", "")) for r in runs) if c is not None]
            https = [c for c in (_parse_int(r.get("http_request_count", "")) for r in runs) if c is not None]
            within = sum(1 for r in runs if _parse_bool(r.get("completed_within_cap", "")))
            nontech = sum(1 for r in runs if _parse_bool(r.get("non_technical_user_could_complete", "")))
            correct = sum(1 for r in runs if _parse_bool(r.get("result_correct", "")))
            lines.append(
                f"| {tool} | {cache} | {len(runs)} | "
                f"{_fmt(_percentile(times, 50))} | "
                f"{_fmt(_percentile(times, 95))} | "
                f"{_fmt(statistics.median(clicks)) if clicks else '—'} | "
                f"{_fmt(statistics.median(https)) if https else '—'} | "
                f"{_rate(within, len(runs))} | "
                f"{_rate(nontech, len(runs))} | "
                f"{_rate(correct, len(runs))} |"
            )
        lines.append("")

    if discarded:
        lines.append("## Discarded runs")
        lines.append("")
        lines.append(f"_{len(discarded)} runs excluded from aggregation; listed for transparency._")
        lines.append("")
        lines.append("| run_id | tool | scenario | cache | notes |")
        lines.append("| :---- | :---- | :---- | :---- | :---- |")
        for r in discarded[:50]:
            lines.append(
                f"| {r.get('run_id','')} | {r.get('tool','')} | "
                f"{r.get('scenario_name','')} | {r.get('cache_state','')} | "
                f"{(r.get('notes','') or '').replace('|','/')} |"
            )
        if len(discarded) > 50:
            lines.append(f"| ... | ... | ... | ... | _(+{len(discarded) - 50} more)_ |")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote report: %s (%d bytes)", out_path, out_path.stat().st_size)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", help="one or more results CSV files (globs ok)")
    p.add_argument("--out", default=None, help="output markdown path (default: results/report-<utc>.md)")
    args = p.parse_args()

    paths: list[Path] = []
    for inp in args.inputs:
        if any(c in inp for c in "*?["):
            paths.extend(sorted(Path().glob(inp)))
        else:
            paths.append(Path(inp))
    if not paths:
        log.error("no input files matched")
        return 2

    rows = load_rows(paths)
    if not rows:
        log.error("no rows found in inputs")
        return 2

    agg = aggregate(rows)

    out_path = (
        Path(args.out)
        if args.out
        else Path("results") / f"report-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.md"
    )
    render_markdown(agg, out_path, paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
