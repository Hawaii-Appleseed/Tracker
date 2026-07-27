from datetime import date, timedelta
from pathlib import Path

from tracker.legislative.adapters.base import BillRecord
from tracker.legislative.db import AgendaStore, connect, init_schema, upsert_bill


def test_upsert_idempotent(tmp_path: Path):
    db = tmp_path / "test.db"
    bill = BillRecord(
        council="honolulu",
        bill_number="BILL040(26)",
        title="RELATING TO REAL PROPERTY TAXATION.",
        bill_type="Bill",
        introducer="Test",
        introduced_date="2026-05-28",
        status="INTRO",
        url="https://example.com/3752",
    )
    with connect(db) as conn:
        init_schema(conn)
        bid, is_new, was_updated = upsert_bill(conn, bill, ["tax"], 0.5)
        assert is_new and not was_updated

        bid2, is_new2, was_updated2 = upsert_bill(conn, bill, ["tax"], 0.5)
        assert bid == bid2 and not is_new2 and not was_updated2

    with connect(db) as conn:
        n = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        assert n == 1


def test_upsert_detects_status_change(tmp_path: Path):
    db = tmp_path / "test.db"
    bill = BillRecord(
        council="maui",
        bill_number="CC 26-1",
        title="Test bill",
        status="Referred",
        url="https://example.com/1",
    )
    with connect(db) as conn:
        init_schema(conn)
        upsert_bill(conn, bill, [], None)

    bill.status = "Passed First Reading"
    with connect(db) as conn:
        _, is_new, was_updated = upsert_bill(conn, bill, [], None)
        assert not is_new and was_updated


def _mention(num="Bill 2988", stage="First Reading", date_="2026-01-15", url="http://x/a1"):
    return {
        "bill_number": num, "bill_type": "Bill",
        "title": "A BILL FOR AN ORDINANCE RELATING TO THE OPERATING BUDGET",
        "summary": None, "stage": stage, "date": date_, "url": url,
    }


def test_agenda_store_roundtrip_and_window(tmp_path: Path):
    db = tmp_path / "test.db"
    with connect(db) as conn:
        init_schema(conn)
        store = AgendaStore(conn, "kauai")
        store.save("http://x/a1", "2026-01-15", [_mention()])
        store.save("http://x/a2", "2026-03-01",
                   [_mention(stage="Second Reading", date_="2026-03-01", url="http://x/a2")])

        out = store.load()
        assert {(m["date"], m["stage"]) for m in out} == {
            ("2026-01-15", "First Reading"), ("2026-03-01", "Second Reading"),
        }
        assert out[0]["url"].startswith("http://x/")
        # since window excludes the older agenda
        assert [m["date"] for m in store.load("2026-02-01")] == ["2026-03-01"]
        # councils are isolated
        assert AgendaStore(conn, "hawaii").load() == []


def test_agenda_store_resave_replaces_mentions(tmp_path: Path):
    db = tmp_path / "test.db"
    with connect(db) as conn:
        init_schema(conn)
        store = AgendaStore(conn, "kauai")
        store.save("http://x/a1", "2026-01-15", [_mention()])
        store.save("http://x/a1", "2026-01-15", [])  # amended parse: now empty
        assert store.load() == []


def test_agenda_store_freshness(tmp_path: Path):
    db = tmp_path / "test.db"
    old = "2026-01-15"
    recent = date.today().isoformat()
    with connect(db) as conn:
        init_schema(conn)
        store = AgendaStore(conn, "kauai")
        store.save("http://x/old", old, [])
        store.save("http://x/recent", recent, [])

        # fetched + settled meeting -> skip; recent meetings may still be
        # amended -> re-fetch; never-fetched or dateless -> fetch.
        assert store.is_fresh("http://x/old", old)
        assert not store.is_fresh("http://x/recent", recent)
        assert not store.is_fresh("http://x/never", old)
        assert not store.is_fresh("http://x/old", "")
        # a meeting just past the horizon boundary is still re-fetched
        boundary = (date.today() - timedelta(days=store.fresh_days)).isoformat()
        store.save("http://x/boundary", boundary, [])
        assert not store.is_fresh("http://x/boundary", boundary)
        # refetch mode ignores the cache entirely
        assert not AgendaStore(conn, "kauai", refetch=True).is_fresh("http://x/old", old)
