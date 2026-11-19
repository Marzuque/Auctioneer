# SPV lookup tool

Looks up the Texas DMV **Standard Presumptive Value** for each vehicle in
`copart_modeling_ready.csv` (VIN + reported mileage) and writes the values to CSV, so we can
use them as price labels and features for the model.

It sends the same form the [TxDMV calculator](https://www.txdmv.gov/standard-presumptive-calculator)
sends (`tools.txdmv.gov/tools/SPV/spv_lookup.php`), one vehicle at a time, at a polite pace.

## Setup (once)

1. Python 3.10 or newer. Tkinter ships with the python.org installers for Windows and macOS.
   On Linux you may need `sudo apt install python3-tk`.
2. From the repo root, install the requirements:
   ```
   pip install -r requirements.txt
   ```
3. Put `copart_modeling_ready.csv` in the repo root. It is **not** in git; get it from the team drive.

## Running your share

```
python -m spv_scraper
```

1. **Input CSV** is filled in automatically if the file is in the repo root. The window shows the row count (252,613).
2. **Quick split → Member N of 4** fills in your rows:

   | Member | Rows |
   |---|---|
   | 1 | 1 – 63,153 |
   | 2 | 63,154 – 126,306 |
   | 3 | 126,307 – 189,459 |
   | 4 | 189,460 – 252,613 |

3. Pick an **Output folder** and press **Start**.

A full share takes about **31 hours**: each lookup is about 1.75 s, plus a one-minute break every 500.
Keep the laptop plugged in and turn off sleep. You don't have to do it in one go:

- **Stop** (or closing the window) saves everything. **Start** with the same rows carries on where it left off.
- **Pause / Resume** holds the run without closing.
- **Retry failed** re-runs only rows that failed for network or site reasons, after the main pass is done.

### Don't get us blocked

- **Run one copy per internet connection.** Two teammates on the same Wi-Fi (e.g. campus) share one IP, so their lookups add up. Space your runs out, or raise the delay to 2+ s each.
- **Leave the delay at 1.0 s** (the minimum allowed is 0.75 s).
- The tool slows itself down when it needs to:
  - It backs off when the site returns errors or "too many requests", starting at 30 s and doubling up to 15 min.
  - It doubles its delay when responses get slow.
  - It **auto-pauses** after 5 failures in a row. If that happens, wait 30+ minutes before pressing Resume.

## What you get

The files are named after your row range, e.g. `spv_000001-063153_*`.

| File | Contents |
|---|---|
| `*_results.csv` | One line per vehicle the site priced (columns below) |
| `*_issues.csv` | One line per row that was skipped or failed: `reason`, `detail`, `attempts`, `at_utc` |
| `*_run.log` | Everything shown in the window's log |
| `raw/` | HTML of any page the tool couldn't understand, for debugging |

Columns in `*_results.csv`:

- `row_num`, `lot_id`, `vin`
- `mileage_sent`, `odometer_status`, `mileage_trusted`
- `spv_private_value`
- `site_year`, `site_make`, `site_model`, `site_mileage`
- `queried_at_utc`

Issue reasons:

| Reason | Meaning | Retried by "Retry failed"? |
|---|---|---|
| `invalid_vin` | Not a 17-character VIN, or the site rejected it | no |
| `mileage_out_of_range` | Over 999,999 miles; the form takes 6 digits | no |
| `not_found` | The site doesn't know the VIN | no |
| `no_value` | The site found the car but shows no value, e.g. `$0` for some late-model vehicles | no |
| `timeout`, `http_error`, `blocked` | Network trouble or the site pushing back | yes |
| `site_error`, `parse_error` | A page the tool didn't recognise (saved in `raw/`) | yes |

## Combining everyone's results

Collect the four output folders in one place, then run:

```
python -m spv_scraper.merge out_member1/ out_member2/ out_member3/ out_member4/ --source copart_modeling_ready.csv
```

This writes `copart_with_spv.csv`: every source row plus these `spv_*` columns:

- `spv_status`: `ok`, an issue reason, or `missing`
- `spv_private_value`
- `spv_site_year`, `spv_site_make`, `spv_site_model`, `spv_site_mileage`
- `spv_mileage_sent`, `spv_mileage_trusted`
- `spv_queried_at_utc`, `spv_issue_detail`

It also prints a coverage report. That lists any row nobody covered, and warns if the lookups span more than a week (TxDMV updates values weekly).

## Notes for the modelling side

- **This isn't a retail price.** The site labels the number "Private Value". TxDMV describes SPV as a Black Book value based on Texas wholesale sales data.
- **Untrusted odometers are looked up anyway.** These are rows with `odometer_status` ≠ `actual`, or 0 miles. Filter on `mileage_trusted`.
  - When we send 0 miles, the site treats it as "no mileage given": `site_mileage` comes back blank.
- **Pre-2005 vehicles:** TxDMV applies no mileage adjustment.
- **Vehicles 25+ years old** are SPV-exempt and may come back `not_found` or `no_value`.
- **Keep the scraped values out of this public repo.** `.gitignore` already blocks `*.csv`.

## Tests

```
python -m pytest spv_scraper/tests
```

The parser tests use real pages saved from the site, in `tests/fixtures/`.
