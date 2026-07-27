"""Granicus adapter — for councils whose only structured source is their
Granicus meeting agendas (Hawaii County and Kauai).

Neither council has a usable bill API: their Legistar tenants are unprovisioned
and their .gov sites are behind an Akamai WAF that blocks headless requests.
But both publish meeting agendas on Granicus, and bills/resolutions appear in
the agenda text with their numbers, titles, and reading stage. We drive a real
headless browser (the WAF/Granicus tolerate Chromium far better than bare HTTP)
to read each recent agenda and extract legislation.

Two agenda render modes:
  - "html": AgendaViewer.php returns an HTML page (Kauai)
  - "pdf":  AgendaViewer.php returns a generated PDF (Hawaii County)

Both are text-extractable. We list recent meetings from ViewPublisher.php,
read each agenda, and pull out Bill/Resolution items. A bill can appear across
several meetings as it advances; we keep the most recent appearance (its
section heading gives the latest reading stage).
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime
from typing import Iterator

from tracker.legislative.adapters.base import (
    ActionRecord,
    BillRecord,
    CouncilAdapter,
)

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# The tracker keeps a rolling window rather than the whole archive (the
# retention policy lives in scrape.py; this is the scrape-side half of it). An
# explicit `since` from the caller always wins — this only supplies the floor
# when the caller asks for "everything you hold", which would otherwise mean
# 2010 for Kauai and 1905 for Hawaii County.
DEFAULT_WINDOW_YEARS = int(_env_num("TRACKER_WINDOW_YEARS", 3))

# Safety cap on a single crawl. The `since` window above is the real bound;
# this only stops a runaway if a publisher view balloons. Measured 2026-07,
# distinct agendas inside a 3-year window: Kauai 190, Hawaii County 452 (it
# publishes committee meetings separately, so it lists far more than Kauai).
# 600 clears both with headroom while staying well under the 1,173 / 1,591
# agendas the views list in full. Exceeding it logs a warning rather than
# silently truncating.
DEFAULT_MAX_MEETINGS = int(_env_num("TRACKER_GRANICUS_MAX_MEETINGS", 600))

# Courtesy pause between agenda fetches. Each fetch already costs ~2s (page
# load or PDF render), so this is a small extra margin rather than the main
# rate limiter.
AGENDA_DELAY = _env_num("TRACKER_GRANICUS_DELAY", 0.25)

# A meeting row on ViewPublisher carries a date like "May 27, 2026" and an
# AgendaViewer link. We pair each agenda link with the nearest date on its row.
_AGENDA_RE = re.compile(r"AgendaViewer\.php\?[^\"']*\b(?:clip_id|event_id)=\d+", re.I)
# The agenda's own id, independent of which publisher view links to it.
_CLIP_ID_RE = re.compile(r"\b(?:clip_id|event_id)=(\d+)", re.I)
_DATE_PATTERNS = [
    re.compile(r"[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}"),          # May 27, 2026
    re.compile(r"\d{1,2}/\d{1,2}/\d{4}"),                         # 05/27/2026
]

# Legislation references inside agenda text. Hawaii County agendas abbreviate
# resolutions as "Res. 556-26: <TITLE>" — without the abbreviation the whole
# resolution half of that council goes titleless, so "Res." is matched too. The
# trailing period is required: bare "Res" is too easy to hit inside other words.
_BILL_RE = re.compile(
    r"\b(Bill|Resolution|Reso|Res\.)\s*(?:No\.?\s*)?(\d{2,4}(?:-\d{1,4})?)\b", re.I
)
# Section headers that indicate a reading stage / status.
_STAGE_RE = re.compile(
    r"(BILLS?\s+FOR\s+FIRST\s+READING"
    r"|BILLS?\s+FOR\s+SECOND\s+(?:AND\s+FINAL\s+)?READING"
    r"|BILLS?\s+FOR\s+SECOND\s+READING"
    r"|RESOLUTION[S]?\b"
    r"|BILLS?\b"
    r"|COMMITTEE\s+REPORTS?"
    r"|UNFINISHED\s+BUSINESS)",
    re.I,
)
_STAGE_LABELS = {
    "first reading": "First Reading",
    "second": "Second Reading",
    "committee": "In Committee",
    "unfinished": "Unfinished Business",
}


def _stage_label(header: str) -> str | None:
    h = header.lower()
    if "first reading" in h:
        return "First Reading"
    if "second" in h:
        return "Second Reading"
    if "committee" in h:
        return "In Committee"
    if "unfinished" in h:
        return "Unfinished Business"
    return None


def _parse_date(s: str) -> str | None:
    s = s.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def window_start(since: date | None, years: int | None = None) -> date:
    """The oldest date a scrape should reach back to.

    A caller's explicit `since` is honoured as-is. `since=None` means "whatever
    you hold", which for these archives is decades — so it resolves to the
    retention window instead.
    """
    if since is not None:
        return since
    yrs = DEFAULT_WINDOW_YEARS if years is None else years
    today = date.today()
    try:
        return today.replace(year=today.year - yrs)
    except ValueError:            # Feb 29
        return today.replace(year=today.year - yrs, day=28)


# --- Hawaii County: council terms disambiguate repeated bill numbers ---------
# Hawaii County restarts its bill AND resolution numbering at 1 every two-year
# council term, so "Bill 148" names a different bill in each of the ~25 terms
# the Laserfiche archive holds. bills.db is UNIQUE(council, bill_number), so a
# bare "Bill 148" would collapse them all into one garbled row. Every Hawaii
# bill/resolution key therefore carries its term: "Bill 148 (2024-2026)".
#
# Qualifying *every* term (rather than leaving the current one bare) keeps keys
# stable across a term rollover — otherwise today's "Bill 148" would have to be
# silently re-keyed each December of an even year, and the incoming term's
# Bill 148 would land on top of it.
#
# Terms are seated the December after each even-year general election, so
# December of an even year already belongs to the term that year opens.
_HI_YEAR_SUFFIX_RE = re.compile(r"^(\d{1,4})-(\d{2})$")


def hawaii_term(year: int, month: int) -> str:
    start = year if (year % 2 == 0 and month == 12) else (year - 1 if year % 2 else year - 2)
    return f"{start}-{start + 2}"


def hawaii_term_for_date(iso_date: str | None) -> str | None:
    """Council term ('2024-2026') containing a meeting date, or None if unknown."""
    if not iso_date or len(iso_date) < 7:
        return None
    try:
        return hawaii_term(int(iso_date[:4]), int(iso_date[5:7]))
    except ValueError:
        return None


def hawaii_bill_key(bill_type: str, number: str | int, term: str | None) -> str:
    """Term-qualified key for a Hawaii County bill/resolution."""
    n = str(number)
    base = f"{bill_type} {int(n)}" if n.isdigit() else f"{bill_type} {n}"
    return f"{base} ({term})" if term else base


def split_hawaii_number(num: str) -> str:
    """Strip the adoption-year suffix Hawaii agendas append to resolution
    numbers ("Res. 585-26" is resolution 585, adopted in 2026 — Laserfiche
    files it as plain RES 585 within its term). Bills are never suffixed."""
    m = _HI_YEAR_SUFFIX_RE.match(num)
    return m.group(1) if m else num


# PDF text extraction (both pypdf and the Granicus HTML viewer) drops "ff"/"fi"
# ligatures, leaving a gap mid-word: "Affordable" -> "Af ordable", "Office" ->
# "Of ice". NFKC expands any surviving ligature glyphs (ﬀﬁﬂ…); the explicit map
# repairs the gap cases we've actually seen (kept conservative — a broad
# space-removal rule would merge legitimately separate words).
_LIG_REPAIRS = {
    "Af ordable": "Affordable", "af ordable": "affordable",
    "Of ice": "Office", "of ice": "office",
}


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    for bad, good in _LIG_REPAIRS.items():
        if bad in text:
            text = text.replace(bad, good)
    return re.sub(r"\s+", " ", text).strip()


# A bill's agenda title runs until the procedural note / next agenda section.
# Without trimming, the title bleeds into following content (committee
# sections, minutes, item numbers), e.g. "...REAL PROPERTY TAX (Long-Term
# Affordable Rental Requirements) (Public Hearing held on May 20, 2026) 10.
# B. COMMITTEE OF THE WHOLE ...".
_HEARING_RE = re.compile(r"\s*\(Public Hearing[^)]*\).*$", re.I)
_SECTION_RE = re.compile(
    r"\s+(?:[A-Z]\.\s+(?:COMMITTEE|EXECUTIVE|PUBLIC|UNFINISHED|NEW|OLD|ADJOURN"
    r"|APPROVAL|MINUTES|COMMUNICATIONS?|REPORTS?|CONSENT)"
    r"|COMMITTEE OF THE WHOLE|EXECUTIVE SESSION|\d+\.\s+Minutes\b).*$",
    re.I,
)
_TRAIL_NUM_RE = re.compile(r"\s*\b\d{1,3}\.\s*$")


def _clean_agenda_title(t: str) -> str:
    t = _HEARING_RE.sub("", t)            # drop "(Public Hearing …)" + trailing bleed
    t = _SECTION_RE.sub("", t)            # drop bled-in agenda sections
    t = _TRAIL_NUM_RE.sub("", t)          # drop trailing agenda item number
    t = re.sub(r"(\w)- (\w)", r"\1-\2", t)  # rejoin hyphen-split words ("Long- Term")
    return re.sub(r"\s{2,}", " ", t).strip(" -–—:.,")


_TITLE_KEYWORDS = re.compile(
    r"\b(ORDINANCE|RESOLUTION|BILL|AMEND|RELAT|ESTABLISH|APPROV|AUTHORIZ"
    r"|PROVID|REPEAL|CHARTER|BUDGET|APPROPRIAT|ADOPT|DESIGNAT|GRANT|CREAT"
    r"|URG|CONFIRM|ACQUIR|INITIAT)\w*",
    re.I,
)


# --- Kauai agenda titles -----------------------------------------------------
# Kauai agendas (rendered PDFs read as text) place a bill/resolution two ways:
#   * Direct action item:  "5. Bill No. 2988 A BILL FOR AN ORDINANCE ...
#     (Public Hearing held on ...)"  — title FOLLOWS the number and opens with a
#     legislative lead-in.
#   * Communication referral: "C 2026-105 Communication ... transmitting for
#     Council consideration, a Resolution Authorizing ... (See Resolution No.
#     2026-16)" — title PRECEDES the number, inside the transmittal phrase.
# Bare cross-references ("Public Hearing re: Bill No. 2989", "(See Resolution
# No. 2026-17)", "... relating to Bill No. 2988, the Mayor's ...") put unrelated
# text after the number; those must yield no title (the real title is captured
# from the agenda where the item appears directly).
_KAUAI_LEADIN_RE = re.compile(
    r'["“]?\s*'
    r"(?:A\s+BILL\s+(?:FOR|TO)\b|AN?\s+ORDINANCE\b|A\s+RESOLUTION\b|RESOLUTION\b)",
    re.I,
)
_KAUAI_RECOVER_RE = re.compile(
    r"transmitting for Council consideration,?\s+(?:a|an|the)\s+"
    r"(?:Resolution|Bill)\s+(.+?)\.?\s*\(See\b[^()]*$",
    re.I | re.S,
)
# Where a Kauai title ends: procedural note, status bracket, see-reference,
# boilerplate header, page footer, or the next lettered agenda section.
_KAUAI_END_RE = re.compile(
    r"\(Public Hearing\b"
    r"|\[[^\]]*\]"
    r"|\(See\b"
    r"|MEETING INFORMATION"
    r"|COUNTY COUNCIL\b"
    r"|\bPage\s+\d+\s+of\s+\d+"
    r"|(?<=\s)[A-Z]\.\s+(?:BILL|RESOLUTION|COMMITTEE|EXECUTIVE|PUBLIC"
    r"|COMMUNICATIONS?|CONSENT|CLAIMS?|MINUTES|MEETING|APPROVAL|ROLL"
    r"|ADJOURN|NEW|OLD|UNFINISHED|REPORTS?)",
    re.S,
)


def _kauai_trim(t: str) -> str:
    t = _BILL_RE.split(t)[0]                 # stop at the next bill/resolution
    end = _KAUAI_END_RE.search(t)
    if end:
        t = t[: end.start()]
    return t.strip(' "“”')


def _clean_kauai_title(flat: str, m: re.Match) -> str | None:
    """Title for a Kauai bill/resolution number matched at `m` in `flat`, or
    None if the match is a bare cross-reference (no real title to extract)."""
    pre = flat[: m.start()]
    # Communication referral: the number sits in a "(See ... No. NNNN)" pointer;
    # recover the transmittal title that precedes it. Referrals are listed
    # back-to-back, each ending in its own pointer, so isolate THIS one (the text
    # since the previous "(See …)") — otherwise a neighbor's title is grabbed.
    if re.search(r"\(See\b[^()]*$", pre[-60:]):
        seg = re.split(r"\(See\b[^)]*\)", pre)[-1]
        rec = _KAUAI_RECOVER_RE.search(seg)
        return _kauai_trim(rec.group(1)) if rec else None
    cand = _clean(flat[m.end(): m.end() + 600]).lstrip("-–—:.,) ")
    # Quoted title: take the quoted span (cuts trailing boilerplate cleanly).
    q = re.match(r'["“](.+?)["”]', cand)
    if q:
        return _kauai_trim(q.group(1))
    # Otherwise the text after the number must read like a real title.
    if not _KAUAI_LEADIN_RE.match(cand):
        return None
    return _kauai_trim(cand)


# --- Hawaii County agenda titles ---------------------------------------------
# Hawaii County agenda PDFs read as: "Bill 156: <ALL-CAPS LEGAL TITLE>
# <Title-case staff summary.> Reference: Comm. NNN  Intr. by: ...  <footer>".
# The clean title is the leading ALL-CAPS run; case is the discriminator — the
# summary and metadata start in Title/sentence case while the title stays caps
# (including mixed-case-looking spans like "(2016 EDITION, AS AMENDED)").
_HI_LEAD_RE = re.compile(r"^\s*(?:ORDER OF RESOLUTIONS|ORDER OF THE DAY)\s+", re.I)
_HI_XREF_RE = re.compile(r"^\s*(?:Bill|Resolution|Reso|Res)\.?\s+(?:No\.?\s*)?\d", re.I)
_HI_TRAILER_RE = re.compile(
    r"\s+(?:Reference:|Intr\.\s*by:|Approve:|Negative:|Positive:|Postpone"
    r"|2/3\s*Vote:|Draft\s+\d|Hawai.i\s+County\s+Council|Page\s+\d+).*$",
    re.I | re.S,
)
# Hawaii titles are uniformly ALL CAPS; the first Title-case word (a capital
# followed by a lowercase letter) marks where the title ends and prose begins —
# the staff summary ("Requires a …", "Adds the …"), a communication attribution
# ("From Mayor …, dated …, transmitting …"), or a stray "Draft 2)".
_HI_DESC_RE = re.compile(r"\s+[A-Z][a-z].*$", re.S)
# Where the staff summary ends: reference/introducer/vote metadata, an attached
# communication ("; and Comm. 754.11: (Memo No. 1) From …"), an agenda note, or
# the page footer.
_HI_SUMMARY_END_RE = re.compile(
    r"\s*(?:;\s*and\b|\(?Comm\.\s*(?:No\.?\s*)?\d|\(NOTE\b|\(Memo\b"
    r"|Reference:|Intr\.\s*by:|Approve:|Negative:|Positive:|Postpone"
    r"|2/3\s*Vote:|Public Hearing:|First Reading:|Second Reading:"
    r"|Hawai.i\s+County\s+Council|Page\s+\d+).*$",
    re.I | re.S,
)


def _hawaii_summary(raw: str) -> str | None:
    """The Title-case staff summary that follows a Hawaii County ALL-CAPS
    title ("Draft 3 includes estimated revenues of …", "Adds a new article
    to regulate …"), or None when what follows is metadata or an attached
    communication rather than a summary of the bill itself."""
    t = _HI_LEAD_RE.sub("", raw)
    m = _HI_DESC_RE.search(t)
    if not m:
        return None
    s = _HI_SUMMARY_END_RE.sub("", m.group(0).strip()).strip()
    # "From Council Member …, dated …, transmitting …" is a communication
    # attribution, not a description of the bill.
    if re.match(r"From\b", s):
        return None
    if len(s) < 25 or len(s.split()) < 5:
        return None
    return s[:600].rstrip()


def _clean_hawaii_title(raw: str) -> str | None:
    t = _HI_LEAD_RE.sub("", raw)
    if _HI_XREF_RE.match(t):       # title belongs to a different (referenced) item
        return None
    # A real title opens with an ALL-CAPS legislative verb ("AMENDS",
    # "ESTABLISHES", "RELATES", …). Other agendas dump budget detail after the
    # number ("(Draft 2) for fiscal year … SUMMARY OF REVENUES …" / "Draft 2. ;
    # and Comm. …") — reject anything not starting in ALL CAPS.
    if not re.match(r'^["“\'(]*[A-Z]{2,}\b', t):
        return None
    t = _HI_DESC_RE.sub("", t)     # drop the Title-case staff summary
    t = _HI_TRAILER_RE.sub("", t)  # drop Reference:/Intr. by:/footer metadata
    return t.strip()


def _looks_like_title(t: str) -> bool:
    """A real agenda item title vs. an incidental cross-reference."""
    if not t or len(t) < 15:
        return False
    if len(t.split()) < 4:
        return False
    return bool(_TITLE_KEYWORDS.search(t))


class GranicusAdapter(CouncilAdapter):
    def __init__(
        self,
        council_id: str,
        host: str,
        view_ids: list[int],
        mode: str = "html",
        max_meetings: int | None = None,
        delay: float | None = None,
    ):
        self.council_id = council_id
        self.host = host
        self.view_ids = view_ids
        self.mode = mode
        self.max_meetings = DEFAULT_MAX_MEETINGS if max_meetings is None else max_meetings
        self.delay = AGENDA_DELAY if delay is None else delay

    @classmethod
    def for_council(cls, council_id: str, **kw) -> "GranicusAdapter":
        """Granicus agenda config per council, kept in one place so the scraper,
        the Hawaii County Laserfiche adapter, and the dump-agendas CLI agree.

        max_meetings/delay default to the module-level settings (overridable via
        TRACKER_GRANICUS_MAX_MEETINGS / TRACKER_GRANICUS_DELAY) so a caller can
        bound an exploratory crawl without editing this table.
        """
        if council_id == "kauai":
            return cls("kauai", "kauai.granicus.com", [2], mode="html", **kw)
        if council_id == "hawaii":
            return cls("hawaii", "hawaiicounty.granicus.com", [1, 2], mode="pdf", **kw)
        raise ValueError(f"no Granicus config for council: {council_id}")

    # ---- keys --------------------------------------------------------------

    def _bill_key(self, bill_type: str, num: str, meeting_date: str) -> str:
        """Hawaii County reuses bill/resolution numbers every council term, so
        its keys are term-qualified (see hawaii_bill_key). Kauai numbers run
        continuously (Bill 2988) or already carry a year (Resolution 2026-11),
        so they are used as-is."""
        if self.council_id != "hawaii":
            return f"{bill_type} {num}"
        return hawaii_bill_key(
            bill_type, split_hawaii_number(num), hawaii_term_for_date(meeting_date)
        )

    # ---- meeting discovery -------------------------------------------------

    def _list_meetings(self, page, view_id: int) -> list[tuple[str, str]]:
        """Return [(iso_date, agenda_url)] for one publisher view.

        Layout varies by tenant (Hawaii County uses table rows, Kauai doesn't),
        so for each agenda link we read the nearest sensible container for its
        meeting date rather than assuming a <tr>.
        """
        url = f"https://{self.host}/ViewPublisher.php?view_id={view_id}"
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            page.wait_for_timeout(1500)
        rows = page.eval_on_selector_all(
            "a",
            """els => els
                .filter(a => /AgendaViewer\\.php/.test(a.href))
                .map(a => {
                    const box = a.closest('tr, li, .listingRow, .row') || a.parentElement?.parentElement || a.parentElement;
                    return { href: a.href, row: (box ? box.innerText : '') || '' };
                })""",
        )
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for r in rows:
            if not _AGENDA_RE.search(r["href"]) or r["href"] in seen:
                continue
            seen.add(r["href"])
            iso = None
            for pat in _DATE_PATTERNS:
                m = pat.search(r["row"])
                if m:
                    iso = _parse_date(m.group(0))
                    if iso:
                        break
            out.append((iso or "", r["href"]))
        return out

    # ---- agenda fetch ------------------------------------------------------

    @staticmethod
    def _pdf_text(ctx, agenda_url: str) -> str:
        from pypdf import PdfReader

        resp = ctx.request.get(agenda_url, timeout=45000)
        body = resp.body()
        if body[:4] != b"%PDF":
            return ""
        reader = PdfReader(io.BytesIO(body))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)

    def _agenda_text(self, ctx, page, agenda_url: str) -> str:
        if self.mode == "pdf":
            return self._pdf_text(ctx, agenda_url)
        # html mode
        try:
            page.goto(agenda_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(800)
            return page.inner_text("body")
        except Exception:
            # Kauai's tenant renders recent agendas as HTML but serves older
            # ones as a PDF download, which navigation aborts on ("Download is
            # starting"). Uncapping the crawl walks straight into those, so fall
            # back to reading the response as a PDF before giving up. A failure
            # in the fallback must not mask the navigation error.
            try:
                text = self._pdf_text(ctx, agenda_url)
            except Exception:
                text = ""
            if text:
                return text
            raise

    # ---- agenda parsing ----------------------------------------------------

    def _parse_agenda(
        self, text: str, meeting_date: str, agenda_url: str
    ) -> list[dict]:
        """Yield mention dicts for bill numbers whose following text reads like
        a real legislative title. Incidental cross-references (a number listed
        in a sentence, a minutes line, etc.) are skipped — their trailing text
        won't pass _looks_like_title."""
        flat = _clean(text)
        out: list[dict] = []
        for m in _BILL_RE.finditer(flat):
            kind = m.group(1).lower()
            num = m.group(2)
            bill_type = "Resolution" if kind.startswith("res") else "Bill"
            bill_number = self._bill_key(bill_type, num, meeting_date)

            # Title extraction is source-shaped: Hawaii County (pdf) trims a
            # Title-case staff summary and metadata trailing an ALL-CAPS title;
            # Kauai (html) must tell a real agenda item from a cross-reference
            # and may recover a title that precedes the number.
            summary = None
            if self.mode == "pdf":
                window = _BILL_RE.split(flat[m.end(): m.end() + 900])[0]
                raw = _clean(window).lstrip("-–—:.,) ")
                raw = re.sub(r"^\(Draft\s+\d+\)\s*", "", raw, flags=re.I).strip()
                title = _clean_hawaii_title(raw)
                if title:
                    summary = _hawaii_summary(raw)
            else:
                title = _clean_kauai_title(flat, m)
            if not title:
                continue
            title = _clean_agenda_title(title)[:400].rstrip()
            if not _looks_like_title(title):
                continue

            # Status from the nearest preceding stage header.
            stage = None
            last_hdr = None
            for last_hdr in _STAGE_RE.finditer(flat[: m.start()]):
                pass
            if last_hdr:
                stage = _stage_label(last_hdr.group(0))

            out.append({
                "bill_number": bill_number,
                "bill_type": bill_type,
                "title": title,
                "summary": summary,
                "stage": stage,
                "date": meeting_date,
                "url": agenda_url,
            })
        return out

    # ---- public API --------------------------------------------------------

    def iter_raw_agendas(
        self, since: date | None = None
    ) -> Iterator[tuple[str, str, str]]:
        """Drive a headless browser and yield (meeting_date, agenda_url, raw_text)
        for each recent agenda. The single place that fetches agenda text — both
        fetch_bills() and the `dump-agendas` CLI consume it."""
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=_UA, ignore_https_errors=True)
            page = ctx.new_page()
            try:
                # Views overlap heavily — Hawaii County's view 2 lists the same
                # meetings as view 1 — but each view links the agenda under its
                # own view_id, so the URLs differ for what is one meeting.
                # Dedupe on the clip/event id instead, which identifies the
                # agenda itself; keying on the URL fetches each twice.
                by_clip: dict[str, tuple[str, str]] = {}
                for vid in self.view_ids:
                    try:
                        for mdate, url in self._list_meetings(page, vid):
                            cid = _CLIP_ID_RE.search(url)
                            key = cid.group(1) if cid else url
                            prev = by_clip.get(key)
                            if prev is None or (mdate and not prev[0]):
                                by_clip[key] = (mdate, url)
                    except Exception as e:
                        log.warning("%s view %s listing failed: %s", self.council_id, vid, e)
                by_url = {url: mdate for mdate, url in by_clip.values()}

                # The date window is the real bound: keep meetings on or after
                # it and drop the rest, however many that is. max_meetings is
                # only a backstop against a runaway view.
                floor = window_start(since).isoformat()
                listed = len(by_url)
                meetings = [(d, u) for u, d in by_url.items() if not d or d >= floor]
                meetings.sort(key=lambda m: m[0], reverse=True)
                in_window = len(meetings)
                meetings = meetings[: self.max_meetings]
                if in_window > len(meetings):
                    log.warning(
                        "%s: %d agendas in window since %s but max_meetings=%d truncated "
                        "the crawl — raise TRACKER_GRANICUS_MAX_MEETINGS",
                        self.council_id, in_window, floor, self.max_meetings,
                    )
                log.info(
                    "%s: %d agendas listed, %d since %s, reading %d",
                    self.council_id, listed, in_window, floor, len(meetings),
                )

                for i, (mdate, agenda_url) in enumerate(meetings):
                    if i and self.delay:
                        time.sleep(self.delay)
                    try:
                        text = self._agenda_text(ctx, page, agenda_url)
                    except Exception as e:
                        log.warning("%s agenda fetch failed (%s): %s", self.council_id, agenda_url, e)
                        continue
                    yield mdate, agenda_url, text
            finally:
                browser.close()

    def fetch_bills(self, since: date | None = None) -> Iterator[BillRecord]:
        # Per bill: keep the longest (best) title and summary ever seen, plus
        # the latest meeting date and the stage from that latest meeting.
        merged: dict[str, dict] = {}
        for mdate, agenda_url, text in self.iter_raw_agendas(since=since):
            for men in self._parse_agenda(text, mdate, agenda_url):
                key = men["bill_number"]
                cur = merged.get(key)
                if cur is None:
                    merged[key] = men
                    continue
                # Best (longest) title / summary wins.
                if len(men["title"] or "") > len(cur["title"] or ""):
                    cur["title"] = men["title"]
                if len(men.get("summary") or "") > len(cur.get("summary") or ""):
                    cur["summary"] = men["summary"]
                # Latest meeting drives date / stage / link.
                if (men["date"] or "") >= (cur["date"] or ""):
                    cur["date"] = men["date"]
                    cur["stage"] = men["stage"] or cur["stage"]
                    cur["url"] = men["url"]

        for men in merged.values():
            yield BillRecord(
                council=self.council_id,
                bill_number=men["bill_number"],
                title=men["title"],
                bill_type=men["bill_type"],
                introducer=None,
                introduced_date=None,
                status=men["stage"],
                last_action=f"On agenda {men['date']}" if men["date"] else "On agenda",
                last_action_date=men["date"] or None,
                url=men["url"],
                # The staff summary (Hawaii County agendas carry one after the
                # legal title) is the bill's best plain-English description;
                # fall back to the title where the agenda has no summary.
                raw_subject=men.get("summary") or men["title"],
            )

    def fetch_actions(self, bill_number: str) -> Iterator[ActionRecord]:
        return iter(())
