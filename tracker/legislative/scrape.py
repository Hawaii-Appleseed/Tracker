"""Scrape orchestrator: run one or more council adapters, classify, upsert."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from tracker.legislative import COUNCILS

# When no explicit --since is given, scrape this far back. County councils run
# on 2-year terms (year-round bodies, not session-bound), so a 730-day window
# covers the full active inventory of the sitting council.
DEFAULT_LOOKBACK_DAYS = 730
from tracker.legislative.adapters.base import CouncilAdapter
from tracker.legislative.classify import classify
from tracker.legislative.db import (
    DEFAULT_DB,
    connect,
    finish_run,
    init_schema,
    start_run,
    upsert_actions,
    upsert_bill,
)

log = logging.getLogger(__name__)


def _build_adapter(council: str) -> CouncilAdapter:
    if council == "maui":
        from tracker.legislative.adapters.legistar_api import LegistarApiAdapter
        return LegistarApiAdapter(council_id="maui", tenant="mauicounty")
    if council == "honolulu":
        from tracker.legislative.adapters.honolulu import HonoluluAdapter
        return HonoluluAdapter()
    if council == "hawaii":
        # Authoritative source = Laserfiche "Council Records System" metadata
        # (introducer, status, dated action history); titles borrowed from
        # Granicus agendas. See adapters/laserfiche.py.
        from tracker.legislative.adapters.laserfiche import HawaiiCountyAdapter
        return HawaiiCountyAdapter()
    if council == "kauai":
        # No bill API; bills live in Granicus meeting agendas (HTML).
        from tracker.legislative.adapters.granicus import GranicusAdapter
        return GranicusAdapter.for_council("kauai")
    raise ValueError(f"unknown council: {council}")


def scrape_council(
    council: str,
    db_path: Path = DEFAULT_DB,
    since: date | None = None,
    fetch_actions: bool = True,
) -> dict:
    """Scrape one council, upsert into DB, return run summary.

    If `since` is None, defaults to a DEFAULT_LOOKBACK_DAYS window so the daily
    cron stays incremental. Pass an explicit old date for a full backfill.
    """
    if since is None:
        since = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    adapter = _build_adapter(council)
    seen = new = updated = 0
    errors: list[str] = []

    with connect(db_path) as conn:
        init_schema(conn)
        run_id = start_run(conn, council)
        try:
            for bill in adapter.fetch_bills(since=since):
                seen += 1
                cls = classify(bill.title, bill.raw_subject)
                bill_id, is_new, was_updated = upsert_bill(
                    conn, bill, cls.subjects, cls.confidence
                )
                if is_new:
                    new += 1
                if was_updated:
                    updated += 1
                if fetch_actions and (is_new or was_updated):
                    try:
                        upsert_actions(
                            conn, bill_id, adapter.fetch_actions(bill.bill_number)
                        )
                    except Exception as e:
                        errors.append(f"{bill.bill_number} actions: {e}")
                        log.warning("actions fetch failed for %s: %s", bill.bill_number, e)
        except Exception as e:
            errors.append(str(e))
            log.exception("scrape failed for council=%s", council)
        finally:
            finish_run(conn, run_id, seen, new, updated, errors or None)

    return {
        "council": council,
        "bills_seen": seen,
        "bills_new": new,
        "bills_updated": updated,
        "errors": errors,
    }


def scrape_all(
    db_path: Path = DEFAULT_DB, since: date | None = None
) -> list[dict]:
    return [scrape_council(c, db_path=db_path, since=since) for c in COUNCILS]
