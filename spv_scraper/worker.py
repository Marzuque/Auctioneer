"""The run loop: reads a row range from the Copart CSV, looks each VIN up, writes results.

Outputs (in the chosen folder, named by row range, e.g. spv_000001-063153_*):
  *_results.csv  one line per vehicle the site priced
  *_issues.csv   one line per row that was skipped or failed, with a reason
  *_run.log      everything shown in the live log
  raw/           HTML of pages we couldn't understand (for debugging)

Every line is flushed as soon as it's written, so the app can be closed or crash at any time.
Starting the same range again skips rows already in results or issues ("resume").
"Retry failed" re-runs only rows whose latest issue is retriable (network/blocked/unknown page).
"""
from __future__ import annotations

import csv
import re
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import client as c
from .pacer import Pacer

RESULT_FIELDS = [
    "row_num", "lot_id", "vin", "mileage_sent", "odometer_status", "mileage_trusted",
    "spv_private_value", "site_year", "site_make", "site_model", "site_mileage", "queried_at_utc",
]
ISSUE_FIELDS = [
    "row_num", "lot_id", "vin", "mileage_sent", "odometer_status", "reason", "detail",
    "site_year", "site_make", "site_model", "attempts", "at_utc",
]

MAX_ATTEMPTS_PER_ROW = 3          # transient failures on one row before it's logged and skipped
MAX_CONSECUTIVE_ANOMALIES = 20    # unknown pages in a row before auto-pause (site may have changed)
MAX_MILEAGE = 999_999             # the form accepts 6 digits
VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

SKIPPED_REASONS = {c.INVALID_VIN, c.MILEAGE_OUT_OF_RANGE}
NO_VALUE_REASONS = {c.NO_VALUE, c.NOT_FOUND}


@dataclass
class RunConfig:
    csv_path: Path
    start_row: int                  # 1-based data row (row 1 = first vehicle under the header)
    end_row: int                    # inclusive
    out_dir: Path
    base_delay: float = 1.0
    retry_failed: bool = False

    @property
    def stem(self) -> str:
        return f"spv_{self.start_row:06d}-{self.end_row:06d}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_mileage(raw: str | None) -> int | None:
    try:
        v = int(float((raw or "").strip()))
    except ValueError:
        return None
    return v if v >= 0 else None


def precheck(vin: str, mileage: int | None) -> tuple[str, str] | None:
    """Rows we know the site will reject. Returns (reason, detail) or None if OK to send."""
    if not VIN_RE.fullmatch(vin):
        return c.INVALID_VIN, f"not a 17-character VIN ({len(vin)} chars)"
    if mileage is not None and mileage > MAX_MILEAGE:
        return c.MILEAGE_OUT_OF_RANGE, f"{mileage:,} miles exceeds the form's 6 digits"
    return None


def count_data_rows(csv_path: Path) -> int:
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)
        return sum(1 for _ in reader)


class _Appender:
    """Append-only CSV writer that survives a crash mid-line."""

    def __init__(self, path: Path, fields: list[str]):
        self.path = path
        new = not path.exists() or path.stat().st_size == 0
        if not new:
            with open(path, "rb") as fb:
                fb.seek(-1, 2)
                needs_newline = fb.read(1) not in (b"\n", b"\r")
        self.f = open(path, "a", newline="", encoding="utf-8")
        if not new and needs_newline:
            self.f.write("\r\n")  # close off a half-written line so the next row starts clean
        self.w = csv.DictWriter(self.f, fieldnames=fields, extrasaction="ignore")
        if new:
            self.w.writeheader()
        self.f.flush()

    def write(self, row: dict) -> None:
        self.w.writerow(row)
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def _read_complete_rows(path: Path, fields: list[str]):
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if None in row or any(row.get(k) is None for k in fields):
                continue  # half-written line from a crash
            try:
                row["row_num"] = int(row["row_num"])
            except (TypeError, ValueError):
                continue
            yield row


def load_progress(results_path: Path, issues_path: Path, start: int, end: int):
    """Returns (rows_with_result, {row_num: latest issue reason}) within [start, end]."""
    done = {r["row_num"] for r in _read_complete_rows(results_path, RESULT_FIELDS) if start <= r["row_num"] <= end}
    issues: dict[int, str] = {}
    for r in _read_complete_rows(issues_path, ISSUE_FIELDS):
        if start <= r["row_num"] <= end:
            issues[r["row_num"]] = r["reason"]
    for n in done:
        issues.pop(n, None)  # a later successful retry wins over an earlier issue
    return done, issues


Emit = Callable[[str, object], None]


class Worker:
    def __init__(self, config: RunConfig, client: c.SpvClient | None = None, pacer: Pacer | None = None,
                 emit: Emit | None = None):
        self.cfg = config
        self.client = client or c.SpvClient()
        self.pacer = pacer or Pacer(base_delay=config.base_delay)
        self.emit = emit or (lambda kind, payload: None)
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()   # set = paused
        self.stats = {"handled": 0, "total": 0, "ok": 0, "no_value": 0, "skipped": 0, "errors": 0}
        self._row_times: deque[float] = deque(maxlen=50)
        self._remaining = 0
        self._anomalies = 0
        self._cache: dict[tuple[str, int | None], c.LookupResult] = {}
        self._log_f = None
        self._status_level = "info"

    # ---- controls (called from the UI thread) -------------------------------
    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def stop(self) -> None:
        self.stop_event.set()

    @property
    def paused(self) -> bool:
        return self.pause_event.is_set()

    # ---- helpers -------------------------------------------------------------
    def _log(self, msg: str) -> None:
        line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
        if self._log_f:
            self._log_f.write(line + "\n")
            self._log_f.flush()
        self.emit("log", line)

    def _status(self, text: str, level: str = "info") -> None:
        self._status_level = level
        self.emit("status", (text, level))
        if level in ("warn", "error"):
            self._log(f"[{level.upper()}] {text}")

    def _progress(self) -> None:
        p = dict(self.stats)
        if len(self._row_times) >= 2:
            span = self._row_times[-1] - self._row_times[0]
            per_row = span / (len(self._row_times) - 1)
            p["rate"] = 1 / per_row if per_row > 0 else 0.0
            p["eta"] = per_row * self._remaining
        else:
            p["rate"], p["eta"] = 0.0, None
        p["delay"] = self.pacer.base_delay * self.pacer.slowdown
        self.emit("progress", p)

    def _wait(self, seconds: float) -> bool:
        """Sleep, waking early on Stop or Pause. Returns False if interrupted."""
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return True
            if self.stop_event.wait(min(0.25, left)) or self.pause_event.is_set():
                return False

    def _hold_while_paused(self) -> bool:
        """Blocks while paused. Returns False if Stop was pressed."""
        if not self.pause_event.is_set():
            return not self.stop_event.is_set()
        self._status("Paused. Press Resume to continue.", "warn")
        self.emit("paused", True)
        while self.pause_event.is_set():
            if self.stop_event.wait(0.25):
                return False
        self.pacer.reset_failures()
        self._anomalies = 0
        self.emit("paused", False)
        self._status("Running")
        return not self.stop_event.is_set()

    def _auto_pause(self, why: str) -> None:
        self.pause()
        self._status(f"Auto-paused: {why}. Wait a while (30+ min if blocked), then press Resume.", "error")

    # ---- writing -------------------------------------------------------------
    def _base(self, n: int, row: dict, vin: str, mileage: int | None) -> dict:
        return {"row_num": n, "lot_id": row.get("lot_id", ""), "vin": vin,
                "mileage_sent": "" if mileage is None else mileage,
                "odometer_status": row.get("odometer_status", "")}

    def _write_result(self, n, row, vin, mileage, res: c.LookupResult, cached=False) -> None:
        trusted = row.get("odometer_status") == "actual" and bool(mileage)
        self._results.write({**self._base(n, row, vin, mileage),
                             "mileage_trusted": int(trusted), "spv_private_value": res.spv_value,
                             "site_year": res.year, "site_make": res.make, "site_model": res.model,
                             "site_mileage": res.site_mileage, "queried_at_utc": utc_now()})
        self.stats["ok"] += 1
        tag = " (cached)" if cached else ""
        self._log(f"row {n:>6}  {vin}  {res.year} {res.make} {res.model}  ${res.spv_value:,}{tag}")

    def _write_issue(self, n, row, vin, mileage, reason, detail, attempts, res: c.LookupResult | None = None):
        self._issues.write({**self._base(n, row, vin, mileage), "reason": reason, "detail": detail,
                            "site_year": res.year if res else "", "site_make": res.make if res else "",
                            "site_model": res.model if res else "", "attempts": attempts, "at_utc": utc_now()})
        if reason in SKIPPED_REASONS:
            self.stats["skipped"] += 1
        elif reason in NO_VALUE_REASONS:
            self.stats["no_value"] += 1
        else:
            self.stats["errors"] += 1
        self._log(f"row {n:>6}  {vin or '(blank)'}  {reason}: {detail}  -> logged")

    def _save_raw(self, n: int, html: str | None) -> None:
        if html:
            raw_dir = self.cfg.out_dir / "raw"
            raw_dir.mkdir(exist_ok=True)
            (raw_dir / f"row_{n:06d}.html").write_text(html, encoding="utf-8")

    # ---- main loop -------------------------------------------------------------
    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # never die silently in a background thread
            self._status(f"Crashed: {e}", "error")
            self._log(traceback.format_exc())
        finally:
            for f in ("_results", "_issues"):
                if hasattr(self, f):
                    getattr(self, f).close()
            self.emit("finished", dict(self.stats))
            if self._log_f:
                self._log_f.close()
                self._log_f = None

    def _run(self) -> None:
        cfg = self.cfg
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        results_path = cfg.out_dir / f"{cfg.stem}_results.csv"
        issues_path = cfg.out_dir / f"{cfg.stem}_issues.csv"
        self._log_f = open(cfg.out_dir / f"{cfg.stem}_run.log", "a", encoding="utf-8")

        done, issues = load_progress(results_path, issues_path, cfg.start_row, cfg.end_row)
        retriable = {n for n, reason in issues.items() if reason in c.RETRIABLE}

        if cfg.retry_failed:
            todo = retriable
            self.stats["total"] = len(todo)
            self._log(f"=== Retry failed: {len(todo):,} rows in {cfg.start_row:,}-{cfg.end_row:,} ===")
        else:
            self.stats["total"] = cfg.end_row - cfg.start_row + 1
            self.stats["handled"] = len(done) + len(issues)
            self.stats["ok"] = len(done)
            for reason in issues.values():
                key = ("skipped" if reason in SKIPPED_REASONS else
                       "no_value" if reason in NO_VALUE_REASONS else "errors")
                self.stats[key] += 1
            todo = None  # everything in range not already handled
            self._log(f"=== Run rows {cfg.start_row:,}-{cfg.end_row:,} -> {cfg.out_dir} "
                      f"(already done: {self.stats['handled']:,}) ===")
        self._remaining = self.stats["total"] - self.stats["handled"]
        self._results = _Appender(results_path, RESULT_FIELDS)
        self._issues = _Appender(issues_path, ISSUE_FIELDS)
        self._status("Running")
        self._progress()

        with open(cfg.csv_path, newline="", encoding="utf-8-sig") as f:
            for n, row in enumerate(csv.DictReader(f), start=1):
                if n < cfg.start_row:
                    continue
                if n > cfg.end_row:
                    break
                if todo is not None:
                    if n not in todo:
                        continue
                elif n in done or n in issues:
                    continue
                if not self._hold_while_paused():
                    break
                if not self._process(n, row):
                    break
                self.stats["handled"] += 1
                self._remaining -= 1
                self._row_times.append(time.monotonic())
                self._progress()

        if self.stop_event.is_set():
            self._status("Stopped. Start again with the same rows to resume.", "warn")
        else:
            self._status("Finished" + (" (some rows need Retry failed)" if self.stats["errors"] else ""), "ok")

    def _process(self, n: int, row: dict) -> bool:
        """Handle one row. Returns False if Stop was pressed before it finished."""
        vin = (row.get("vin") or "").strip().upper()
        mileage = parse_mileage(row.get("mileage_reported"))

        pre = precheck(vin, mileage)
        if pre:
            self._write_issue(n, row, vin, mileage, pre[0], pre[1], attempts=0)
            return True

        key = (vin, mileage)
        if key in self._cache:
            res = self._cache[key]
            if res.status == c.OK:
                self._write_result(n, row, vin, mileage, res, cached=True)
            else:
                self._write_issue(n, row, vin, mileage, res.status, res.detail + " (cached)", 0, res)
            return True

        attempts = 0
        while True:
            if not self._hold_while_paused():
                return False
            delay, why = self.pacer.next_delay()
            if why:
                self._status(f"Waiting {delay:.0f}s: {why}", "warn" if "slow" in why else "info")
            if not self._wait(delay):
                if self.stop_event.is_set():
                    return False
                continue  # paused mid-wait: loop back and hold

            res = self.client.lookup(vin, "" if mileage is None else mileage)
            attempts += 1

            if res.status in c.TRANSIENT:
                backoff = self.pacer.record_failure(res.retry_after)
                give_up = attempts >= MAX_ATTEMPTS_PER_ROW
                if give_up:
                    self._save_raw(n, res.raw_html)
                    self._write_issue(n, row, vin, mileage, res.status, res.detail, attempts, res)
                if self.pacer.tripped:
                    self._auto_pause(f"{self.pacer.consecutive_failures} failures in a row (last: {res.detail})")
                    if give_up:
                        return True
                    attempts = 0
                    continue
                self._status(f"Backing off {backoff:.0f}s after {res.status} ({res.detail})", "warn")
                if not self._wait(backoff) and self.stop_event.is_set():
                    return give_up  # the issue line (if any) is already written
                if give_up:
                    return True
                continue

            self.pacer.record_success(res.elapsed)
            if self._status_level != "info":
                self._status("Running")  # clear a backoff / break message
            if res.status == c.OK:
                self._anomalies = 0
                self._write_result(n, row, vin, mileage, res)
                self._cache[key] = res
            elif res.status in c.FINAL:
                self._anomalies = 0
                self._write_issue(n, row, vin, mileage, res.status, res.detail, attempts, res)
                self._cache[key] = res
            else:  # SITE_ERROR / PARSE_ERROR: log it, keep the page, move on
                self._save_raw(n, res.raw_html)
                self._write_issue(n, row, vin, mileage, res.status, res.detail, attempts, res)
                self._anomalies += 1
                if self._anomalies >= MAX_CONSECUTIVE_ANOMALIES:
                    self._auto_pause(f"{self._anomalies} unrecognised pages in a row; the site may have changed "
                                     f"(see the raw/ folder)")
            return True
