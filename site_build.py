"""Render the static dashboard payload from the SQLite store.

The dashboard is a search interface over every measure all four councils
publish, so the payload is split in two:

  bills.json              The search corpus: every bill, minified, WITHOUT
                          action histories. It is fetched on first paint and
                          fully indexed in the browser, so it has to stay
                          small — dropping indentation and inline timelines is
                          most of that.

  actions/<council>.json  Per-bill action timelines, fetched only when a reader
                          expands a row. Sharded by council so opening one Maui
                          bill doesn't drag Honolulu's history along with it.

Both are minified: this file is machine-written and machine-read, and the
indentation was a meaningful share of the old 2.4 MB single-file payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tracker.legislative import COUNCILS, MATTER_CLASSES, SUBJECTS
from tracker.legislative.db import DEFAULT_DB, connect, init_schema, last_completed_run
from tracker.legislative.feeds import build_feeds

SITE_DIR = Path(__file__).resolve().parent / "site"

# Columns that reach the browser. classification_confidence is deliberately
# absent — nothing in the front-end reads it, and it is pure weight in a
# payload that ships on every page load.
_BILL_COLUMNS = """
    id, council, bill_number, title, bill_type, matter_class, introducer,
    introduced_date, status, last_action, last_action_date, url,
    raw_subject, committee, subjects, first_seen, last_updated
"""


def _write_json(path: Path, payload) -> int:
    """Write minified JSON and return the byte size."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text)
    return len(text.encode("utf-8"))


def build(db_path: Path = DEFAULT_DB, site_dir: Path = SITE_DIR) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    out = site_dir / "bills.json"

    with connect(db_path) as conn:
        # A build can run against a DB the current scraper hasn't touched yet
        # (rebuild-only, or a checkout predating a column). Migrate first so the
        # SELECT below can rely on the columns existing.
        init_schema(conn)
        rows = conn.execute(
            f"""
            SELECT {_BILL_COLUMNS}
            FROM bills
            ORDER BY COALESCE(last_action_date, introduced_date, first_seen) DESC
            """
        ).fetchall()
        last_run = last_completed_run(conn)
        action_rows = conn.execute(
            """
            SELECT b.council, a.bill_id, a.action_date, a.action, a.committee
            FROM bill_actions a
            JOIN bills b ON b.id = a.bill_id
            ORDER BY a.bill_id, a.action_date DESC, a.id DESC
            """
        ).fetchall()

    # Action timelines, grouped per council then per bill. Newest action first;
    # the front-end leads with [0] as the latest.
    by_council: dict[str, dict[str, list[dict]]] = {c: {} for c in COUNCILS}
    counts: dict[int, int] = {}
    for a in action_rows:
        shard = by_council.setdefault(a["council"], {})
        shard.setdefault(str(a["bill_id"]), []).append(
            {"date": a["action_date"], "action": a["action"], "committee": a["committee"]}
        )
        counts[a["bill_id"]] = counts.get(a["bill_id"], 0) + 1

    bills = []
    for r in rows:
        d = dict(r)
        try:
            d["subjects"] = json.loads(d.get("subjects") or "[]")
        except json.JSONDecodeError:
            d["subjects"] = []
        # Row expansion needs to know a timeline exists before deciding whether
        # to fetch its shard; the timeline itself stays out of this payload.
        d["action_count"] = counts.get(d["id"], 0)
        bills.append(d)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "last_scrape": dict(last_run) if last_run else None,
        "subjects": list(SUBJECTS),
        "councils": list(COUNCILS),
        "matter_classes": list(MATTER_CLASSES),
        "bills": bills,
    }
    size = _write_json(out, payload)

    actions_dir = site_dir / "actions"
    actions_dir.mkdir(parents=True, exist_ok=True)
    shard_total = 0
    for council in COUNCILS:
        shard_total += _write_json(
            actions_dir / f"{council}.json", by_council.get(council, {})
        )

    by_class: dict[str, int] = {}
    for b in bills:
        k = b.get("matter_class") or "legislation"
        by_class[k] = by_class.get(k, 0) + 1

    print(f"Wrote {out} ({len(bills)} bills, {size / 1e6:.2f} MB)")
    print(f"Wrote {actions_dir}/*.json ({len(action_rows)} actions, {shard_total / 1e6:.2f} MB)")
    print(f"  by class: {by_class}")
    build_feeds(db_path=db_path, site_dir=site_dir)
    return out


if __name__ == "__main__":
    build()
# real git-push test 1787993272
