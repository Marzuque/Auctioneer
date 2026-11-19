"""Tkinter window for running SPV lookups over a row range of the Copart CSV.

Run from the repo root:   python -m spv_scraper
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .worker import RunConfig, Worker, count_data_rows

SETTINGS = Path.home() / ".spv_scraper.json"
DEFAULT_CSV = Path.cwd() / "copart_modeling_ready.csv"
TEAM_SIZE = 4
MIN_DELAY = 0.75
MAX_LOG_LINES = 2000
COLORS = {"info": "#1f2937", "ok": "#15803d", "warn": "#b45309", "error": "#b91c1c"}


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s"


def split_ranges(total: int, parts: int) -> list[tuple[int, int]]:
    base, extra = divmod(total, parts)
    out, start = [], 1
    for i in range(parts):
        size = base + (1 if i >= parts - extra else 0)  # any remainder goes to the last members
        out.append((start, start + size - 1))
        start += size
    return out


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("TxDMV SPV Lookup")
        self.minsize(720, 560)
        self.events: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.thread: threading.Thread | None = None
        self.total_rows: int | None = None

        s = self._load_settings()
        self.csv_var = tk.StringVar(value=s.get("csv", str(DEFAULT_CSV) if DEFAULT_CSV.exists() else ""))
        self.start_var = tk.StringVar(value=s.get("start", "1"))
        self.end_var = tk.StringVar(value=s.get("end", ""))
        self.out_var = tk.StringVar(value=s.get("out", str(Path.cwd() / "spv_out")))
        self.delay_var = tk.StringVar(value=s.get("delay", "1.0"))
        self.member_var = tk.StringVar(value="")
        self.rows_info = tk.StringVar(value="")
        self.progress_text = tk.StringVar(value="Not started")
        self.counts_text = tk.StringVar(value="")
        self.rate_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="Ready")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)
        if self.csv_var.get():
            self._count_rows()

    # ---- layout ----------------------------------------------------------------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Input CSV").grid(row=0, column=0, sticky="w", **pad)
        self.csv_entry = ttk.Entry(frm, textvariable=self.csv_var)
        self.csv_entry.grid(row=0, column=1, sticky="ew", **pad)
        self.csv_btn = ttk.Button(frm, text="Browse...", command=self._pick_csv)
        self.csv_btn.grid(row=0, column=2, **pad)
        ttk.Label(frm, textvariable=self.rows_info, foreground="#4b5563").grid(row=1, column=1, sticky="w", padx=8)

        rng = ttk.Frame(frm)
        rng.grid(row=2, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(frm, text="Rows").grid(row=2, column=0, sticky="w", **pad)
        ttk.Label(rng, text="from").pack(side="left")
        self.start_entry = ttk.Entry(rng, textvariable=self.start_var, width=9)
        self.start_entry.pack(side="left", padx=4)
        ttk.Label(rng, text="to").pack(side="left")
        self.end_entry = ttk.Entry(rng, textvariable=self.end_var, width=9)
        self.end_entry.pack(side="left", padx=4)
        ttk.Label(rng, text="   Quick split:").pack(side="left")
        self.member_box = ttk.Combobox(rng, textvariable=self.member_var, state="readonly", width=16,
                                       values=[f"Member {i} of {TEAM_SIZE}" for i in range(1, TEAM_SIZE + 1)])
        self.member_box.pack(side="left", padx=4)
        self.member_box.bind("<<ComboboxSelected>>", self._apply_split)

        ttk.Label(frm, text="Output folder").grid(row=3, column=0, sticky="w", **pad)
        self.out_entry = ttk.Entry(frm, textvariable=self.out_var)
        self.out_entry.grid(row=3, column=1, sticky="ew", **pad)
        self.out_btn = ttk.Button(frm, text="Browse...", command=self._pick_out)
        self.out_btn.grid(row=3, column=2, **pad)

        dl = ttk.Frame(frm)
        dl.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        ttk.Label(frm, text="Delay (s)").grid(row=4, column=0, sticky="w", **pad)
        self.delay_entry = ttk.Entry(dl, textvariable=self.delay_var, width=6)
        self.delay_entry.pack(side="left")
        ttk.Label(dl, text=f"between lookups, plus random jitter (minimum {MIN_DELAY}). "
                           "Backs off on its own if the site pushes back.",
                  foreground="#4b5563").pack(side="left", padx=6)

        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4), padx=8)
        self.start_btn = ttk.Button(btns, text="Start", command=lambda: self._start(retry=False))
        self.pause_btn = ttk.Button(btns, text="Pause", command=self._toggle_pause, state="disabled")
        self.stop_btn = ttk.Button(btns, text="Stop", command=self._stop, state="disabled")
        self.retry_btn = ttk.Button(btns, text="Retry failed", command=lambda: self._start(retry=True))
        self.open_btn = ttk.Button(btns, text="Open output folder", command=self._open_out)
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.retry_btn, self.open_btn):
            b.pack(side="left", padx=(0, 6))

        self.bar = ttk.Progressbar(frm, mode="determinate", maximum=1)
        self.bar.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=(8, 2))
        ttk.Label(frm, textvariable=self.progress_text, font=("Segoe UI", 11, "bold")).grid(
            row=7, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Label(frm, textvariable=self.counts_text).grid(row=8, column=0, columnspan=3, sticky="w", padx=8)
        ttk.Label(frm, textvariable=self.rate_text, foreground="#4b5563").grid(
            row=9, column=0, columnspan=3, sticky="w", padx=8)
        self.status_label = ttk.Label(frm, textvariable=self.status_text, wraplength=680)
        self.status_label.grid(row=10, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 6))

        self.log = ScrolledText(frm, height=14, font=("Consolas", 9), state="disabled", wrap="none")
        self.log.grid(row=11, column=0, columnspan=3, sticky="nsew", padx=8, pady=(0, 4))
        frm.rowconfigure(11, weight=1)

        self.inputs = [self.csv_entry, self.csv_btn, self.start_entry, self.end_entry, self.member_box,
                       self.out_entry, self.out_btn, self.delay_entry]

    # ---- settings -----------------------------------------------------------------
    def _load_settings(self) -> dict:
        try:
            return json.loads(SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_settings(self):
        data = {"csv": self.csv_var.get(), "start": self.start_var.get(), "end": self.end_var.get(),
                "out": self.out_var.get(), "delay": self.delay_var.get()}
        try:
            SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- input handlers -----------------------------------------------------------
    def _pick_csv(self):
        path = filedialog.askopenfilename(title="Choose the Copart CSV",
                                          filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.csv_var.set(path)
            self._count_rows()

    def _pick_out(self):
        path = filedialog.askdirectory(title="Choose where to save results")
        if path:
            self.out_var.set(path)

    def _count_rows(self):
        path = Path(self.csv_var.get())
        if not path.is_file():
            self.rows_info.set("File not found")
            return
        self.rows_info.set("Counting rows...")

        def work():
            try:
                n = count_data_rows(path)
                self.events.put(("rows_counted", n))
            except Exception as e:
                self.events.put(("rows_counted", e))
        threading.Thread(target=work, daemon=True).start()

    def _apply_split(self, _evt=None):
        if not self.total_rows:
            messagebox.showinfo("Quick split", "Still counting rows in the CSV. Try again in a moment.")
            return
        i = int(self.member_var.get().split()[1]) - 1
        start, end = split_ranges(self.total_rows, TEAM_SIZE)[i]
        self.start_var.set(str(start))
        self.end_var.set(str(end))

    def _open_out(self):
        out = Path(self.out_var.get())
        if not out.exists():
            messagebox.showinfo("Output folder", "That folder doesn't exist yet. It's created on Start.")
            return
        if sys.platform.startswith("win"):
            os.startfile(out)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(out)], check=False)
        else:
            subprocess.run(["xdg-open", str(out)], check=False)

    def _validated_config(self, retry: bool) -> RunConfig | None:
        csv_path = Path(self.csv_var.get())
        if not csv_path.is_file():
            messagebox.showerror("Input CSV", "Choose the Copart CSV file first.")
            return None
        try:
            start, end = int(self.start_var.get()), int(self.end_var.get())
        except ValueError:
            messagebox.showerror("Rows", "Row numbers must be whole numbers (row 1 = first vehicle).")
            return None
        if start < 1 or end < start or (self.total_rows and end > self.total_rows):
            limit = f" (the file has {self.total_rows:,} rows)" if self.total_rows else ""
            messagebox.showerror("Rows", f"Pick a range with 1 <= from <= to{limit}.")
            return None
        try:
            delay = float(self.delay_var.get())
        except ValueError:
            messagebox.showerror("Delay", "Delay must be a number of seconds, e.g. 1.0")
            return None
        if delay < MIN_DELAY:
            delay = MIN_DELAY
            self.delay_var.set(str(MIN_DELAY))
        out = self.out_var.get().strip()
        if not out:
            messagebox.showerror("Output folder", "Choose an output folder.")
            return None
        return RunConfig(csv_path=csv_path, start_row=start, end_row=end, out_dir=Path(out),
                         base_delay=delay, retry_failed=retry)

    # ---- run controls ----------------------------------------------------------
    def _start(self, retry: bool):
        cfg = self._validated_config(retry)
        if cfg is None:
            return
        self._save_settings()
        self._set_running(True)
        self.status_text.set("Starting...")
        self.status_label.configure(foreground=COLORS["info"])
        self.worker = Worker(cfg, emit=lambda kind, payload: self.events.put((kind, payload)))
        self.thread = threading.Thread(target=self.worker.run, daemon=True)
        self.thread.start()

    def _toggle_pause(self):
        if not self.worker:
            return
        if self.worker.paused:
            self.worker.resume()
            self.pause_btn.configure(text="Pause")
        else:
            self.worker.pause()
            self.pause_btn.configure(text="Resume")
            self.status_text.set("Pausing after the current lookup...")

    def _stop(self):
        if self.worker:
            self.worker.stop()
            self.status_text.set("Stopping after the current lookup...")
            self.stop_btn.configure(state="disabled")

    def _set_running(self, running: bool):
        for w in self.inputs:
            w.configure(state="disabled" if running else ("readonly" if w is self.member_box else "normal"))
        self.start_btn.configure(state="disabled" if running else "normal")
        self.retry_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(state="normal" if running else "disabled", text="Pause")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _on_close(self):
        if self.thread and self.thread.is_alive():
            if not messagebox.askyesno("Quit", "A run is in progress. Stop it and quit?\n"
                                               "Everything done so far is saved; Start resumes later."):
                return
            self.worker.stop()
            self.thread.join(timeout=5)
        self._save_settings()
        self.destroy()

    # ---- events from the worker thread --------------------------------------------
    def _append_log(self, line: str):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_progress(self, p: dict):
        total, handled = max(1, p["total"]), p["handled"]
        self.bar.configure(maximum=total, value=handled)
        self.progress_text.set(f"{handled:,} / {p['total']:,}  ({handled / total:.1%})")
        self.counts_text.set(f"OK {p['ok']:,}   |   No value / not found {p['no_value']:,}   |   "
                             f"Skipped {p['skipped']:,}   |   Errors {p['errors']:,}")
        if "rate" in p:
            self.rate_text.set(f"{p['rate']:.2f} rows/s   |   ETA {fmt_duration(p.get('eta'))}   |   "
                               f"delay {p.get('delay', 0):.2f}s")

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "progress":
                    self._show_progress(payload)
                elif kind == "status":
                    text, level = payload
                    self.status_text.set(text)
                    self.status_label.configure(foreground=COLORS.get(level, COLORS["info"]))
                elif kind == "paused":
                    self.pause_btn.configure(text="Resume" if payload else "Pause")
                elif kind == "finished":
                    self._show_progress(payload)  # keeps the last rate line visible
                    self._set_running(False)
                elif kind == "rows_counted":
                    if isinstance(payload, Exception):
                        self.rows_info.set(f"Couldn't read file: {payload}")
                    else:
                        self.total_rows = payload
                        self.rows_info.set(f"{payload:,} vehicle rows")
                        if not self.end_var.get():
                            self.end_var.set(str(payload))
        except queue.Empty:
            pass
        self.after(150, self._poll)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
