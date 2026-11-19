from pathlib import Path

import pytest
import requests

from spv_scraper import client as c

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_parse_normal_result():
    r = c.parse_response(load("ok_normal.html"))
    assert r.status == c.OK
    assert r.spv_value == 6275
    assert (r.year, r.make, r.model, r.site_mileage) == ("2011", "Lexus", "ES 350", "167,218")
    assert r.raw_html is None


def test_parse_zero_miles_echoes_blank_mileage():
    r = c.parse_response(load("ok_zero_miles.html"))
    assert r.status == c.OK
    assert r.spv_value == 2950
    assert r.site_mileage == ""  # site treats 0 as "no mileage given"


def test_parse_pre2005():
    r = c.parse_response(load("ok_pre2005.html"))
    assert r.status == c.OK and r.spv_value == 1200 and r.year == "2003"


def test_parse_invalid_vin():
    r = c.parse_response(load("error_invalid_vin.html"))
    assert r.status == c.INVALID_VIN
    assert "Invalid VIN" in r.detail


def test_parse_not_found():
    r = c.parse_response(load("error_not_found.html"))
    assert r.status == c.NOT_FOUND


def test_form_page_counts_as_blocked_not_no_value():
    # If the site ever bounces us back to the empty form, don't mark the row as "no value".
    r = c.parse_response(load("form_page.html"))
    assert r.status == c.BLOCKED
    assert r.raw_html


def test_unexpected_page_is_blocked():
    r = c.parse_response("<html><title>Access Denied</title><body>Request blocked</body></html>")
    assert r.status == c.BLOCKED and "Access Denied" in r.detail


def test_rate_limit_error_message_is_blocked():
    r = c.parse_response("<body><p>Error: Too many requests, try again later.</p></body>")
    assert r.status == c.BLOCKED


def test_unknown_error_message_is_site_error():
    r = c.parse_response("<body><p>Error: Value not available for this vehicle.</p></body>")
    assert r.status == c.SITE_ERROR and r.raw_html


def test_empty_value_is_no_value():
    html = load("ok_normal.html").replace("$6,275", "")
    r = c.parse_response(html)
    assert r.status == c.NO_VALUE and r.spv_value is None and r.year == "2011"


def test_missing_value_row_is_parse_error():
    html = load("ok_normal.html").replace("Private Value:", "Something Else:")
    r = c.parse_response(html)
    assert r.status == c.PARSE_ERROR


@pytest.mark.parametrize("text,expected", [("$6,275", 6275), ("$12,000.49", 12000), ("N/A", None), ("", None)])
def test_parse_money(text, expected):
    assert c.parse_money(text) == expected


class FakeResp:
    def __init__(self, status, text="", headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}


class FakeSession:
    def __init__(self, outcome):
        self.outcome, self.headers, self.calls = outcome, {}, []

    def post(self, url, data, headers, timeout):
        self.calls.append(data)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def close(self):
        pass


def test_lookup_ok_sends_form_fields():
    s = FakeSession(FakeResp(200, load("ok_normal.html")))
    r = c.SpvClient(session=s).lookup("JTHBK1EGXB2421163", 167218)
    assert r.status == c.OK and r.http_status == 200
    assert s.calls == [{"vin": "JTHBK1EGXB2421163", "mileage": "167218"}]


def test_lookup_429_with_retry_after():
    r = c.SpvClient(session=FakeSession(FakeResp(429, headers={"Retry-After": "120"}))).lookup("X", 1)
    assert r.status == c.BLOCKED and r.retry_after == 120.0 and r.http_status == 429


def test_lookup_500_is_http_error():
    r = c.SpvClient(session=FakeSession(FakeResp(503))).lookup("X", 1)
    assert r.status == c.HTTP_ERROR and r.retry_after is None


def test_lookup_timeout():
    r = c.SpvClient(session=FakeSession(requests.Timeout("slow"))).lookup("X", 1)
    assert r.status == c.TIMEOUT


def test_lookup_connection_error():
    r = c.SpvClient(session=FakeSession(requests.ConnectionError("down"))).lookup("X", 1)
    assert r.status == c.HTTP_ERROR and "ConnectionError" in r.detail
