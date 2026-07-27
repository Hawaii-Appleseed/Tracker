import json
from pathlib import Path

import pytest

from tracker.legislative.adapters.granicus import GranicusAdapter, _looks_like_title


def test_looks_like_title_filters_cross_references():
    assert not _looks_like_title("4.")
    assert not _looks_like_title(", and")
    assert not _looks_like_title("E.")
    assert _looks_like_title("A BILL FOR AN ORDINANCE AMENDING CHAPTER 5A, KAUAI COUNTY CODE")
    assert _looks_like_title("RESOLUTION ESTABLISHING THE REAL PROPERTY TAX RATES FOR THE FISCAL YEAR")


def test_parse_agenda_extracts_titled_bills_only():
    adapter = GranicusAdapter("kauai", "kauai.granicus.com", [2], mode="html")
    agenda = (
        "BILLS FOR SECOND READING "
        "Bill 2995 A BILL FOR AN ORDINANCE AMENDING CHAPTER 5A, KAUAI COUNTY CODE 1987, "
        "RELATING TO REAL PROPERTY TAXES. "
        "Bill 2996 A BILL FOR AN ORDINANCE AMENDING CHAPTER 17 RELATING TO TRANSPORTATION AND BUSES. "
        "The council also discussed Bill 2995 and Bill 2996 during testimony."
    )
    recs = adapter._parse_agenda(agenda, "2026-05-27", "http://x/agenda")
    nums = {r["bill_number"]: r for r in recs}
    assert "Bill 2995" in nums and "Bill 2996" in nums
    assert "REAL PROPERTY TAXES" in nums["Bill 2995"]["title"]
    assert "TRANSPORTATION" in nums["Bill 2996"]["title"]
    # cross-reference mention ("discussed Bill 2995 and Bill 2996") must not
    # overwrite with junk — titles stay intact
    assert nums["Bill 2995"]["title"].startswith("A BILL FOR AN ORDINANCE")


def test_parse_agenda_skips_bare_number_mentions():
    adapter = GranicusAdapter("kauai", "kauai.granicus.com", [2], mode="html")
    agenda = "Minutes approved. Bill 2987, Bill 2988, and Bill 2990 were referred to committee."
    recs = adapter._parse_agenda(agenda, "2026-05-27", "http://x/agenda")
    assert recs == []  # no real titles → nothing extracted


def test_clean_agenda_title_strips_bleed():
    from tracker.legislative.adapters.granicus import _clean_agenda_title
    raw = ("A BILL FOR AN ORDINANCE AMENDING CHAPTER 5A, RELATING TO REAL PROPERTY "
           "TAX (Long- Term Affordable Rental Requirements) (Public Hearing held on "
           "May 20, 2026) 10. B. COMMITTEE OF THE WHOLE C. EXECUTIVE SESSION")
    out = _clean_agenda_title(raw)
    assert out.endswith("(Long-Term Affordable Rental Requirements)")  # hyphen rejoined, bleed gone
    assert "Public Hearing" not in out
    assert "COMMITTEE OF THE WHOLE" not in out
    assert "EXECUTIVE SESSION" not in out


# --- ligature repair ---------------------------------------------------------

def test_clean_repairs_dropped_ligature():
    from tracker.legislative.adapters.granicus import _clean
    # PDF text extraction drops the "ff" ligature mid-word.
    assert _clean("Long-Term Af ordable Rental") == "Long-Term Affordable Rental"
    assert _clean("the Of ice of the County Clerk") == "the Office of the County Clerk"


# --- Hawaii County: ALL-CAPS title vs. Title-case staff summary --------------

from tracker.legislative.adapters.granicus import _clean_hawaii_title

HAWAII_CASES = [
    # (raw window after "Bill NNN:", expected clean title or None to drop)
    ("AMENDS CHAPTER 6 OF THE HAWAI‘I COUNTY CODE 1983 (2016 EDITION, AS AMENDED) "
     "BY ADDING AN ARTICLE RELATING TO PAID PARKING FACILITIES IN KAILUA VILLAGE "
     "Adds a new article to regulate parking rates at private parking facilities",
     "AMENDS CHAPTER 6 OF THE HAWAI‘I COUNTY CODE 1983 (2016 EDITION, AS AMENDED) "
     "BY ADDING AN ARTICLE RELATING TO PAID PARKING FACILITIES IN KAILUA VILLAGE"),
    ("ESTABLISHES AN OPERATING BUDGET FOR THE COUNTY OF HAWAI‘I FOR THE FISCAL YEAR "
     "JULY 1, 2026, TO JUNE 30, 2027 Draft 3 includes estimated revenues of $976,408,620",
     "ESTABLISHES AN OPERATING BUDGET FOR THE COUNTY OF HAWAI‘I FOR THE FISCAL YEAR "
     "JULY 1, 2026, TO JUNE 30, 2027"),
    ("ADOPTS THE COUNTY OF HAWAI‘I GENERAL PLAN 2045 AND REPEALS ORDINANCE NO. 05-025, "
     "AS AMENDED Reference: Comm. 372.30 Intr. by: Council Member",
     "ADOPTS THE COUNTY OF HAWAI‘I GENERAL PLAN 2045 AND REPEALS ORDINANCE NO. 05-025, AS AMENDED"),
    ("RELATES TO PUBLIC IMPROVEMENTS AND FINANCING THEREOF FOR THE FISCAL YEAR JULY 1, "
     "2026, TO JUNE 30, 2027 Draft 3 requires a total appropriation of $380,200,000",
     "RELATES TO PUBLIC IMPROVEMENTS AND FINANCING THEREOF FOR THE FISCAL YEAR JULY 1, "
     "2026, TO JUNE 30, 2027"),
    # outlier: a resolution-order header cross-attributing another item's title -> drop
    ("ORDER OF RESOLUTIONS Res. 575-26: AUTHORIZES THE ACCEPTANCE OF ALL DONATIONS", None),
]


@pytest.mark.parametrize("raw,expected", HAWAII_CASES)
def test_clean_hawaii_title(raw, expected):
    assert _clean_hawaii_title(raw) == expected


def test_hawaii_summary_extracted_from_staff_description():
    from tracker.legislative.adapters.granicus import _hawaii_summary
    raw = (
        "ESTABLISHES AN OPERATING BUDGET FOR THE COUNTY OF HAWAI‘I FOR THE "
        "FISCAL YEAR JULY 1, 2026, TO JUNE 30, 2027 Draft 3 includes estimated "
        "revenues of $976,408,620 and appropriations of $975,980,388. "
        "Reference: Comm. 754.10 Intr. by: Council Member Kānealiʻi-Kleinfelder (B/R) "
        "Public Hearing: May 19, 2026 First Reading: May 21, 2026"
    )
    assert _hawaii_summary(raw) == (
        "Draft 3 includes estimated revenues of $976,408,620 and "
        "appropriations of $975,980,388."
    )


def test_hawaii_summary_stops_before_attached_communication():
    from tracker.legislative.adapters.granicus import _hawaii_summary
    raw = (
        "AMENDS CHAPTER 6 OF THE HAWAI‘I COUNTY CODE 1983 (2016 EDITION, AS "
        "AMENDED) BY ADDING AN ARTICLE RELATING TO PAID PARKING FACILITIES "
        "Adds a new article to regulate parking rates at private parking "
        "facilities. ; and Comm. 754.11: (Memo No. 1) From Council Member "
        "Jenn Kagiwada, dated May 27, 2026, transmitting a proposed amendment"
    )
    assert _hawaii_summary(raw) == (
        "Adds a new article to regulate parking rates at private parking facilities."
    )


def test_hawaii_summary_rejects_communication_attribution():
    from tracker.legislative.adapters.granicus import _hawaii_summary
    # When the Title-case text after the title is a communication attribution
    # rather than a staff summary, there is no summary.
    assert _hawaii_summary(
        "ADOPTS THE COUNTY OF HAWAI‘I GENERAL PLAN 2045 "
        "From Mayor Mitch Roth, dated May 27, 2026, transmitting the plan"
    ) is None
    # Bare metadata after the title -> no summary either.
    assert _hawaii_summary(
        "ADOPTS THE COUNTY OF HAWAI‘I GENERAL PLAN 2045 "
        "Reference: Comm. 372.30 Intr. by: Council Member"
    ) is None


def test_hawaii_parse_agenda_carries_summary():
    ad = GranicusAdapter.for_council("hawaii")
    agenda = (
        "ORDER OF THE DAY (SECOND OR FINAL READING) "
        "Bill 135: (Draft 3) ESTABLISHES AN OPERATING BUDGET FOR THE COUNTY OF "
        "HAWAI‘I FOR THE FISCAL YEAR JULY 1, 2026, TO JUNE 30, 2027 "
        "Draft 3 includes estimated revenues of $976,408,620 and appropriations "
        "of $975,980,388. Reference: Comm. 754.10 Intr. by: Council Member"
    )
    recs = ad._parse_agenda(agenda, "2026-06-04", "http://x")
    by_num = {r["bill_number"]: r for r in recs}
    # Hawaii keys are term-qualified — numbering restarts every council term.
    key = "Bill 135 (2024-2026)"
    assert by_num[key]["summary"].startswith("Draft 3 includes estimated revenues")
    assert by_num[key]["title"].startswith("ESTABLISHES AN OPERATING BUDGET")


def test_hawaii_title_keeps_parenthetical_edition():
    # The mixed-looking "(2016 EDITION, AS AMENDED)" is part of the title, not
    # the staff summary, and must not be trimmed.
    out = _clean_hawaii_title(
        "AMENDS CHAPTER 19, ARTICLE 8, OF THE HAWAIʻI COUNTY CODE 1983 "
        "(2016 EDITION, AS AMENDED), RELATING TO REAL PROPERTY TAXES "
        "Establishes a new real property tax dedication"
    )
    assert out.endswith("(2016 EDITION, AS AMENDED), RELATING TO REAL PROPERTY TAXES")


# --- Kauai: direct items, cross-references, see-pointer recovery --------------

def _kauai(agenda):
    ad = GranicusAdapter("kauai", "kauai.granicus.com", [2], mode="html")
    return {r["bill_number"]: r["title"] for r in ad._parse_agenda(agenda, "2026-05-27", "http://x")}


def test_kauai_direct_item_extracts_clean_title():
    out = _kauai(
        "4. Resolution No. 2026-11 RESOLUTION ESTABLISHING THE REAL PROPERTY TAX "
        "RATES FOR THE FISCAL YEAR JULY 1, 2026 TO JUNE 30, 2027 FOR THE COUNTY OF "
        "KAUA‘I (Public Hearing held on May 13, 2026) "
        "5. Bill No. 2988 A BILL FOR AN ORDINANCE RELATING TO THE OPERATING BUDGET "
        "(Public Hearing held on May 13, 2026)"
    )
    assert out["Resolution 2026-11"] == (
        "RESOLUTION ESTABLISHING THE REAL PROPERTY TAX RATES FOR THE FISCAL YEAR "
        "JULY 1, 2026 TO JUNE 30, 2027 FOR THE COUNTY OF KAUA‘I"
    )
    assert out["Bill 2988"].startswith("A BILL FOR AN ORDINANCE RELATING TO THE OPERATING BUDGET")


def test_kauai_cross_reference_yields_no_title():
    # A number listed in a hearing reference, then unrelated communication text:
    # this must NOT become a title (the real title lives on another agenda).
    out = _kauai(
        "May 13, 2026 Public Hearing re: Resolution No. 2026-11, Bill No. 2988, and "
        "Bill No. 2989 C 2026-128 Communication (05/20/2026) from the Hawai‘i State "
        "Association of Counties (HSAC) President, transmitting for Council "
        "consideration, HSAC’s Fiscal Year 2027 Proposed Operating Budget."
    )
    assert out == {}


def test_kauai_conflict_disclosure_yields_no_title():
    out = _kauai(
        "C 2026-129 Communication (05/22/2026) from Council Vice Chair Kuali‘i, "
        "providing written disclosure of a possible conflict of interest and recusal "
        "relating to Bill No. 2988, the Mayor’s Proposed Operating Budget for Fiscal "
        "Year 2026-2027 regarding the appropriation to the YWCA."
    )
    assert out == {}


def test_kauai_recovers_title_from_communication_referral():
    # Resolutions introduced by communication carry the title before the number,
    # in the "(See Resolution No. NNNN)" pointer.
    out = _kauai(
        "3. C 2026-105 Communication (04/28/2026) from Council Chair Rapozo and "
        "Council Vice Chair Kuali‘i, transmitting for Council consideration, a "
        "Resolution Authorizing The Acquisition Of A Public Pedestrian Beach Access "
        "Easement, And Determining The Necessity Of The Acquisition Thereof By "
        "Eminent Domain (For Condemnation). (See Resolution No. 2026-16)"
    )
    assert "Resolution 2026-16" in out
    assert out["Resolution 2026-16"].startswith("Authorizing The Acquisition Of A Public Pedestrian Beach Access Easement")
    assert "Communication" not in out["Resolution 2026-16"]


def test_kauai_clustered_referrals_are_not_misattributed():
    # Referrals are listed back-to-back, each ending in its own "(See ... No.)"
    # pointer; the pointer for one resolution must get that communication's
    # title, not a neighbor's.
    out = _kauai(
        "2. C 2026-33 Communication from the Housing Director, transmitting for "
        "Council consideration, a Resolution Approving Modifications To The "
        "Preliminary Subdivision Map. (See Resolution No. 2026-04) "
        "3. C 2026-34 Communication from the County Engineer, transmitting for "
        "Council consideration, a Resolution Authorizing Installation Of Speed "
        "Tables On Hauiki Road. (See Resolution No. 2026-05) "
        "4. C 2026-35 Communication, transmitting for Council consideration, a "
        "Resolution Urging The State Legislature To Fund PEG Access. "
        "(See Resolution No. 2026-06)"
    )
    assert out["Resolution 2026-04"].startswith("Approving Modifications")
    assert out["Resolution 2026-05"].startswith("Authorizing Installation Of Speed Tables")
    assert out["Resolution 2026-06"].startswith("Urging The State Legislature")


def test_hawaii_rejects_non_caps_budget_dump():
    # A different agenda dumps budget detail after the number instead of a title;
    # it does not open in ALL CAPS, so it must be rejected (the clean ALL-CAPS
    # title is captured from the agenda where the bill is listed properly).
    assert _clean_hawaii_title(
        "for fiscal year 2026-2027 are as follows: SUMMARY OF REVENUES AND "
        "APPROPRIATIONS BY FUNDS REVENUES General Fund Highway Fund"
    ) is None
    assert _clean_hawaii_title("Draft 2. ; and Comm. 372.195: From Council Member") is None


def test_kauai_quoted_title_drops_meeting_information_bleed():
    # The exact gibberish from the bug report: a quoted title bleeding into the
    # MEETING INFORMATION boilerplate.
    out = _kauai(
        'Resolution No. 2026-11 "Resolution Establishing The Real Property Tax Rates '
        'For The Fiscal Year July 1, 2026 to June 30, 2027 For The County of Kaua‘i." '
        'MEETING INFORMATION: This is an in-person meeting at multiple meeting sites '
        'connected by interactive conference technology.'
    )
    assert out["Resolution 2026-11"] == (
        "Resolution Establishing The Real Property Tax Rates For The Fiscal Year "
        "July 1, 2026 to June 30, 2027 For The County of Kaua‘i"
    )
    assert "MEETING INFORMATION" not in out["Resolution 2026-11"]


# --- fixture regression: parse real captured agendas, compare to snapshot -----

_FIXTURES = Path(__file__).parent / "fixtures" / "agendas"


def _fixture_files():
    return sorted(p.name for p in _FIXTURES.glob("*.txt"))


@pytest.mark.parametrize("fname", _fixture_files())
def test_fixture_titles_match_snapshot(fname):
    expected = json.loads((_FIXTURES / "expected_titles.json").read_text())
    council = fname.split("_", 1)[0]
    ad = GranicusAdapter.for_council(council)
    recs = ad._parse_agenda((_FIXTURES / fname).read_text(), "2026-01-01", "http://x")
    got = {r["bill_number"]: r["title"] for r in recs}
    assert got == expected.get(fname, {})


# --- Hawaii County term qualification ----------------------------------------
# Hawaii restarts bill/resolution numbering every two-year council term, so
# both this adapter and the Laserfiche one key bills by (number, term).

from tracker.legislative.adapters.granicus import (
    DEFAULT_MAX_MEETINGS,
    window_start,
    hawaii_bill_key,
    hawaii_term,
    hawaii_term_for_date,
    split_hawaii_number,
)


@pytest.mark.parametrize("year,month,expected", [
    (2026, 7, "2024-2026"),    # mid-term
    (2025, 3, "2024-2026"),    # odd year is always mid-term
    (2024, 12, "2024-2026"),   # terms are seated the December after the election
    (2024, 6, "2022-2024"),    # ...so earlier in that even year is the old term
    (2025, 12, "2024-2026"),
])
def test_hawaii_term_boundaries(year, month, expected):
    assert hawaii_term(year, month) == expected


def test_hawaii_term_for_date_handles_missing_dates():
    assert hawaii_term_for_date("2026-06-04") == "2024-2026"
    assert hawaii_term_for_date("") is None
    assert hawaii_term_for_date(None) is None


def test_hawaii_bill_key_format():
    assert hawaii_bill_key("Bill", "148", "2024-2026") == "Bill 148 (2024-2026)"
    assert hawaii_bill_key("Bill", "0148", "2024-2026") == "Bill 148 (2024-2026)"
    assert hawaii_bill_key("Bill", "148", None) == "Bill 148"


def test_split_hawaii_number_strips_adoption_year():
    # Hawaii agendas write resolutions as "Res. 585-26"; Laserfiche files that
    # as plain RES 585 within its term.
    assert split_hawaii_number("585-26") == "585"
    assert split_hawaii_number("135") == "135"


def test_hawaii_agenda_key_is_term_qualified():
    ad = GranicusAdapter.for_council("hawaii")
    agenda = ("BILLS FOR FIRST READING Bill 135: AMENDS CHAPTER 3 OF THE HAWAII "
              "COUNTY CODE RELATING TO PAID PARKING FACILITIES")
    got = [r["bill_number"] for r in ad._parse_agenda(agenda, "2022-05-01", "http://x")]
    assert got == ["Bill 135 (2020-2022)"]
    got = [r["bill_number"] for r in ad._parse_agenda(agenda, "2026-05-01", "http://x")]
    assert got == ["Bill 135 (2024-2026)"]


def test_kauai_keys_are_not_term_qualified():
    # Kauai bill numbers run continuously and its resolutions already carry a
    # year, so they must be left alone.
    out = _kauai(
        "5. Bill No. 2988 A BILL FOR AN ORDINANCE RELATING TO THE OPERATING BUDGET "
        "(Public Hearing held on May 13, 2026)"
    )
    assert list(out) == ["Bill 2988"]


# --- crawl bounds ------------------------------------------------------------

def test_max_meetings_clears_a_three_year_window():
    # The date window is the real bound; max_meetings is only a backstop. It
    # must comfortably clear 3 years of meetings — measured 2026-07, that is
    # ~165 agendas for Kauai and ~400 for Hawaii County (two views) — while
    # staying well under the 1,173 / 1,591 the views list in full.
    assert 450 <= DEFAULT_MAX_MEETINGS < 1100
    for council in ("kauai", "hawaii"):
        assert GranicusAdapter.for_council(council).max_meetings == DEFAULT_MAX_MEETINGS


# --- retention window --------------------------------------------------------

def test_window_start_defaults_to_the_retention_window():
    from datetime import date, timedelta
    from tracker.legislative.adapters.granicus import DEFAULT_WINDOW_YEARS
    start = window_start(None)
    assert DEFAULT_WINDOW_YEARS == 3
    assert start.year == date.today().year - 3
    # a real window, not the whole archive
    assert date.today() - start < timedelta(days=3 * 366 + 2)


def test_window_start_honours_an_explicit_since():
    from datetime import date
    assert window_start(date(2019, 5, 1)) == date(2019, 5, 1)


def test_window_start_years_override():
    from datetime import date
    assert window_start(None, years=1).year == date.today().year - 1


def test_max_meetings_and_delay_are_overridable():
    ad = GranicusAdapter.for_council("kauai", max_meetings=5, delay=0)
    assert ad.max_meetings == 5 and ad.delay == 0


# --- agenda transport --------------------------------------------------------

class _FakePage:
    def __init__(self, fail): self.fail = fail
    def goto(self, *a, **kw):
        if self.fail:
            raise RuntimeError("Page.goto: Download is starting")
    def wait_for_timeout(self, *a): pass
    def inner_text(self, *a): return "HTML AGENDA TEXT"


class _FakeCtx:
    """Stands in for playwright's request context; returns a one-page PDF."""
    def __init__(self, body): self.request = self; self._body = body
    def get(self, *a, **kw): return self
    def body(self): return self._body


def test_html_mode_falls_back_to_pdf_when_navigation_downloads():
    # Older Kauai agendas are served as PDF downloads; page.goto aborts on them.
    ad = GranicusAdapter.for_council("kauai")
    ad._pdf_text = staticmethod(lambda ctx, url: "PDF AGENDA TEXT")
    assert ad._agenda_text(None, _FakePage(fail=True), "http://x") == "PDF AGENDA TEXT"
    # the HTML path is still preferred when navigation works
    assert ad._agenda_text(None, _FakePage(fail=False), "http://x") == "HTML AGENDA TEXT"


def test_html_mode_reraises_navigation_error_when_not_a_pdf():
    ad = GranicusAdapter.for_council("kauai")
    ad._pdf_text = staticmethod(lambda ctx, url: "")
    with pytest.raises(RuntimeError, match="Download is starting"):
        ad._agenda_text(None, _FakePage(fail=True), "http://x")


def test_pdf_text_ignores_non_pdf_bodies():
    assert GranicusAdapter._pdf_text(_FakeCtx(b"<html>nope</html>"), "http://x") == ""


def test_hawaii_abbreviated_resolution_is_matched():
    # Hawaii County agendas write resolutions as "Res. 556-26: <TITLE>". Missing
    # this form left that council's resolutions entirely without titles.
    ad = GranicusAdapter.for_council("hawaii")
    agenda = ("Res. 556-26: AUTHORIZES THE MAYOR TO ENTER INTO AN INTERGOVERNMENTAL "
              "AGREEMENT WITH THE UNITED STATES POSTAL INSPECTION SERVICE")
    out = {r["bill_number"]: r["title"] for r in ad._parse_agenda(agenda, "2026-06-03", "http://x")}
    # the "-26" is the adoption year, not part of the number, and the key is
    # term-qualified so it lines up with the Laserfiche index
    assert list(out) == ["Resolution 556 (2024-2026)"]
    assert out["Resolution 556 (2024-2026)"].startswith("AUTHORIZES THE MAYOR")


def test_bare_res_without_period_is_not_matched():
    # "Res" without a period is too easy to hit inside other text.
    ad = GranicusAdapter.for_council("hawaii")
    agenda = "The Res 556 reference AUTHORIZES NOTHING AND RELATES TO NOTHING AT ALL"
    assert ad._parse_agenda(agenda, "2026-06-03", "http://x") == []


def test_agenda_dedupe_key_ignores_the_publisher_view():
    # Hawaii County lists the same meeting under two views, each linking the
    # agenda with its own view_id. Deduping on the URL fetches each twice.
    from tracker.legislative.adapters.granicus import _CLIP_ID_RE
    a = "https://h.granicus.com/AgendaViewer.php?view_id=1&clip_id=3785"
    b = "https://h.granicus.com/AgendaViewer.php?view_id=2&clip_id=3785"
    assert _CLIP_ID_RE.search(a).group(1) == _CLIP_ID_RE.search(b).group(1) == "3785"
