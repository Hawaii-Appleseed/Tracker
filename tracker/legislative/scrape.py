"""Scrape orchestrator: run one or more council adapters, classify, upsert."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from tracker.legislative import COUNCILS, matter_class

# Retention window. The sources reach much further back than this — Maui's
# Legistar API to Nov 2015, Honolulu's browse index to 2017 — but the tracker
# deliberately keeps a rolling 3 years: it covers the sitting council's full
# 2-year term plus the one before it, which is the horizon anyone actually
# searches. Older records are neither fetched nor kept (see prune_expired).
RETENTION_YEARS = 3
DEFAULT_LOOKBACK_DAYS = RETENTION_YEARS * 365
from tracker.legislative.adapters.base import CouncilAdapter
from tracker.legislative.classify import classify
from tracker.legislative.db import (
    DEFAULT_DB,
    connect,
    finish_run,
    init_schema,
    prune_expired,
    start_run,
    upsert_actions,
    upsert_bill,
)

log = logging.getLogger(__name__)


def _build_adapter(
    council: str, conn=None, refetch_agendas: bool = False
) -> CouncilAdapter:
    """Adapter for one council. `conn` (when given) backs an AgendaStore for
    the Granicus-based councils, so settled agendas are fetched once and the
    full since-window is assembled from cache — without it (e.g. in tests)
    every agenda in the window is fetched."""
    def _store(c: str):
        if conn is None:
            return None
        from tracker.legislative.db import AgendaStore
        return AgendaStore(conn, c, refetch=refetch_agendas)

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
        return HawaiiCountyAdapter(agenda_store=_store("hawaii"))
    if council == "kauai":
        # No bill API; bills live in Granicus meeting agendas (HTML).
        from tracker.legislative.adapters.granicus import GranicusAdapter
        return GranicusAdapter.for_council("kauai", agenda_store=_store("kauai"))
    raise ValueError(f"unknown council: {council}")


def scrape_council(
    council: str,
    db_path: Path = DEFAULT_DB,
    since: date | None = None,
    fetch_actions: bool = True,
    force_actions: bool = False,
    refetch_agendas: bool = False,
) -> dict:
    """Scrape one council, upsert into DB, return run summary.

    If `since` is None it defaults to the retention window, which is also the
    furthest back this tracker will go. Passing an older `since` explicitly
    will fetch more, but prune_expired() will drop it again on the next run —
    so the window, not the caller, is the source of truth.

    `force_actions` fetches action history for every bill seen, not just new or
    updated ones — a heavier one-time backfill (each measure is an extra
    request for adapters that fetch history per-bill). Inline-action adapters
    backfill regardless.

    `refetch_agendas` re-fetches Granicus agendas the cache already holds —
    needed after changing the agenda-parsing rules, since only parsed mentions
    (not raw agenda text) are cached.
    """
    if since is None:
        since = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    seen = new = updated = 0
    errors: list[str] = []

    with connect(db_path) as conn:
        init_schema(conn)
        adapter = _build_adapter(council, conn, refetch_agendas=refetch_agendas)
        run_id = start_run(conn, council)
        try:
            for bill in adapter.fetch_bills(since=since):
                seen += 1
                cls = classify(bill.title, bill.raw_subject, bill.bill_type)
                bill_id, is_new, was_updated = upsert_bill(
                    conn, bill, cls.subjects, cls.confidence
                )
                if is_new:
                    new += 1
                if was_updated:
                    updated += 1
                # Action history is only worth a round-trip for legislation.
                # Communications, committee reports and minutes are ~70% of
                # Maui's inventory and have no meaningful progression, so
                # fetching theirs would more than triple a full backfill for
                # timelines nobody reads.
                if fetch_actions and matter_class(bill.bill_type) == "legislation":
                    try:
                        if bill.actions:
                            # Adapter already has the history (no extra request) —
                            # upsert unconditionally so existing bills backfill too.
                            upsert_actions(conn, bill_id, bill.actions)
                        elif is_new or was_updated or force_actions:
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
    db_path: Path = DEFAULT_DB, since: date | None = None, prune: bool = True
) -> list[dict]:
    """Scrape every council, then enforce the retention window.

    Pruning runs here rather than per-council so a single council's failure
    can't drop the others' records: a failed scrape yields nothing new, but the
    prune only ever removes rows that aged out on their own.
    """
    results = [scrape_council(c, db_path=db_path, since=since) for c in COUNCILS]
    if prune:
        with connect(db_path) as conn:
            init_schema(conn)
            removed = prune_expired(conn, years=RETENTION_YEARS)
        if removed:
            log.info("pruned %d records older than %d years", removed, RETENTION_YEARS)
            results.append({"council": "*", "pruned": removed})
    return results
