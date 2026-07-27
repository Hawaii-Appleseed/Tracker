"""Honolulu City Council adapter — uses the (undocumented but stable) JSON
endpoints that back the hnldoc.ehawaii.gov bill/resolution browser.

POST /hnldoc/browse/bills.json with:
  {"year": YYYY, "pagination": {"page": N, "sort": "date", "sortDirection": "desc"}}
Returns: {"total": int, "criteria": {...}, "results": [BillRow, ...]}

The same shape exists for /hnldoc/browse/resolutions.json.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Iterator

import requests
from bs4 import BeautifulSoup

from tracker.legislative.adapters.base import (
    ActionRecord,
    BillRecord,
    CouncilAdapter,
)

log = logging.getLogger(__name__)

BASE = "https://hnldoc.ehawaii.gov"
# A browser-like UA + HTML Accept for the server-rendered measure page (the
# browse JSON endpoints are happy with the Tracker UA, but the measure page
# gateway is fussier).
_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_TYPE_LABEL = {
    "BILL": "Bill",
    "RESOLUTION": "Resolution",
}

# hnldoc's browse index begins at 2017 — 2016 and earlier return an empty
# result set rather than an error (probed 2026-07-27). A full backfill starts
# here instead of walking back through years that can never return anything.
EARLIEST_YEAR = 2017


def _epoch_ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    try:
        return datetime.utcfromtimestamp(ms / 1000).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _date_obj_to_iso(d: dict | None) -> str | None:
    if not d:
        return None
    try:
        return f"{d['year']:04d}-{d['monthValue']:02d}-{d['dayOfMonth']:02d}"
    except (KeyError, TypeError):
        return None


def _to_int(s: str | None) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def _mdy_to_iso(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


class HonoluluAdapter(CouncilAdapter):
    council_id = "honolulu"
    PAGE_SIZE = 50  # server-enforced, but pagination.page=-1 returns all

    def __init__(self, session: requests.Session | None = None, timeout: int = 30):
        self.timeout = timeout
        # bill_number -> measure id, populated as fetch_bills yields each row so
        # fetch_actions can reach the per-measure page without re-querying.
        self._measure_ids: dict[str, int] = {}
        # The measure-page gateway redirect-loops when sent the JSON browse
        # headers (Content-Type/X-Requested-With), so the HTML page uses its own
        # bare session. Built lazily.
        self._html_session: requests.Session | None = None
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Tracker/0.1 (+https://github.com/dtomkatsu/Tracker)"
                ),
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE}/hnldoc/browse/c/bills",
            }
        )

    def _fetch_year(self, kind: str, year: int) -> list[dict[str, Any]]:
        url = f"{BASE}/hnldoc/browse/{kind}.json"
        # page=-1 returns everything in one shot; total counts are small (<200/year).
        payload = {
            "year": year,
            "pagination": {"page": -1, "sort": "date", "sortDirection": "desc"},
        }
        r = self.session.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("results", []) or []

    def _row_to_bill(self, row: dict[str, Any]) -> BillRecord:
        bill_type = _TYPE_LABEL.get(row.get("type"), row.get("type"))
        last_action_iso = _date_obj_to_iso(row.get("lastEventDate"))
        title = (row.get("title") or "").strip() or None
        summary = (row.get("summary") or "").strip() or None
        # Use title + summary as raw_subject for richer classification signal.
        raw_subject = summary
        return BillRecord(
            council=self.council_id,
            bill_number=row.get("displayNumber") or str(row.get("number", "")),
            title=title,
            bill_type=bill_type,
            introducer=(row.get("introducers") or "").strip() or None,
            introduced_date=_epoch_ms_to_iso(row.get("dateIntroduced")),
            status=row.get("lastEventType"),
            last_action=row.get("lastEventDescription"),
            last_action_date=last_action_iso,
            # Public measure page. The /measure/browse/{id} variant is the
            # in-app authenticated route: it gateway-bounces fine for anonymous
            # users but 403s for anyone with an existing eHawaii SSO session.
            url=f"{BASE}/hnldoc/measure/{row['id']}",
            raw_subject=raw_subject,
        )

    def fetch_bills(self, since: date | None = None) -> Iterator[BillRecord]:
        # since=None means "everything available", not "this year" — the browse
        # endpoint is queried a year at a time, so a full backfill walks from
        # the first year hnldoc holds.
        start_year = since.year if since else EARLIEST_YEAR
        end_year = date.today().year
        for year in range(start_year, end_year + 1):
            for kind in ("bills", "resolutions"):
                try:
                    rows = self._fetch_year(kind, year)
                except requests.HTTPError as e:
                    log.warning("Honolulu %s %s fetch failed: %s", kind, year, e)
                    continue
                for row in rows:
                    bill = self._row_to_bill(row)
                    if since and bill.introduced_date:
                        if bill.introduced_date < since.isoformat():
                            continue
                    if row.get("id") is not None:
                        self._measure_ids[bill.bill_number] = row["id"]
                    yield bill

    def fetch_actions(self, bill_number: str) -> Iterator[ActionRecord]:
        """Scrape the per-measure page's status table for the full event history.

        The browse JSON only exposes the latest event; the measure page at
        /hnldoc/measure/{id} server-renders a Date / Type / Description table of
        every event (the 4th column is a hidden epoch-ms timestamp). Relies on
        fetch_bills having cached the measure id for this bill_number.
        """
        measure_id = self._measure_ids.get(bill_number)
        if measure_id is None:
            return
        if self._html_session is None:
            self._html_session = requests.Session()
            self._html_session.headers.update(_HTML_HEADERS)
        url = f"{BASE}/hnldoc/measure/{measure_id}"
        try:
            r = self._html_session.get(url, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("Honolulu measure page fetch failed for %s: %s", bill_number, e)
            return
        yield from self._parse_events(r.text, bill_number)

    def _parse_events(self, html: str, bill_number: str) -> Iterator[ActionRecord]:
        soup = BeautifulSoup(html, "lxml")
        table = self._event_table(soup)
        if table is None:
            return
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) < 3:
                continue
            date_txt = cells[0].get_text(" ", strip=True)
            ev_type = cells[1].get_text(" ", strip=True) or None
            desc = cells[2].get_text(" ", strip=True)
            if not desc:
                continue
            # 4th cell is an epoch-ms timestamp — more reliable than the m/d/Y text.
            iso = None
            if len(cells) >= 4:
                iso = _epoch_ms_to_iso(_to_int(cells[3].get_text(strip=True)))
            iso = iso or _mdy_to_iso(date_txt)
            yield ActionRecord(
                council=self.council_id,
                bill_number=bill_number,
                action_date=iso or "",
                action=desc,
                committee=ev_type,
            )

    @staticmethod
    def _event_table(soup: BeautifulSoup) -> Any:
        """The status/events table, found by its Date/Type/Description header."""
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if {"date", "type", "description"} <= set(headers):
                return table
        return None
