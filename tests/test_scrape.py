"""scrape_council end to end with fake adapters: a degraded run must not erase
what earlier runs stored, and must say it was degraded."""

import json
from datetime import date
from pathlib import Path

import pytest

from tracker.legislative import scrape
from tracker.legislative.adapters.base import BillRecord, CouncilAdapter
from tracker.legislative.adapters.granicus import GranicusAdapter
from tracker.legislative.adapters.laserfiche import HawaiiCountyAdapter
from tracker.legislative.db import AgendaStore, connect, init_schema

TAX_TITLE = "A BILL FOR AN ORDINANCE RELATING TO REAL PROPERTY TAXATION."


class FakeAdapter(CouncilAdapter):
    council_id = "hawaii"
    text_may_be_missing = True

    def __init__(self, bills, errors=()):
        self.bills = bills
        self.errors.extend(errors)

    def fetch_bills(self, since=None):
        yield from self.bills

    def fetch_actions(self, bill_number):
        return iter(())


def _bill(**kw):
    base = dict(council="hawaii", bill_number="Bill 148 (2024-2026)",
                bill_type="Bill", status="Referred", url="http://lf/1")
    return BillRecord(**{**base, **kw})


class ApiAdapter(FakeAdapter):
    # e.g. Honolulu: the source reports its text directly, so None is real.
    text_may_be_missing = False


def _run(monkeypatch, db, adapter, **kw):
    monkeypatch.setattr(scrape, "_build_adapter", lambda *a, **k: adapter)
    return scrape.scrape_council("hawaii", db_path=db, since=date(2024, 1, 1), **kw)


def _row(db, number="Bill 148 (2024-2026)"):
    with connect(db) as conn:
        return dict(conn.execute(
            "SELECT title, raw_subject, committee, subjects FROM bills "
            "WHERE bill_number = ?", (number,)).fetchone())


def test_untitled_run_keeps_stored_title_and_subjects(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    _run(monkeypatch, db, FakeAdapter([_bill(title=TAX_TITLE, raw_subject="Staff summary.")]))
    assert "tax" in json.loads(_row(db)["subjects"])

    # The agendas were unreadable this run: same bill, no title or summary.
    out = _run(monkeypatch, db, FakeAdapter([_bill(title=None, raw_subject=None)]))

    row = _row(db)
    assert row["title"] == TAX_TITLE
    assert row["raw_subject"] == "Staff summary."
    assert "tax" in json.loads(row["subjects"])
    # Nothing changed, so nothing is reported as updated.
    assert out["bills_updated"] == 0


def test_new_text_still_replaces_old(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    _run(monkeypatch, db, FakeAdapter([_bill(title="OLD TITLE", raw_subject="Old.")]))
    _run(monkeypatch, db, FakeAdapter([_bill(title=TAX_TITLE, raw_subject="New.")]))
    row = _row(db)
    assert (row["title"], row["raw_subject"]) == (TAX_TITLE, "New.")


def test_api_adapter_none_is_authoritative(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    _run(monkeypatch, db, ApiAdapter([_bill(title=TAX_TITLE, raw_subject="Summary.")]))
    _run(monkeypatch, db, ApiAdapter([_bill(title=TAX_TITLE, raw_subject=None)]))
    assert _row(db)["raw_subject"] is None


def test_clean_refetch_can_clear_text(tmp_path: Path, monkeypatch):
    """After a parser fix, --refetch-agendas must be able to drop a title the
    fixed parser no longer attributes to this bill."""
    db = tmp_path / "t.db"
    _run(monkeypatch, db, FakeAdapter([_bill(title="MISATTRIBUTED", raw_subject="Wrong.")]))
    _run(monkeypatch, db, FakeAdapter([_bill(title=None, raw_subject=None)]),
         refetch_agendas=True)
    row = _row(db)
    assert (row["title"], row["raw_subject"]) == (None, None)


def test_degraded_refetch_still_keeps_text(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    _run(monkeypatch, db, FakeAdapter([_bill(title=TAX_TITLE)]))
    _run(monkeypatch, db, FakeAdapter([_bill(title=None)], errors=["crawl failed"]),
         refetch_agendas=True)
    assert _row(db)["title"] == TAX_TITLE


def test_committee_code_summary_is_not_carried_forward(tmp_path: Path, monkeypatch):
    """Rows written under the old Laserfiche mapping hold the committee code
    as their summary; that must clear, not be preserved as if it were text."""
    db = tmp_path / "t.db"
    _run(monkeypatch, db, FakeAdapter([_bill(title=None, raw_subject="COUNCIL")]))
    _run(monkeypatch, db, FakeAdapter([_bill(title=None, raw_subject=None, committee="FC")]))
    row = _row(db)
    assert row["raw_subject"] is None
    assert row["committee"] == "FC"


def test_adapter_errors_are_recorded_on_the_run(tmp_path: Path, monkeypatch):
    db = tmp_path / "t.db"
    out = _run(monkeypatch, db, FakeAdapter([_bill(title=TAX_TITLE)],
                                            errors=["hawaii agenda crawl failed"]))
    assert out["errors"] == ["hawaii agenda crawl failed"]
    assert out["bills_seen"] == 1
    with connect(db) as conn:
        errs = conn.execute("SELECT errors FROM runs").fetchone()[0]
    assert json.loads(errs) == ["hawaii agenda crawl failed"]


# --- Granicus: a failed crawl falls back to the agenda cache ------------------

def _no_browser(self, since=None):
    raise RuntimeError("BrowserType.launch: Executable doesn't exist\n╔═══ banner ═══╗")
    yield  # pragma: no cover — makes this a generator, like the real one


def _cached_store(conn, council):
    store = AgendaStore(conn, council)
    store.save("http://g/a1", "2026-03-04", [{
        "bill_number": "Bill 3000", "bill_type": "Bill", "title": TAX_TITLE,
        "summary": None, "stage": "First Reading", "date": "2026-03-04",
        "url": "http://g/a1",
    }])
    return store


def test_granicus_crawl_failure_serves_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(GranicusAdapter, "iter_raw_agendas", _no_browser)
    with connect(tmp_path / "t.db") as conn:
        init_schema(conn)
        ad = GranicusAdapter.for_council("kauai", agenda_store=_cached_store(conn, "kauai"))
        bills = list(ad.fetch_bills(since=date(2024, 1, 1)))

    assert [(b.bill_number, b.title) for b in bills] == [("Bill 3000", TAX_TITLE)]
    assert len(ad.errors) == 1
    assert "served from cache" in ad.errors[0]
    assert "Executable doesn't exist" in ad.errors[0]
    assert "banner" not in ad.errors[0]  # first line only


def test_granicus_crawl_failure_without_cache_still_raises(monkeypatch):
    monkeypatch.setattr(GranicusAdapter, "iter_raw_agendas", _no_browser)
    ad = GranicusAdapter.for_council("kauai")
    with pytest.raises(RuntimeError):
        list(ad.fetch_bills(since=date(2024, 1, 1)))


def test_hawaii_inherits_granicus_degradation(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(GranicusAdapter, "iter_raw_agendas", _no_browser)
    with connect(tmp_path / "t.db") as conn:
        init_schema(conn)
        ad = HawaiiCountyAdapter(delay=0, agenda_store=_cached_store(conn, "hawaii"))
        active = ad._active_from_granicus(since=date(2024, 1, 1))

    assert TAX_TITLE in {b.title for b in active.values()}
    assert any("served from cache" in e for e in ad.errors)


def test_per_agenda_failures_are_recorded(monkeypatch):
    """A browser that launches and then dies fails each agenda without
    raising; the run must still be flagged."""
    import playwright.sync_api

    class _Obj:
        def __getattr__(self, name):
            return lambda *a, **k: self

    class _PW:
        chromium = _Obj()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _closed(self, ctx, page, url):
        raise RuntimeError("Target page, context or browser has been closed\nmore")

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(GranicusAdapter, "_list_meetings",
                        lambda self, page, vid: [("2026-09-02", "http://g/a1"),
                                                 ("2026-09-09", "http://g/a2")])
    monkeypatch.setattr(GranicusAdapter, "_agenda_text", _closed)
    ad = GranicusAdapter.for_council("kauai", delay=0)
    assert list(ad.fetch_bills(since=date(2024, 1, 1))) == []
    assert ad.errors == [
        "kauai 2 of 2 agenda fetches failed: Target page, context or browser has been closed"
    ]


# --- Laserfiche outages leave stored rows alone ------------------------------

def test_laserfiche_unreachable_aborts_without_writing(tmp_path: Path, monkeypatch):
    ad = HawaiiCountyAdapter(delay=0)
    ad._active_from_granicus = lambda since: {"Bill 1 (2024-2026)": _bill(
        bill_number="Bill 1 (2024-2026)", title=TAX_TITLE, url="http://agenda")}
    ad._session = lambda: (_ for _ in ()).throw(ConnectionError("503\nbody"))
    with pytest.raises(RuntimeError, match="Laserfiche unreachable: 503"):
        list(ad.fetch_bills(since=date(2024, 1, 1)))


def test_laserfiche_metadata_failure_skips_doc(monkeypatch):
    from tracker.legislative.adapters.laserfiche import _Doc
    ad = HawaiiCountyAdapter(delay=0)
    key = "Bill 148 (2024-2026)"
    ad._active_from_granicus = lambda since: {key: _bill(title=TAX_TITLE, url="http://agenda")}
    ad._session = lambda: None
    ad._doc_index = lambda since=None: {key: _Doc("1", "Bill", "148", "2024-2026", 1, "Bill/Resolution")}
    def _boom(doc_id):
        raise TimeoutError("read timed out")
    ad._metadata = _boom
    # Neither the Laserfiche record nor the thinner agenda record is emitted.
    assert list(ad.fetch_bills(since=date(2024, 1, 1))) == []
    assert ad.errors == ["hawaii 1 of 1 Laserfiche metadata fetches failed "
                         "(rows left unchanged): read timed out"]
