"""Hawaii County adapter — Laserfiche "Council Records System" metadata,
enriched with bill titles from Granicus agendas.

Hawaii County's authoritative legislative record is the Laserfiche WebLink
portal at records.hawaiicounty.gov. Each bill/resolution/ordinance is a
document carrying a structured "Bill/Resolution" template: type, number,
introducer, referring committee, a dated action history, status, reading
dates, and vote tallies. This is far richer than the meeting agendas — but the
template has NO title field (the title lives only in the scanned PDF), so we
borrow the descriptive title from the Granicus agenda for the same bill number.

The records site runs a WAF (Barracuda) that blocks *headless browsers*, but
WebLink 9 is plain server-rendered ASP.NET WebForms, so a normal requests
session (browser-like headers, cookie jar for the CookieCheck handshake) reads
it fine — no browser, and it passes the WAF where Playwright is blocked.

Navigation is all GETs:
  Browse.aspx?startid={folderId}            -> category folder (year subfolders)
  pager "Last" link                         -> page with the current term folder
  ../{termFolderId}/Row1.aspx  (relative)   -> the term folder's documents
  ../{docId}/Page1.aspx        (relative)   -> a document
  DocView.aspx?id={docId}&dbid=0            -> the document's template metadata
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Iterator
from urllib.parse import urljoin

import requests
import urllib3
from bs4 import BeautifulSoup

from tracker.legislative.adapters.base import (
    ActionRecord,
    BillRecord,
    CouncilAdapter,
)
from tracker.legislative.adapters.granicus import GranicusAdapter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

_BASE = "https://records.hawaiicounty.gov/WebLink/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_CATEGORY_STARTID = {"Bill": 50, "Resolution": 41, "Ordinance": 47}
_TYPE_LABEL = {"BIL": "Bill", "RES": "Resolution", "ORD": "Ordinance"}
_DOCNAME_RE = re.compile(r"\b(BIL|RES|ORD)\s+(\d+)\s+Draft\s+(\d+)", re.I)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
_ROW1_RE = re.compile(r"Row1\.aspx", re.I)
_PAGE_RE = re.compile(r"/\d+/Page1\.aspx", re.I)


def _norm_key(type_label: str, number: str) -> str:
    return f"{type_label} {int(number)}"


def _iso_from_mdy(s: str) -> str | None:
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    mo, d, y = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return date(y, int(mo), int(d)).isoformat()
    except ValueError:
        return None


class HawaiiCountyAdapter(CouncilAdapter):
    council_id = "hawaii"

    def __init__(self, term_year: int | None = None):
        self.term_year = term_year or date.today().year
        self._s: requests.Session | None = None

    # ---- HTTP --------------------------------------------------------------

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        s.verify = False
        # Establish the CookieCheck session.
        s.get(urljoin(_BASE, "Browse.aspx?startid=50&dbid=0"), timeout=30)
        return s

    def _get(self, url: str) -> tuple[BeautifulSoup, str]:
        r = self._s.get(url, timeout=30)
        return BeautifulSoup(r.text, "lxml"), str(r.url)

    # ---- navigation --------------------------------------------------------

    def _term_folder(self, startid: int) -> str | None:
        """URL of the current council-term folder for a category. Year folders
        are paginated oldest-first; the current term is on the last page."""
        soup, cur = self._get(urljoin(_BASE, f"Browse.aspx?startid={startid}&dbid=0"))
        last = next(
            (urljoin(cur, a["href"]) for a in soup.select("a[href]")
             if a.get_text(strip=True) == "Last"),
            None,
        )
        if last:
            soup, cur = self._get(last)

        yr = str(self.term_year)
        best: tuple[str, str] | None = None
        for a in soup.select("a[href]"):
            if not _ROW1_RE.search(a["href"]):
                continue
            label = a.get_text(" ", strip=True).replace("[Icon]", "").strip()
            if not re.search(r"\d{4}", label):  # skip pager links (First/1/2…)
                continue
            full = urljoin(cur, a["href"])
            if yr in label:
                return full
            if best is None or label > best[0]:
                best = (label, full)
        return best[1] if best else None

    def _list_docs(self, folder_url: str) -> list[tuple[str, str]]:
        """[(doc_id, name)] across all pages of a folder. Folders paginate with
        numbered page links (Row1/Row26/Row51…), not a 'Next' link, so we visit
        every pager page belonging to this folder."""
        m = re.search(r"/fol/(\d+)/", folder_url)
        fol_id = m.group(1) if m else None
        docs: dict[str, str] = {}
        to_visit = [folder_url]
        visited: set[str] = set()
        while to_visit and len(visited) < 12:  # page cap
            url = to_visit.pop()
            if url in visited:
                continue
            visited.add(url)
            soup, cur = self._get(url)
            for a in soup.select("a[href]"):
                full = urljoin(cur, a["href"])
                pm = re.search(r"/(\d+)/Page1\.aspx", full)
                if pm:
                    docs[pm.group(1)] = a.get_text(" ", strip=True).replace("[Icon]", "").strip()
                elif fol_id and re.search(rf"/fol/{fol_id}/Row\d+\.aspx", full) and full not in visited:
                    to_visit.append(full)
        return list(docs.items())

    def _metadata(self, doc_id: str) -> dict[str, str]:
        soup, _ = self._get(urljoin(_BASE, f"DocView.aspx?id={doc_id}&dbid=0&cr=1"))
        scope = soup.select_one(".TemplateFields") or soup
        names = [e.get_text(" ", strip=True) for e in scope.select(".FieldDisplayName")]
        vals = [e.get_text(" ", strip=True) for e in scope.select(".FieldDisplayValue")]
        return {n: v for n, v in zip(names, vals) if n}

    # ---- record assembly ---------------------------------------------------

    def _build_record(self, meta: dict[str, str], doc_id: str) -> BillRecord | None:
        type_code = (meta.get("Bill/Resolution - Type") or "").strip().upper()
        type_label = _TYPE_LABEL.get(type_code)
        number = (meta.get("Bill/Resolution") or "").strip()
        if not type_label or not number.isdigit():
            return None

        actions = sorted(
            ((int(k.split()[1]), v) for k, v in meta.items()
             if re.fullmatch(r"Action \d+", k) and v),
            key=lambda x: x[0],
        )
        last_action = actions[-1][1] if actions else None
        last_action_date = _iso_from_mdy(last_action) if last_action else None

        status = (meta.get("Status") or "").strip() or None
        if not status and last_action:
            la = last_action.lower()
            if "second" in la and "final" in la:
                status = "Passed Second Reading"
            elif "first reading" in la:
                status = "Passed First Reading"
            elif "postponed" in la:
                status = "Postponed"

        intro_date = _iso_from_mdy(meta.get("Reading Date", "")) or (
            _iso_from_mdy(actions[0][1]) if actions else None
        )
        return BillRecord(
            council=self.council_id,
            bill_number=_norm_key(type_label, number),
            title=None,
            bill_type=type_label,
            introducer=(meta.get("Introducer") or "").strip() or None,
            introduced_date=intro_date,
            status=status,
            last_action=last_action,
            last_action_date=last_action_date,
            url=urljoin(_BASE, f"DocView.aspx?id={doc_id}&dbid=0"),
            raw_subject=(meta.get("Referred To") or "").strip() or None,
        )

    def _doc_index(self) -> dict[str, str]:
        index: dict[str, tuple[str, int]] = {}
        for type_label, startid in _CATEGORY_STARTID.items():
            try:
                folder = self._term_folder(startid)
            except Exception as e:
                log.warning("hawaii term folder (%s) failed: %s", type_label, e)
                continue
            if not folder:
                continue
            for doc_id, name in self._list_docs(folder):
                m = _DOCNAME_RE.search(name)
                if not m:
                    continue
                code, num, draft = m.group(1).upper(), m.group(2), int(m.group(3))
                key = _norm_key(_TYPE_LABEL.get(code, code), num)
                if key not in index or draft > index[key][1]:
                    index[key] = (doc_id, draft)
        return {k: v[0] for k, v in index.items()}

    def _active_from_granicus(self, since: date | None) -> dict[str, BillRecord]:
        active: dict[str, BillRecord] = {}
        try:
            gran = GranicusAdapter.for_council("hawaii")
            for b in gran.fetch_bills(since=since):
                active[b.bill_number] = b
        except Exception as e:
            log.warning("Granicus active-set fetch failed: %s", e)
        return active

    # ---- public API --------------------------------------------------------

    def fetch_bills(self, since: date | None = None) -> Iterator[BillRecord]:
        active = self._active_from_granicus(since)
        if not active:
            return

        try:
            self._s = self._session()
            index = self._doc_index()
        except Exception as e:
            log.warning("Laserfiche unreachable (%s); using Granicus-only data", e)
            yield from active.values()
            return

        if not index:
            log.warning("Laserfiche index empty; using Granicus-only data for hawaii")
            yield from active.values()
            return

        for number, gbill in active.items():
            doc_id = index.get(number)
            if not doc_id:
                yield gbill  # on agenda but not yet filed in Laserfiche
                continue
            try:
                rec = self._build_record(self._metadata(doc_id), doc_id)
            except Exception as e:
                log.warning("hawaii metadata (%s) failed: %s", number, e)
                rec = None
            if rec is None:
                yield gbill
                continue
            rec.title = gbill.title  # Laserfiche authoritative for metadata,
            rec.raw_subject = gbill.title  # Granicus for the descriptive title
            yield rec

    def fetch_actions(self, bill_number: str) -> Iterator[ActionRecord]:
        return iter(())
