"""Combine everyone's outputs and join them back onto the source CSV.

    python -m spv_scraper.merge out_alex/ out_sam/ out_jo/ out_kim/ --source copart_modeling_ready.csv

Each argument can be a folder (searched recursively for *_results.csv / *_issues.csv) or a file.
Writes copart_with_spv.csv (every source row + SPV columns) and prints a coverage report.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from .worker import ISSUE_FIELDS, RESULT_FIELDS, _read_complete_rows

SPV_COLUMNS = [
    "spv_status", "spv_private_value", "spv_site_year", "spv_site_make", "spv_site_model",
    "spv_site_mileage", "spv_mileage_sent", "spv_mileage_trusted", "spv_queried_at_utc", "spv_issue_detail",
]
MAX_DATE_SPREAD_DAYS = 7  # TxDMV updates values weekly


def collect(paths: list[Path]) -> tuple[list[Path], list[Path]]:
    results, issues = [], []
    for p in paths:
        if p.is_dir():
            results += sorted(p.rglob("*_results.csv"))
            issues += sorted(p.rglob("*_issues.csv"))
        elif p.name.endswith("_results.csv"):
            results.append(p)
        elif p.name.endswith("_issues.csv"):
            issues.append(p)
        else:
            raise SystemExit(f"Not an output folder or *_results.csv / *_issues.csv file: {p}")
    return results, issues


def compress_ranges(nums: list[int]) -> str:
    if not nums:
        return ""
    nums = sorted(nums)
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        parts.append(f"{start:,}" if start == prev else f"{start:,}-{prev:,}")
        if n is not None:
            start = prev = n
    return ", ".join(parts)


def merge(inputs: list[Path], source: Path, out: Path) -> dict:
    result_files, issue_files = collect(inputs)
    if not result_files and not issue_files:
        raise SystemExit("No *_results.csv or *_issues.csv files found.")

    results: dict[str, dict] = {}
    dup_results = 0
    for f in result_files:
        for r in _read_complete_rows(f, RESULT_FIELDS):
            key = r["lot_id"]
            if key in results:
                dup_results += 1
                if r["queried_at_utc"] <= results[key]["queried_at_utc"]:
                    continue
            results[key] = r

    issues: dict[str, dict] = {}
    for f in issue_files:
        for r in _read_complete_rows(f, ISSUE_FIELDS):
            key = r["lot_id"]
            if key not in issues or r["at_utc"] >= issues[key]["at_utc"]:
                issues[key] = r

    status_counts: Counter = Counter()
    missing_rows: list[int] = []
    with open(source, newline="", encoding="utf-8-sig") as fin, open(out, "w", newline="", encoding="utf-8") as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=list(reader.fieldnames) + SPV_COLUMNS)
        writer.writeheader()
        for n, row in enumerate(reader, start=1):
            lot = row["lot_id"]
            extra = dict.fromkeys(SPV_COLUMNS, "")
            if lot in results:
                r = results[lot]
                extra.update(spv_status="ok", spv_private_value=r["spv_private_value"],
                             spv_site_year=r["site_year"], spv_site_make=r["site_make"],
                             spv_site_model=r["site_model"], spv_site_mileage=r["site_mileage"],
                             spv_mileage_sent=r["mileage_sent"], spv_mileage_trusted=r["mileage_trusted"],
                             spv_queried_at_utc=r["queried_at_utc"])
            elif lot in issues:
                i = issues[lot]
                extra.update(spv_status=i["reason"], spv_site_year=i["site_year"], spv_site_make=i["site_make"],
                             spv_site_model=i["site_model"], spv_mileage_sent=i["mileage_sent"],
                             spv_queried_at_utc=i["at_utc"], spv_issue_detail=i["detail"])
            else:
                extra["spv_status"] = "missing"
                missing_rows.append(n)
            status_counts[extra["spv_status"]] += 1
            writer.writerow({**row, **extra})

    dates = sorted(r["queried_at_utc"] for r in results.values() if r["queried_at_utc"])
    spread_days = None
    if dates:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        spread_days = (datetime.strptime(dates[-1], fmt) - datetime.strptime(dates[0], fmt)).days
    return {
        "result_files": len(result_files), "issue_files": len(issue_files),
        "rows": sum(status_counts.values()), "status_counts": dict(status_counts),
        "missing_rows": missing_rows, "duplicate_results": dup_results,
        "first_query": dates[0] if dates else None, "last_query": dates[-1] if dates else None,
        "spread_days": spread_days,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+", type=Path, help="output folders or *_results.csv / *_issues.csv files")
    ap.add_argument("--source", type=Path, default=Path("copart_modeling_ready.csv"))
    ap.add_argument("--out", type=Path, default=Path("copart_with_spv.csv"))
    args = ap.parse_args(argv)

    rep = merge(args.inputs, args.source, args.out)
    print(f"Read {rep['result_files']} results file(s) and {rep['issue_files']} issues file(s).")
    print(f"Wrote {rep['rows']:,} rows to {args.out}")
    for status, n in sorted(rep["status_counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {status:<22} {n:>9,}  ({n / max(1, rep['rows']):.1%})")
    if rep["duplicate_results"]:
        print(f"Note: {rep['duplicate_results']:,} vehicles were looked up more than once (kept the latest).")
    if rep["missing_rows"]:
        print(f"MISSING: {len(rep['missing_rows']):,} rows are in nobody's output: "
              f"{compress_ranges(rep['missing_rows'])[:600]}")
    if rep["first_query"]:
        print(f"Lookups ran from {rep['first_query']} to {rep['last_query']}.")
        if rep["spread_days"] is not None and rep["spread_days"] > MAX_DATE_SPREAD_DAYS:
            print(f"WARNING: lookups span {rep['spread_days']} days. TxDMV updates values weekly, "
                  "so early and late rows may come from different value tables.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
