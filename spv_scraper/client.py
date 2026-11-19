"""Talks to the TxDMV SPV lookup form and turns its HTML into a LookupResult.

The calculator on txdmv.gov is an iframe around a plain HTML form:
POST https://tools.txdmv.gov/tools/SPV/spv_lookup.php  with fields  vin, mileage
No CAPTCHA, no token, no JavaScript.

Success pages contain <table id="result"> with "Label:" / value cell pairs
(Year, Make, Model, VIN, Mileage, Private Value). Failures are a single
<p>Error: ...</p>.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

URL = "https://tools.txdmv.gov/tools/SPV/spv_lookup.php"
USER_AGENT = "Mozilla/5.0 (compatible; research data collection; SPV lookup)"

# Final statuses: the answer for this VIN + mileage, never retried.
OK = "ok"
NO_VALUE = "no_value"          # vehicle found but no value shown
NOT_FOUND = "not_found"        # site says the VIN was not found
INVALID_VIN = "invalid_vin"    # site (or our pre-check) rejects the VIN
MILEAGE_OUT_OF_RANGE = "mileage_out_of_range"  # pre-check only: form takes 6 digits

# Transient statuses: retried in the run loop with backoff.
HTTP_ERROR = "http_error"
TIMEOUT = "timeout"
BLOCKED = "blocked"            # page doesn't look like the SPV tool (or says "too many requests")

# Anomalies: logged right away (no in-loop retry), but "Retry failed" re-runs them.
SITE_ERROR = "site_error"      # an "Error:" message we don't recognise
PARSE_ERROR = "parse_error"    # result table present but we couldn't read it

FINAL = {OK, NO_VALUE, NOT_FOUND, INVALID_VIN, MILEAGE_OUT_OF_RANGE}
TRANSIENT = {HTTP_ERROR, TIMEOUT, BLOCKED}
RETRIABLE = TRANSIENT | {SITE_ERROR, PARSE_ERROR}

_BLOCK_HINTS = ("too many", "rate limit", "temporarily", "try again later",
                "access denied", "forbidden", "unavailable", "captcha", "request rejected")
_MONEY = re.compile(r"\$?\s*([\d,]+(?:\.\d+)?)")


@dataclass
class LookupResult:
    status: str
    spv_value: int | None = None
    year: str = ""
    make: str = ""
    model: str = ""
    site_mileage: str = ""
    detail: str = ""
    http_status: int | None = None
    elapsed: float = 0.0
    retry_after: float | None = None
    raw_html: str | None = field(default=None, repr=False)  # kept only for anomalies


def _clean(text: str) -> str:
    return " ".join(text.replace("﻿", "").split())


def parse_money(text: str) -> int | None:
    m = _MONEY.search(text or "")
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", ""))))
    except ValueError:
        return None


def parse_response(html: str) -> LookupResult:
    """Classify one response page. Pure function: no network."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="result")

    if table is not None:
        fields: dict[str, str] = {}
        for td in table.find_all("td"):
            label = _clean(td.get_text())
            if not label.endswith(":"):
                continue
            value_td = td.find_next_sibling("td")
            if value_td is not None:
                fields.setdefault(label[:-1].strip().lower(), _clean(value_td.get_text()))

        res = LookupResult(
            status=OK,
            year=fields.get("year", ""),
            make=fields.get("make", ""),
            model=fields.get("model", ""),
            site_mileage=fields.get("mileage", ""),
        )
        value_text = fields.get("private value")
        if value_text is None:
            # Table exists but the value row is missing: page layout may have changed.
            res.status = PARSE_ERROR
            res.detail = "result table has no 'Private Value' row"
            res.raw_html = html
            return res
        res.spv_value = parse_money(value_text)
        if res.spv_value is None or res.spv_value <= 0:
            res.status = NO_VALUE
            res.spv_value = None
            res.detail = value_text or "empty value"
        return res

    error_p = next((p for p in soup.find_all("p") if _clean(p.get_text()).lower().startswith("error")), None)
    if error_p is not None:
        msg = _clean(error_p.get_text())
        low = msg.lower()
        if "invalid vin" in low:
            return LookupResult(status=INVALID_VIN, detail=msg)
        if "not found" in low:
            return LookupResult(status=NOT_FOUND, detail=msg)
        if any(h in low for h in _BLOCK_HINTS):
            return LookupResult(status=BLOCKED, detail=msg, raw_html=html)
        return LookupResult(status=SITE_ERROR, detail=msg, raw_html=html)

    # Neither a result nor an error: a block page, maintenance page, or redesign.
    title = _clean(soup.title.get_text()) if soup.title else ""
    snippet = _clean(soup.get_text())[:200]
    return LookupResult(status=BLOCKED, detail=f"unexpected page: {title or snippet or 'empty body'}",
                        raw_html=html)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: ignore, our own backoff applies


class SpvClient:
    """One persistent HTTP session; call lookup() once per VIN."""

    def __init__(self, timeout: float = 20.0, session: requests.Session | None = None, url: str = URL):
        self.url = url
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def lookup(self, vin: str, mileage: int | str | None) -> LookupResult:
        data = {"vin": vin, "mileage": "" if mileage in (None, "") else str(mileage)}
        t0 = time.perf_counter()
        try:
            resp = self.session.post(self.url, data=data, headers={"Referer": self.url}, timeout=self.timeout)
        except requests.Timeout as e:
            return LookupResult(status=TIMEOUT, detail=str(e)[:200], elapsed=time.perf_counter() - t0)
        except requests.RequestException as e:
            return LookupResult(status=HTTP_ERROR, detail=f"{type(e).__name__}: {str(e)[:200]}",
                                elapsed=time.perf_counter() - t0)
        elapsed = time.perf_counter() - t0

        if resp.status_code != 200:
            status = BLOCKED if resp.status_code in (403, 429) else HTTP_ERROR
            return LookupResult(status=status, detail=f"HTTP {resp.status_code}", http_status=resp.status_code,
                                elapsed=elapsed, retry_after=_retry_after_seconds(resp.headers.get("Retry-After")))

        res = parse_response(resp.text)
        res.http_status = resp.status_code
        res.elapsed = elapsed
        return res

    def close(self) -> None:
        self.session.close()
