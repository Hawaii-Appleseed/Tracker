"""Failures must be visible: exit code, per-council freshness, no error text
in the public payload, and "updated" meaning a real status move."""

import json
from argparse import Namespace
from pathlib import Path

from tracker.legislative import COUNCILS, cli
from tracker.legislative.adapters.base import BillRecord
from tracker.legislative.db import connect, council_status, finish_run, init_schema, start_run, upsert_bill
from tracker.legislative.diff import diff_since


def _run(conn, council, seen, errors=None):
    rid = start_run(conn, council)
    finish_run(conn, rid, seen, 0, 0, errors)


def test_scrape_exit_code_reflects_errors(monkeypatch, capsys):
    ok = {"council": "maui", "bills_seen": 5, "bills_new": 0, "bills_updated": 0, "errors": []}
    bad = {**ok, "council": "kauai", "errors": ["crawl failed"]}
    args = Namespace(council="all", db=None, since=None)
    monkeypatch.setattr(cli, "scrape_all", lambda **k: [ok])
    assert cli.cmd_scrape(args) == 0
    monkeypatch.setattr(cli, "scrape_all", lambda **k: [ok, bad])
    assert cli.cmd_scrape(args) == 1


def test_council_status(tmp_path: Path):
    with connect(tmp_path / "t.db") as conn:
        init_schema(conn)
        _run(conn, "kauai", 150)
        _run(conn, "kauai", 0, ["Executable doesn't exist"])
        _run(conn, "maui", 1900)
        st = council_status(conn, ["kauai", "maui", "hawaii"])
    assert st["kauai"]["ok"] is False and st["kauai"]["last_success"]
    assert st["maui"]["ok"] is True
    assert st["hawaii"] == {"last_success": None, "last_run": None, "ok": False}


def test_site_payload_has_no_error_text(tmp_path: Path):
    import site_build
    db = tmp_path / "t.db"
    with connect(db) as conn:
        init_schema(conn)
        _run(conn, "kauai", 0, ["Executable doesn't exist at /Users/someone/Library"])
    site_build.build(db_path=db, site_dir=tmp_path / "site")
    payload = json.loads((tmp_path / "site" / "bills.json").read_text())
    assert "/Users/" not in json.dumps(payload)
    assert set(payload["last_scrape"]) == {"completed_at"}
    assert payload["council_status"]["kauai"]["ok"] is False
    assert set(payload["council_status"]) == set(COUNCILS)


def test_diff_updated_means_status_change(tmp_path: Path):
    db = tmp_path / "t.db"
    b = BillRecord(council="maui", bill_number="Bill 1", title="T", bill_type="Bill",
                   status="Referred", url="http://x/1")
    t = BillRecord(**{**b.model_dump(), "bill_number": "Bill 2"})
    with connect(db) as conn:
        init_schema(conn)
        upsert_bill(conn, b, [], None)
        upsert_bill(conn, t, [], None)
    with connect(db) as conn:
        cutoff = conn.execute("SELECT MAX(last_updated) FROM bills").fetchone()[0]
    import time; time.sleep(1.1)
    with connect(db) as conn:
        upsert_bill(conn, b.model_copy(update={"status": "Passed"}), [], None)   # real move
        upsert_bill(conn, t.model_copy(update={"title": "New title"}), [], None)  # text only
    out = diff_since(since_iso=cutoff, db_path=db)
    assert [r["bill_number"] for r in out["updated"]] == ["Bill 1"]
