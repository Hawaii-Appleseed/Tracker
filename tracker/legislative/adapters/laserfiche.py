"""Hawaii County adapter — Laserfiche "Council Records System" metadata,
enriched with bill titles from Granicus agendas.

Hawaii County's authoritative legislative record is the Laserfiche WebLink
portal at records.hawaiicounty.gov. Each bill/resolution is a document carrying
a structured "Bill/Resolution" template: type, number, council term, introducer,
referring committee, a dated action history, status, reading dates, and vote
tallies. Ordinances carry a thinner "Ordinances" template (type, term, year,
number, effective date). None of these templates has a title field — the title
lives only in the scanned PDF — so titles are borrowed from the Granicus agenda
for the same bill.

Laserfiche is the *spine* of this adapter: every document inside the retention
window is yielded, and a Granicus title is attached where one exists. (It used
to be the other way round — iterate the handful of bills seen on recent agendas
and look each one up — which threw away all but ~2% of the window.) Bills that
never reached a scraped agenda are still yielded, with title=None; their number,
type, introducer, status, dates and action history are all searchable on their
own. The walk is bounded by the retention window rather than descending the
whole archive, which runs back to 1969 for bills and 1905 for ordinances.

The portal runs WebLink 11, an Angular SPA over a JSON API. Browse.aspx now
serves only an app shell, so the old server-rendered Row1.aspx/DocView.aspx
scraping reads nothing at all. Navigation is three POSTs:
  FolderListingService.aspx/GetFolderListing2 -> folder children (paged)
  FolderListingService.aspx/GetMetaData       -> a document's template fields
with DocView.aspx?id=… kept as the human-facing permalink.

The records site runs a WAF (Barracuda) that blocks *headless browsers* but is
happy with a normal requests session (browser-like headers, cookie jar for the
CookieCheck handshake) — no browser, and it passes where Playwright is blocked.
Granicus is the opposite (blocks bare HTTP, tolerates real Chromium), which is
why the two halves of this adapter use different transports.

Folder layout:
  Bills (50) / Resolutions (41) / Ordinances (47)
    └── term folder: "2024-2026" (bills, resolutions) or "2026" (ordinances)
          └── documents: "BIL 001 Draft 01 2024-2026", "ORD 2026-001 2024-2026"
                └── "Word Documents" — untemplated .doc duplicates, skipped
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date
from typing import Iterator, NamedTuple
from urllib.parse import urljoin

import requests
import urllib3

from tracker.legislative.adapters.base import (
    ActionRecord,
    BillRecord,
    CouncilAdapter,
)
from tracker.legislative.adapters.granicus import (
    GranicusAdapter,
    hawaii_bill_key,
    window_start,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
log = logging.getLogger(__name__)

_BASE = "https://records.hawaiicounty.gov/WebLink/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_REPO_FALLBACK = "ClerkCouncil"
_CATEGORY_STARTID = {"Bill": 50, "Resolution": 41, "Ordinance": 47}
_TYPE_LABEL = {"BIL": "Bill", "RES": "Resolution", "ORD": "Ordinance"}

# "BIL 001 Draft 01 2024-2026" — bills and resolutions are filed per draft; the
# highest draft is the current text.
_DOCNAME_RE = re.compile(r"\b(BIL|RES)\s+(\d+)\s+Draft\s+(\d+)", re.I)
# "ORD 2026-001 2024-2026" — ordinances are numbered year-sequence, no drafts.
_ORDNAME_RE = re.compile(r"\bORD\s+(\d{4})-(\d+)", re.I)
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
# Term folder names: "2024-2026" (councils) or "2026" (ordinance years).
_TERM_RE = re.compile(r"^(\d{4})(?:\s*-\s*(\d{4}))?$")

_TEMPLATE_BILL = "Bill/Resolution"
_TEMPLATE_ORD = "Ordinances"


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# Courtesy pause between API calls. records.hawaiicounty.gov is a small county
# server; a full backfill is ~18k metadata reads, so it gets a deliberate gap
# rather than being hit as fast as it will answer.
API_DELAY = _env_num("TRACKER_LASERFICHE_DELAY", 0.2)
# Folder listings are paged; 500 keeps a single response manageable.
_PAGE_SIZE = 500


def _norm_key(type_label: str, number: str, term: str | None = None) -> str:
    """Bill key. Hawaii County restarts numbering every council term, so the
    term qualifies the key ("Bill 148 (2024-2026)"); see granicus.hawaii_bill_key.
    Ordinances are numbered year-sequence and are already unique, so they carry
    no term suffix."""
    return hawaii_bill_key(type_label, number, term)


def _iso_from_mdy(s: str) -> str | None:
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    mo, d, y = m.groups()
    y = int(y)
    if y < 100:
        # Action text writes two-digit years ("passes second reading - 3/4/89").
        # Now that the whole archive back to 1969 is indexed, a naive +2000
        # would date a 1989 action to 2089; nothing here is ever in the future,
        # so pivot anything implausible back a century.
        y += 2000
        if y > date.today().year + 1:
            y -= 100
    try:
        return date(y, int(mo), int(d)).isoformat()
    except ValueError:
        return None


def _term_end_year(name: str) -> int | None:
    """Last calendar year a term folder covers ("2022-2024" -> 2024), or None
    for a folder whose name is not a year/term (e.g. "MAPS AND LARGE
    ATTACHMENTS")."""
    m = _TERM_RE.match(name.strip())
    if not m:
        return None
    return int(m.group(2) or m.group(1))


class _Doc(NamedTuple):
    doc_id: str
    type_label: str
    number: str
    term: str | None
    draft: int
    template: str


class HawaiiCountyAdapter(CouncilAdapter):
    council_id = "hawaii"

    def __init__(
        self,
        term_year: int | None = None,
        min_year: int | None = None,
        categories: tuple[str, ...] | None = None,
        delay: float | None = None,
        agenda_store=None,
    ):
        # Passed through to the Granicus adapter this borrows titles from, so
        # Hawaii County's ~450 in-window agendas are cached rather than
        # re-rendered on every nightly run. See GranicusAdapter.agenda_store.
        self.agenda_store = agenda_store
        # term_year is retained for callers that pin a term; the index itself is
        # no longer scoped to one term.
        self.term_year = term_year or date.today().year
        # Oldest term year to walk. None = derive it from the retention window
        # (see granicus.window_start); set it to override that floor.
        self.min_year = min_year
        self.categories = categories or tuple(_CATEGORY_STARTID)
        self.delay = API_DELAY if delay is None else delay
        self._s: requests.Session | None = None
        self._repo = _REPO_FALLBACK

    # ---- HTTP --------------------------------------------------------------

    def _session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            # WebLink answers anonymous API calls with a 302 to the sign-in page
            # unless the SPA's own header is present.
            "X-Lf-Suppress-Login-Redirect": "1",
            "Referer": urljoin(_BASE, "Browse.aspx?startid=50&dbid=0"),
        })
        s.verify = False
        # Establish the CookieCheck session, and read the repository name out of
        # the SPA bootstrap rather than hardcoding it.
        r = s.get(urljoin(_BASE, "Browse.aspx?startid=50&dbid=0"), timeout=30)
        m = re.search(r'"repoName"\s*:\s*"([^"]+)"', r.text)
        if m:
            self._repo = m.group(1)
        return s

    def _api(self, method: str, payload: dict) -> dict:
        if self.delay:
            time.sleep(self.delay)
        r = self._s.post(urljoin(_BASE, method), data=json.dumps(payload), timeout=60)
        r.raise_for_status()
        data = r.json().get("data") or {}
        if data.get("failed"):
            raise RuntimeError(f"{method} failed: {data.get('errMsg')}")
        return data

    # ---- navigation --------------------------------------------------------

    def _children(self, folder_id: int) -> list[dict]:
        """Every direct child of a folder, following the listing's paging."""
        out: list[dict] = []
        start = 0
        while True:
            data = self._api(
                "FolderListingService.aspx/GetFolderListing2",
                {
                    "repoName": self._repo, "folderId": folder_id,
                    "getNewListing": start == 0,
                    "start": start, "end": start + _PAGE_SIZE,
                    "sortColumn": "", "sortAscending": True,
                },
            )
            rows = data.get("results") or []
            if not rows:
                break
            # The row `data` array is positional; resolve the template column by
            # name from colTypes rather than trusting a fixed index.
            cols = [c.get("name") for c in (data.get("colTypes") or [])]
            ti = cols.index("TemplateName") if "TemplateName" in cols else None
            for row in rows:
                vals = row.get("data") or []
                row["_template"] = (
                    vals[ti] if ti is not None and ti < len(vals) else None
                ) or ""
                out.append(row)
            total = data.get("totalEntries") or 0
            start += len(rows)
            if start >= total:
                break
        return out

    def _term_folders(self, startid: int) -> list[tuple[str, int]]:
        """[(term_name, folder_id)] for EVERY year/term folder in a category,
        newest last. The old code resolved only the current term."""
        folders = [
            (c.get("name") or "", c["entryId"])
            for c in self._children(startid)
            if c.get("type") == 0
        ]
        if self.min_year is not None:
            folders = [
                (n, i) for n, i in folders
                if (_term_end_year(n) or 0) >= self.min_year
            ]
        return sorted(folders)

    def _list_docs(self, folder_id: int) -> list[tuple[str, str, str]]:
        """[(doc_id, name, template)] for the documents directly in a folder.

        Subfolders are not followed: the only one that exists ("Word Documents")
        holds untemplated .doc duplicates of the same bills.
        """
        return [
            (str(c["entryId"]), (c.get("name") or "").strip(), c.get("_template") or "")
            for c in self._children(folder_id)
            if c.get("type") != 0
        ]

    def _metadata(self, doc_id: str) -> dict[str, str]:
        data = self._api(
            "FolderListingService.aspx/GetMetaData",
            {"repoName": self._repo, "entryId": int(doc_id)},
        )
        out: dict[str, str] = {}
        for f in data.get("fInfo") or []:
            name = (f.get("name") or "").strip()
            vals = [v for v in (f.get("values") or []) if v]
            if name:
                out[name] = "; ".join(str(v).strip() for v in vals)
        return out

    # ---- record assembly ---------------------------------------------------

    def _build_record(
        self, meta: dict[str, str], doc_id: str, term: str | None = None
    ) -> BillRecord | None:
        if (meta.get("Ordinances - Type") or "").strip().upper() == "ORD":
            return self._build_ordinance(meta, doc_id)

        type_code = (meta.get("Bill/Resolution - Type") or "").strip().upper()
        type_label = _TYPE_LABEL.get(type_code)
        number = (meta.get("Bill/Resolution") or "").strip()
        if not type_label or not number.isdigit():
            return None
        term = (meta.get("Bill/Resolution - Council Term") or "").strip() or term

        actions = sorted(
            ((int(k.split()[1]), v) for k, v in meta.items()
             if re.fullmatch(r"Action \d+", k) and v),
            key=lambda x: x[0],
        )
        last_action = actions[-1][1] if actions else None
        last_action_date = _iso_from_mdy(last_action) if last_action else None
        key = _norm_key(type_label, number, term)
        # The template's numbered Action fields ARE the dated action history;
        # surface them so the dashboard timeline shows the full progression
        # (ordered oldest-first by the field number).
        action_records = [
            ActionRecord(
                council=self.council_id,
                bill_number=key,
                action_date=_iso_from_mdy(text) or "",
                action=text.strip(),
            )
            for _, text in actions
        ]

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
            bill_number=key,
            title=None,
            bill_type=type_label,
            introducer=(meta.get("Introducer") or "").strip() or None,
            introduced_date=intro_date,
            status=status,
            last_action=last_action,
            last_action_date=last_action_date,
            url=urljoin(_BASE, f"DocView.aspx?id={doc_id}&dbid=0"),
            raw_subject=(meta.get("Referred To") or "").strip() or None,
            actions=action_records,
        )

    def _build_ordinance(self, meta: dict[str, str], doc_id: str) -> BillRecord | None:
        """Enacted ordinances carry a much thinner template — no introducer,
        status, or action history, and their number ("2026-001") is already
        globally unique, so no term suffix."""
        year = (meta.get("Year") or "").strip()
        seq = (meta.get("Ordinance") or "").strip()
        if not (year.isdigit() and seq):
            return None
        eff = _iso_from_mdy(meta.get("Effective Date", ""))
        return BillRecord(
            council=self.council_id,
            bill_number=f"Ordinance {year}-{seq}",
            title=None,
            bill_type="Ordinance",
            introducer=None,
            introduced_date=None,
            status="Enacted",
            last_action=f"Effective {eff}" if eff else None,
            last_action_date=eff,
            url=urljoin(_BASE, f"DocView.aspx?id={doc_id}&dbid=0"),
            raw_subject=None,
            actions=[],
        )

    # ---- indexing ----------------------------------------------------------

    def _doc_index(self, since: date | None = None) -> dict[str, _Doc]:
        """Index of every document in the retention window, keyed by bill key.

        Every document in range is indexed — not just the handful that turned up
        on a recent agenda — but the walk stops at the window rather than
        descending the whole archive (bills to 1969, ordinances to 1905).
        For bills/resolutions the highest-numbered draft wins.
        """
        floor = self.min_year if self.min_year is not None else window_start(since).year
        index: dict[str, _Doc] = {}
        for type_label in self.categories:
            startid = _CATEGORY_STARTID.get(type_label)
            if startid is None:
                continue
            try:
                folders = self._term_folders(startid)
            except Exception as e:
                log.warning("hawaii term folders (%s) failed: %s", type_label, e)
                continue
            before, walked = len(index), 0
            for term, folder_id in folders:
                # A term that ended before the window opened holds nothing we
                # keep; skip the listing entirely. Terms that straddle it (a
                # 2022-2024 term against a 2023 floor) are walked in full —
                # they hold bills still moving inside the window.
                end = _term_end_year(term)
                if end is not None and end < floor:
                    continue
                try:
                    docs = self._list_docs(folder_id)
                except Exception as e:
                    log.warning("hawaii folder %s (%s) failed: %s", term, type_label, e)
                    continue
                walked += 1
                for doc_id, name, template in docs:
                    self._index_doc(index, doc_id, name, template, term)
            log.info(
                "hawaii index: %s -> %d across %d term folders",
                type_label, len(index) - before, walked,
            )
        return index

    @staticmethod
    def _index_doc(
        index: dict[str, _Doc], doc_id: str, name: str, template: str, term: str
    ) -> None:
        m = _DOCNAME_RE.search(name)
        if m:
            code, num, draft = m.group(1).upper(), m.group(2), int(m.group(3))
            label = _TYPE_LABEL.get(code, code)
            key = _norm_key(label, num, term)
            cur = index.get(key)
            if cur is None or draft > cur.draft:
                index[key] = _Doc(doc_id, label, num, term, draft, template or _TEMPLATE_BILL)
            return
        o = _ORDNAME_RE.search(name)
        if o:
            year, seq = o.group(1), o.group(2)
            key = f"Ordinance {year}-{seq}"
            if key not in index:
                index[key] = _Doc(
                    doc_id, "Ordinance", f"{year}-{seq}", term, 0, template or _TEMPLATE_ORD
                )

    def _active_from_granicus(self, since: date | None) -> dict[str, BillRecord]:
        active: dict[str, BillRecord] = {}
        try:
            gran = GranicusAdapter.for_council("hawaii", agenda_store=self.agenda_store)
            for b in gran.fetch_bills(since=since):
                active[b.bill_number] = b
        except Exception as e:
            log.warning("Granicus active-set fetch failed: %s", e)
        return active

    # ---- public API --------------------------------------------------------

    def fetch_bills(self, since: date | None = None) -> Iterator[BillRecord]:
        active = self._active_from_granicus(since)

        try:
            self._s = self._session()
            index = self._doc_index(since=since)
        except Exception as e:
            log.warning("Laserfiche unreachable (%s); using Granicus-only data", e)
            yield from active.values()
            return

        if not index:
            log.warning("Laserfiche index empty; using Granicus-only data for hawaii")
            yield from active.values()
            return

        log.info("hawaii: %d Laserfiche docs, %d Granicus titles", len(index), len(active))
        seen: set[str] = set()
        for key, doc in index.items():
            gbill = active.get(key)
            try:
                rec = self._build_record(self._metadata(doc.doc_id), doc.doc_id, doc.term)
            except Exception as e:
                log.warning("hawaii metadata (%s) failed: %s", key, e)
                rec = None
            if rec is None:
                # No usable template — fall back to the agenda record if this
                # bill happened to appear on one, else skip.
                if gbill:
                    seen.add(key)
                    yield gbill
                continue
            # Both keys are marked seen: when the document's own Council Term
            # disagrees with its folder, the record is keyed off the metadata,
            # and the agenda record filed under the folder key must not then be
            # re-emitted below as an "agenda-only" bill.
            seen.add(key)
            seen.add(rec.bill_number)
            if gbill is None and rec.bill_number != key:
                gbill = active.get(rec.bill_number)
            if gbill is not None:
                # Laserfiche is authoritative for metadata; Granicus supplies the
                # descriptive title and the agenda staff summary (raw_subject
                # falls back to the title when no summary was found).
                rec.title = gbill.title
                rec.raw_subject = gbill.raw_subject or gbill.title or rec.raw_subject
            yield rec

        # On an agenda but not (yet) filed in Laserfiche.
        for key, gbill in active.items():
            if key not in seen:
                yield gbill

    def fetch_actions(self, bill_number: str) -> Iterator[ActionRecord]:
        return iter(())
