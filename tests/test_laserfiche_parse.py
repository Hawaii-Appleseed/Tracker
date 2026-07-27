from datetime import date
from tracker.legislative.adapters.base import BillRecord
from tracker.legislative.adapters.laserfiche import (
    HawaiiCountyAdapter,
    _Doc,
    _iso_from_mdy,
    _norm_key,
    _term_end_year,
)

META_BILL = {
    "Bill/Resolution - Type": "BIL",
    "Bill/Resolution - Council Term": "2024-2026",
    "Bill/Resolution": "148",
    "Draft": "01",
    "Introducer": "James E. Hustace, Council Member",
    "Referred To": "FC",
    "Action 1": "FC-100: Recommended passage on first reading - 4/1/2026",
    "Action 2": "Council: Bill 148 passes first reading - 4/15/26",
    "Action 3": "Council: Bill 148 passes second & final reading - 05/20/26",
    "Status": "Adopted",
    "Reading Date": "4/15/2026",
}


def test_iso_from_mdy():
    assert _iso_from_mdy("passes - 05/20/26") == "2026-05-20"
    assert _iso_from_mdy("nope") is None


def test_iso_from_mdy_pivots_two_digit_years_out_of_the_future():
    # The archive runs back to 1969, and action text writes two-digit years.
    assert _iso_from_mdy("Council: passes - 3/4/89") == "1989-03-04"
    assert _iso_from_mdy("Council: passes - 10/1/72") == "1972-10-01"
    assert _iso_from_mdy("Council: passes - 1/15/25") == "2025-01-15"
    assert _iso_from_mdy("full year survives - 4/1/2026") == "2026-04-01"


def test_build_record_extracts_metadata():
    rec = HawaiiCountyAdapter()._build_record(META_BILL, doc_id="999")
    assert rec is not None
    # Numbering restarts each council term, so the term qualifies the key.
    assert rec.bill_number == "Bill 148 (2024-2026)"
    assert rec.bill_type == "Bill"
    assert rec.introducer == "James E. Hustace, Council Member"
    assert rec.status == "Adopted"
    # latest (highest-numbered) action wins
    assert "second & final reading" in rec.last_action
    assert rec.last_action_date == "2026-05-20"
    assert "DocView.aspx?id=999" in rec.url


def test_build_record_derives_status_from_action_when_blank():
    meta = dict(META_BILL)
    meta["Status"] = ""
    rec = HawaiiCountyAdapter()._build_record(meta, doc_id="1")
    assert rec.status == "Passed Second Reading"


def test_build_record_rejects_non_bill():
    assert HawaiiCountyAdapter()._build_record({"Bill/Resolution - Type": "FUND"}, "1") is None


def test_action_records_use_the_term_qualified_key():
    rec = HawaiiCountyAdapter()._build_record(META_BILL, doc_id="999")
    assert [a.bill_number for a in rec.actions] == ["Bill 148 (2024-2026)"] * 3
    # oldest-first, by the template's Action field number
    assert rec.actions[0].action.startswith("FC-100")
    assert rec.actions[0].action_date == "2026-04-01"


# --- term qualification ------------------------------------------------------
# Hawaii County restarts numbering every council term, so the same "Bill 148"
# exists in ~25 terms; bills.db is UNIQUE(council, bill_number).

def test_norm_key_strips_leading_zeros_and_qualifies_by_term():
    assert _norm_key("Bill", "001") == "Bill 1"
    assert _norm_key("Resolution", "0148") == "Resolution 148"
    assert _norm_key("Bill", "148", "2024-2026") == "Bill 148 (2024-2026)"
    assert _norm_key("Bill", "148", "2022-2024") == "Bill 148 (2022-2024)"


def test_term_end_year():
    assert _term_end_year("2022-2024") == 2024
    assert _term_end_year("2026") == 2026
    assert _term_end_year("MAPS AND LARGE ATTACHMENTS") is None


# --- retention window --------------------------------------------------------
# The archive runs back to 1969 (bills) and 1905 (ordinances); the tracker keeps
# a rolling window, so the walk must stop rather than descend the whole thing.

def _walk_recorder(min_year=None):
    """Adapter that records which term folders the index walk actually lists."""
    ad = HawaiiCountyAdapter(min_year=min_year, delay=0)
    walked = []
    ad._children = lambda fid: []
    ad._term_folders = lambda startid: [
        ("1969-1972", 1), ("2018-2020", 2), ("2022-2024", 3), ("2024-2026", 4),
    ]
    ad._list_docs = lambda fid: walked.append(fid) or []
    ad.categories = ("Bill",)
    return ad, walked


def test_index_walk_stops_at_the_retention_window():
    ad, walked = _walk_recorder()
    ad._doc_index(since=date(2023, 7, 1))
    assert walked == [3, 4]          # 2022-2024 straddles the floor and is kept


def test_index_walk_defaults_to_the_window_not_the_whole_archive():
    ad, walked = _walk_recorder()
    ad._doc_index(since=None)
    # since=None must NOT mean 1969: only terms reaching into the last 3 years.
    assert 1 not in walked and 2 not in walked
    assert walked == [3, 4]


def test_min_year_overrides_the_window():
    ad, walked = _walk_recorder(min_year=2019)
    ad._doc_index(since=None)
    assert walked == [2, 3, 4]


def test_build_record_prefers_the_terms_own_metadata_over_the_folder():
    meta = dict(META_BILL)
    meta["Bill/Resolution - Council Term"] = "2020-2022"
    rec = HawaiiCountyAdapter()._build_record(meta, doc_id="1", term="2024-2026")
    assert rec.bill_number == "Bill 148 (2020-2022)"


def test_build_record_falls_back_to_folder_term():
    meta = {k: v for k, v in META_BILL.items() if k != "Bill/Resolution - Council Term"}
    rec = HawaiiCountyAdapter()._build_record(meta, doc_id="1", term="2018-2020")
    assert rec.bill_number == "Bill 148 (2018-2020)"


# --- index walking -----------------------------------------------------------

def _index(*docs):
    """docs: (name, template, term) -> the index _doc_index() would build."""
    idx = {}
    for i, (name, template, term) in enumerate(docs):
        HawaiiCountyAdapter._index_doc(idx, str(100 + i), name, template, term)
    return idx


def test_index_keeps_highest_draft_per_bill():
    idx = _index(
        ("BIL 001 Draft 01 2024-2026", "Bill/Resolution", "2024-2026"),
        ("BIL 001 Draft 03 2024-2026", "Bill/Resolution", "2024-2026"),
        ("BIL 001 Draft 02 2024-2026", "Bill/Resolution", "2024-2026"),
    )
    assert list(idx) == ["Bill 1 (2024-2026)"]
    assert idx["Bill 1 (2024-2026)"].draft == 3
    assert idx["Bill 1 (2024-2026)"].doc_id == "101"


def test_index_does_not_collapse_the_same_number_across_terms():
    idx = _index(
        ("BIL 148 Draft 01 2024-2026", "Bill/Resolution", "2024-2026"),
        ("BIL 148 Draft 01 2022-2024", "Bill/Resolution", "2022-2024"),
        ("BIL 148 Draft 01 1988-1992", "Bill/Resolution", "1988-1992"),
    )
    assert sorted(idx) == [
        "Bill 148 (1988-1992)", "Bill 148 (2022-2024)", "Bill 148 (2024-2026)",
    ]


def test_index_handles_resolutions_and_ordinances():
    idx = _index(
        ("RES 585 Draft 01 2024-2026", "Bill/Resolution", "2024-2026"),
        ("ORD 2026-001 2024-2026", "Ordinances", "2026"),
        # Ordinance numbers are year-sequence, so they need no term suffix.
        ("ORD 1905-002", "Ordinances", "1905"),
    )
    assert sorted(idx) == ["Ordinance 1905-002", "Ordinance 2026-001",
                           "Resolution 585 (2024-2026)"]
    assert idx["Ordinance 2026-001"].type_label == "Ordinance"


def test_index_skips_untemplated_word_document_duplicates():
    # The "Word Documents" subfolder is never descended into, but a stray
    # unparseable name must not create an entry either.
    idx = _index(("Word Documents", "", "2024-2026"),
                 ("FUND", "", "2024-2026"))
    assert idx == {}


# --- ordinances --------------------------------------------------------------

META_ORD = {
    "Ordinances - Type": "ORD",
    "Ordinances - Council Term": "2024-2026",
    "Year": "2026",
    "Ordinance": "001",
    "Effective Date": "1/2/2026",
}


def test_build_ordinance_record():
    rec = HawaiiCountyAdapter()._build_record(META_ORD, doc_id="7", term="2026")
    assert rec.bill_number == "Ordinance 2026-001"
    assert rec.bill_type == "Ordinance"
    assert rec.status == "Enacted"
    assert rec.last_action_date == "2026-01-02"
    assert rec.actions == []


# --- the inverted join -------------------------------------------------------

def _adapter_with(index, meta, granicus):
    ad = HawaiiCountyAdapter(delay=0)
    ad._session = lambda: None
    ad._doc_index = lambda since=None: index
    ad._metadata = lambda doc_id: meta[doc_id]
    ad._active_from_granicus = lambda since: granicus
    return ad


def _gbill(number, title):
    return BillRecord(council="hawaii", bill_number=number, title=title,
                      url="http://agenda", raw_subject=None)


def test_fetch_bills_yields_every_laserfiche_doc_titled_or_not():
    """The whole point of the inversion: Laserfiche is the spine. A bill that
    never reached a scraped agenda still has to be yielded, with title=None."""
    index = {
        "Bill 148 (2024-2026)": _Doc("1", "Bill", "148", "2024-2026", 1, "Bill/Resolution"),
        "Bill 12 (1988-1992)": _Doc("2", "Bill", "12", "1988-1992", 1, "Bill/Resolution"),
    }
    meta = {
        "1": META_BILL,
        "2": {"Bill/Resolution - Type": "BIL", "Bill/Resolution": "012",
              "Bill/Resolution - Council Term": "1988-1992",
              "Introducer": "Someone", "Action 1": "Council: passes - 3/4/89"},
    }
    granicus = {"Bill 148 (2024-2026)": _gbill("Bill 148 (2024-2026)", "A TITLE FROM THE AGENDA")}
    out = {b.bill_number: b for b in _adapter_with(index, meta, granicus).fetch_bills()}

    assert sorted(out) == ["Bill 12 (1988-1992)", "Bill 148 (2024-2026)"]
    # enriched where an agenda title exists...
    assert out["Bill 148 (2024-2026)"].title == "A TITLE FROM THE AGENDA"
    assert out["Bill 148 (2024-2026)"].introducer == "James E. Hustace, Council Member"
    # ...and still yielded, untitled but with real metadata, where it does not.
    assert out["Bill 12 (1988-1992)"].title is None
    assert out["Bill 12 (1988-1992)"].introducer == "Someone"
    assert out["Bill 12 (1988-1992)"].last_action_date == "1989-03-04"


def test_fetch_bills_still_yields_agenda_only_bills():
    # On an agenda but not yet filed in Laserfiche.
    index = {"Bill 148 (2024-2026)": _Doc("1", "Bill", "148", "2024-2026", 1, "Bill/Resolution")}
    granicus = {"Bill 200 (2024-2026)": _gbill("Bill 200 (2024-2026)", "NOT YET FILED")}
    out = {b.bill_number: b for b in _adapter_with(index, {"1": META_BILL}, granicus).fetch_bills()}
    assert out["Bill 200 (2024-2026)"].title == "NOT YET FILED"
    assert len(out) == 2


def test_fetch_bills_falls_back_to_granicus_when_laserfiche_is_down():
    ad = HawaiiCountyAdapter(delay=0)
    ad._active_from_granicus = lambda since: {"Bill 1 (2024-2026)": _gbill("Bill 1 (2024-2026)", "T")}
    def boom():
        raise RuntimeError("WAF said no")
    ad._session = boom
    out = list(ad.fetch_bills())
    assert [b.bill_number for b in out] == ["Bill 1 (2024-2026)"]


def test_fetch_bills_survives_a_single_bad_metadata_read():
    index = {
        "Bill 1 (2024-2026)": _Doc("1", "Bill", "1", "2024-2026", 1, "Bill/Resolution"),
        "Bill 2 (2024-2026)": _Doc("2", "Bill", "2", "2024-2026", 1, "Bill/Resolution"),
    }
    ad = _adapter_with(index, {"1": META_BILL}, {})   # doc 2 raises KeyError
    assert [b.bill_number for b in ad.fetch_bills()] == ["Bill 148 (2024-2026)"]
