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
