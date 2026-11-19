import csv
import threading
import time
from pathlib import Path

from spv_scraper import client as c
from spv_scraper.pacer import Pacer
from spv_scraper.worker import ISSUE_FIELDS, RESULT_FIELDS, RunConfig, Worker, load_progress

FIELDS = ["lot_id", "vin", "year", "make", "mileage_reported", "odometer_status"]
VIN_A = "JTHBK1EGXB2421163"
VIN_B = "1C6RR6FG0FS518330"
VIN_NF = "12345678901234567"
VIN_SLOW = "4T1BE32K83U210171"

ROWS = [
    # lot_id, vin, year, make, mileage, odometer_status
    ("101", VIN_A, "2011", "LEXUS", "167218", "actual"),        # 1 ok
    ("102", "LWC04590B515", "2015", "LOWE", "0", "not_actual"),  # 2 short VIN: pre-check, no request
    ("103", VIN_B, "2015", "RAM", "0", "not_actual"),           # 3 ok, untrusted mileage
    ("104", VIN_NF, "2010", "X", "50000", "actual"),            # 4 not found
    ("105", VIN_A, "2011", "LEXUS", "1200000", "actual"),       # 5 mileage too big: pre-check
    ("106", VIN_A, "2011", "LEXUS", "167218", "actual"),        # 6 same vin+mileage as row 1: cached
    ("107", VIN_SLOW, "2003", "TOYOTA", "141671", "actual"),    # 7 times out (in some tests)
]


def write_csv(path: Path, rows=ROWS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        w.writerows(rows)
    return path


class FakeClient:
    def __init__(self, timeouts=()):
        self.calls = []
        self.timeouts = set(timeouts)

    def lookup(self, vin, mileage):
        self.calls.append((vin, mileage))
        if vin in self.timeouts:
            return c.LookupResult(status=c.TIMEOUT, detail="read timed out")
        if vin == VIN_NF:
            return c.LookupResult(status=c.NOT_FOUND, detail="VIN not found")
        return c.LookupResult(status=c.OK, spv_value=5000, year="2011", make="Make", model="Model",
                              site_mileage=str(mileage), elapsed=0.01)


def fast_pacer(**kw):
    kw = {"base_delay": 0, "floor": 0, "jitter": 0, "break_every": 0, "backoff_start": 0,
          "max_consecutive_failures": 100, **kw}
    return Pacer(**kw)


def read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run(tmp_path, client, start=1, end=len(ROWS), retry=False, pacer=None):
    events = []
    cfg = RunConfig(csv_path=tmp_path / "in.csv", start_row=start, end_row=end,
                    out_dir=tmp_path / "out", retry_failed=retry)
    w = Worker(cfg, client=client, pacer=pacer or fast_pacer(), emit=lambda k, p: events.append((k, p)))
    w.run()
    return cfg, w, events


def test_full_run_writes_results_and_issues(tmp_path):
    write_csv(tmp_path / "in.csv")
    fc = FakeClient()
    cfg, w, events = run(tmp_path, fc)

    results = read(cfg.out_dir / f"{cfg.stem}_results.csv")
    issues = read(cfg.out_dir / f"{cfg.stem}_issues.csv")
    assert [r["row_num"] for r in results] == ["1", "3", "6", "7"]
    assert {r["row_num"]: r["reason"] for r in issues} == {
        "2": c.INVALID_VIN, "4": c.NOT_FOUND, "5": c.MILEAGE_OUT_OF_RANGE}
    # pre-checked rows and the cached duplicate never hit the network
    assert fc.calls == [(VIN_A, 167218), (VIN_B, 0), (VIN_NF, 50000), (VIN_SLOW, 141671)]
    r3 = results[1]
    assert r3["mileage_trusted"] == "0" and r3["odometer_status"] == "not_actual" and r3["lot_id"] == "103"
    assert results[0]["mileage_trusted"] == "1" and results[0]["spv_private_value"] == "5000"
    finished = [p for k, p in events if k == "finished"][0]
    assert finished == {"handled": 7, "total": 7, "ok": 4, "no_value": 1, "skipped": 2, "errors": 0}
    assert (cfg.out_dir / f"{cfg.stem}_run.log").read_text(encoding="utf-8").count("row ") >= 7


def test_resume_skips_handled_rows(tmp_path):
    write_csv(tmp_path / "in.csv")
    # first session is stopped after handling rows 1-4 of the 1-7 range
    fc1 = FakeClient()
    cfg = RunConfig(csv_path=tmp_path / "in.csv", start_row=1, end_row=7, out_dir=tmp_path / "out")
    w = Worker(cfg, client=fc1, pacer=fast_pacer())
    orig = w._process

    def stop_after_four(n, row):
        if n == 5:
            w.stop()
            return False
        return orig(n, row)
    w._process = stop_after_four
    w.run()
    assert len(fc1.calls) == 3

    fc2 = FakeClient()
    cfg2, w2, events = run(tmp_path, fc2)
    # rows 1-4 skipped; 5 is a pre-check; 6 is queried (the cache doesn't outlive a session); then 7
    assert fc2.calls == [(VIN_A, 167218), (VIN_SLOW, 141671)]
    results = read(cfg2.out_dir / f"{cfg2.stem}_results.csv")
    nums = [r["row_num"] for r in results]
    assert len(nums) == len(set(nums))
    assert sorted(nums, key=int) == ["1", "3", "6", "7"]
    first_progress = [p for k, p in events if k == "progress"][0]
    assert first_progress["handled"] == 4


def test_timeouts_logged_after_three_attempts_then_retry_failed_fixes_them(tmp_path):
    write_csv(tmp_path / "in.csv")
    fc = FakeClient(timeouts={VIN_SLOW})
    cfg, w, _ = run(tmp_path, fc)
    assert fc.calls.count((VIN_SLOW, 141671)) == 3
    issues = read(cfg.out_dir / f"{cfg.stem}_issues.csv")
    assert issues[-1]["row_num"] == "7" and issues[-1]["reason"] == c.TIMEOUT and issues[-1]["attempts"] == "3"

    fc2 = FakeClient()
    cfg2, w2, events = run(tmp_path, fc2, retry=True)
    assert fc2.calls == [(VIN_SLOW, 141671)]
    done, open_issues = load_progress(cfg2.out_dir / f"{cfg2.stem}_results.csv",
                                      cfg2.out_dir / f"{cfg2.stem}_issues.csv", 1, 7)
    assert 7 in done and 7 not in open_issues
    assert set(open_issues.values()) == {c.INVALID_VIN, c.NOT_FOUND, c.MILEAGE_OUT_OF_RANGE}


def test_auto_pause_after_consecutive_failures_then_stop(tmp_path):
    write_csv(tmp_path / "in.csv", [ROWS[6]])
    fc = FakeClient(timeouts={VIN_SLOW})
    events = []
    cfg = RunConfig(csv_path=tmp_path / "in.csv", start_row=1, end_row=1, out_dir=tmp_path / "out")
    w = Worker(cfg, client=fc, pacer=fast_pacer(max_consecutive_failures=2), emit=lambda k, p: events.append((k, p)))
    t = threading.Thread(target=w.run)
    t.start()
    deadline = time.time() + 5
    while not w.paused and time.time() < deadline:
        time.sleep(0.01)
    assert w.paused, "worker should auto-pause"
    assert len(fc.calls) == 2
    assert any(k == "status" and p[1] == "error" and "Auto-paused" in p[0] for k, p in events)
    w.stop()
    t.join(5)
    assert not t.is_alive()
    # the row was never given up on, so it's not written anywhere: the next Start retries it
    assert read(cfg.out_dir / f"{cfg.stem}_issues.csv") == []
    assert read(cfg.out_dir / f"{cfg.stem}_results.csv") == []


def test_resume_after_crash_mid_line(tmp_path):
    write_csv(tmp_path / "in.csv")
    out = tmp_path / "out"
    out.mkdir()
    stem = "spv_000001-000007"
    with open(out / f"{stem}_results.csv", "w", newline="", encoding="utf-8") as f:
        f.write(",".join(RESULT_FIELDS) + "\r\n")
        f.write(f"1,101,{VIN_A},167218,actual,1,5000,2011,M,M,\"167,218\",2026-11-19T00:00:00Z\r\n")
        f.write("3,103,1C6RR6")  # crash mid-write, no newline
    fc = FakeClient()
    cfg, w, _ = run(tmp_path, fc)
    assert (VIN_B, 0) in fc.calls  # row 3 re-queried
    assert fc.calls.count((VIN_A, 167218)) == 1  # only row 6; row 1 was already done
    results = read(out / f"{stem}_results.csv")
    complete = [r for r in results if all(r.get(k) is not None for k in RESULT_FIELDS)]
    assert sorted({r["row_num"] for r in complete}, key=int) == ["1", "3", "6", "7"]


def test_issue_fields_header_written_once(tmp_path):
    write_csv(tmp_path / "in.csv")
    run(tmp_path, FakeClient())
    run(tmp_path, FakeClient())
    text = (tmp_path / "out" / "spv_000001-000007_issues.csv").read_text(encoding="utf-8")
    assert text.count(ISSUE_FIELDS[0] + "," + ISSUE_FIELDS[1]) == 1
