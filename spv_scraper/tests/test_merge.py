import csv

from spv_scraper.merge import SPV_COLUMNS, compress_ranges, merge
from spv_scraper.worker import ISSUE_FIELDS, RESULT_FIELDS


def write(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


def result(n, lot, value, when="2026-11-20T10:00:00Z"):
    return {"row_num": n, "lot_id": lot, "vin": f"VIN{n}", "mileage_sent": 1000, "odometer_status": "actual",
            "mileage_trusted": 1, "spv_private_value": value, "site_year": "2015", "site_make": "Ford",
            "site_model": "F150", "site_mileage": "1,000", "queried_at_utc": when}


def issue(n, lot, reason, when="2026-11-20T10:00:00Z"):
    return {"row_num": n, "lot_id": lot, "vin": f"VIN{n}", "mileage_sent": 1000, "odometer_status": "actual",
            "reason": reason, "detail": reason, "attempts": 1, "at_utc": when}


def test_merge_two_members(tmp_path):
    src = tmp_path / "source.csv"
    write(src, ["lot_id", "vin", "year"], [{"lot_id": str(100 + i), "vin": f"VIN{i}", "year": "2015"}
                                           for i in range(1, 9)])
    # member A did rows 1-4, member B did rows 5-7 (row 8 never run)
    write(tmp_path / "a" / "spv_000001-000004_results.csv", RESULT_FIELDS,
          [result(1, "101", 5000), result(2, "102", 6000)])
    write(tmp_path / "a" / "spv_000001-000004_issues.csv", ISSUE_FIELDS,
          [issue(3, "103", "not_found"), issue(4, "104", "timeout")])
    write(tmp_path / "b" / "spv_000005-000007_results.csv", RESULT_FIELDS,
          [result(5, "105", 7000, "2026-11-30T10:00:00Z"), result(6, "106", 8000),
           result(4, "104", 4000, "2026-11-21T10:00:00Z")])  # row 4 fixed later by a retry
    write(tmp_path / "b" / "spv_000005-000007_issues.csv", ISSUE_FIELDS, [issue(7, "107", "invalid_vin")])

    out = tmp_path / "merged.csv"
    rep = merge([tmp_path / "a", tmp_path / "b"], src, out)

    rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
    assert len(rows) == 8
    assert list(rows[0].keys()) == ["lot_id", "vin", "year"] + SPV_COLUMNS
    by_lot = {r["lot_id"]: r for r in rows}
    assert by_lot["101"]["spv_status"] == "ok" and by_lot["101"]["spv_private_value"] == "5000"
    assert by_lot["103"]["spv_status"] == "not_found"
    assert by_lot["104"]["spv_status"] == "ok" and by_lot["104"]["spv_private_value"] == "4000"
    assert by_lot["107"]["spv_status"] == "invalid_vin"
    assert by_lot["108"]["spv_status"] == "missing"
    assert rep["missing_rows"] == [8]
    assert rep["status_counts"] == {"ok": 5, "not_found": 1, "invalid_vin": 1, "missing": 1}
    assert rep["spread_days"] == 10


def test_compress_ranges():
    assert compress_ranges([5, 1, 2, 3, 9, 10]) == "1-3, 5, 9-10"
    assert compress_ranges([]) == ""
