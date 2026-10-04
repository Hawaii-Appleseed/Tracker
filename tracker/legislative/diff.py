"""Compute diff between two points in time: new bills and status changes."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tracker.legislative.db import DEFAULT_DB, connect, last_completed_run


def diff_since(
    since_iso: str | None = None, db_path: Path = DEFAULT_DB
) -> dict:
    """Return dict of new bills and updated bills since `since_iso` (UTC).

    If since_iso is None, uses the second-most-recent completed run's
    completed_at, so 'diff since last run' makes sense after a fresh scrape.
    """
    with connect(db_path) as conn:
        cutoff = since_iso
        if cutoff is None:
            # Second-most-recent run: the one BEFORE the most recent
            rows = conn.execute(
                "SELECT completed_at FROM runs "
                "WHERE completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 2"
            ).fetchall()
            if len(rows) >= 2:
                cutoff = rows[1]["completed_at"]
            else:
                cutoff = "1970-01-01T00:00:00+00:00"

        # Alerts stay scoped to legislation. The store also holds county
        # communications, committee reports and minutes so they're searchable,
        # but Maui alone files dozens a week — routing those to Slack would
        # bury the bill movement the alert exists to surface.
        new_rows = conn.execute(
            "SELECT council, bill_number, title, status, url, subjects, "
            "       introduced_date, first_seen "
            "FROM bills WHERE first_seen > ? AND matter_class = 'legislation' "
            "ORDER BY first_seen DESC",
            (cutoff,),
        ).fetchall()
        # "Updated" = a status / latest-action move, as logged in bill_changes.
        # last_updated also moves on title, URL or summary edits, which made
        # a title backfill read as "1447 status changes".
        updated_rows = conn.execute(
            "SELECT b.council, b.bill_number, b.title, b.status, b.last_action, "
            "       b.last_action_date, b.url, b.subjects, b.last_updated, b.first_seen "
            "FROM bills b WHERE b.first_seen <= ? AND b.matter_class = 'legislation' "
            "  AND EXISTS (SELECT 1 FROM bill_changes c WHERE c.bill_id = b.id "
            "              AND c.kind = 'update' AND c.changed_at > ?) "
            "ORDER BY b.last_updated DESC",
            (cutoff, cutoff),
        ).fetchall()

    def _row(r):
        d = dict(r)
        if "subjects" in d and d["subjects"]:
            try:
                d["subjects"] = json.loads(d["subjects"])
            except (TypeError, json.JSONDecodeError):
                d["subjects"] = []
        return d

    return {
        "since": cutoff,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "new": [_row(r) for r in new_rows],
        "updated": [_row(r) for r in updated_rows],
    }
