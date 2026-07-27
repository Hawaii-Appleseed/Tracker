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
            elif rows:
                cutoff = rows[0]["started_at"] if False else "1970-01-01T00:00:00+00:00"
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
        updated_rows = conn.execute(
            "SELECT council, bill_number, title, status, last_action, "
            "       last_action_date, url, subjects, last_updated, first_seen "
            "FROM bills WHERE last_updated > ? AND first_seen <= ? "
            "  AND matter_class = 'legislation' "
            "ORDER BY last_updated DESC",
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
