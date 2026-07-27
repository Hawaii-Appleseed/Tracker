"""CLI entry point: python -m tracker.legislative <command> ..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from tracker.legislative import COUNCILS
from tracker.legislative.db import DEFAULT_DB
from tracker.legislative.diff import diff_since
from tracker.legislative.notify import post as post_slack
from tracker.legislative.scrape import RETENTION_YEARS, scrape_all, scrape_council


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    return date.fromisoformat(s)


def cmd_scrape(args: argparse.Namespace) -> int:
    if args.council == "all":
        results = scrape_all(db_path=args.db, since=_parse_date(args.since))
    else:
        results = [
            scrape_council(
                args.council,
                db_path=args.db,
                since=_parse_date(args.since),
                force_actions=args.refetch_actions,
            )
        ]
    print(json.dumps(results, indent=2))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    out = diff_since(since_iso=args.since, db_path=args.db)
    if args.output:
        Path(args.output).write_text(json.dumps(out, indent=2))
    else:
        print(json.dumps(out, indent=2))
    return 0


def cmd_notify(args: argparse.Namespace) -> int:
    diff_path = Path(args.diff)
    diff = json.loads(diff_path.read_text())
    ok = post_slack(diff, webhook_url=args.webhook, dry_run=args.dry_run)
    return 0 if ok else 1


def cmd_reclassify(args: argparse.Namespace) -> int:
    """Re-run the keyword classifier over bills already in the DB and update
    their subject tags in place.

    No network access — it only re-reads each bill's stored title + raw_subject,
    so it's safe to run when the scraping sources are unavailable. Use after
    editing the rules in classify.py to re-tag the existing inventory, then
    rebuild site/bills.json.
    """
    from tracker.legislative.classify import classify
    from tracker.legislative.db import connect, init_schema

    total = changed = 0
    gained: dict[str, int] = {}
    examples: list[dict] = []
    with connect(args.db) as conn:
        init_schema(conn)
        rows = conn.execute(
            "SELECT id, council, bill_number, title, raw_subject, subjects FROM bills"
        ).fetchall()
        for r in rows:
            total += 1
            old = set(json.loads(r["subjects"] or "[]"))
            cls = classify(r["title"], r["raw_subject"])
            new = set(cls.subjects)
            if new == old:
                continue
            changed += 1
            for s in new - old:
                gained[s] = gained.get(s, 0) + 1
            if (new - old) and len(examples) < args.show:
                examples.append({
                    "council": r["council"],
                    "bill": r["bill_number"],
                    "added": sorted(new - old),
                    "removed": sorted(old - new),
                    "title": (r["title"] or "")[:90],
                })
            if not args.dry_run:
                conn.execute(
                    "UPDATE bills SET subjects = ?, classification_confidence = ? "
                    "WHERE id = ?",
                    (json.dumps(cls.subjects), cls.confidence, r["id"]),
                )
    print(json.dumps({
        "db": str(args.db),
        "dry_run": args.dry_run,
        "bills": total,
        "changed": changed,
        "newly_tagged_by_subject": gained,
        "examples": examples,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    """Enforce the retention window against the stored inventory.

    scrape --council all prunes on its own; this exists to apply the window
    after a one-off backfill, or to preview what it would remove.
    """
    from datetime import timedelta

    from tracker.legislative.db import connect, expired_count, init_schema, prune_expired

    cutoff = (date.today() - timedelta(days=args.years * 365)).isoformat()
    with connect(args.db) as conn:
        init_schema(conn)
        total = conn.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        doomed = expired_count(conn, cutoff)
        by_year = [
            dict(r)
            for r in conn.execute(
                "SELECT substr(COALESCE(introduced_date, first_seen), 1, 4) AS year, "
                "       COUNT(*) AS n FROM bills "
                "WHERE COALESCE(NULLIF(MAX(COALESCE(last_action_date, ''), "
                "      COALESCE(introduced_date, '')), ''), first_seen) < ? "
                "GROUP BY year ORDER BY year",
                (cutoff,),
            )
        ]
        removed = 0 if args.dry_run else prune_expired(conn, cutoff)
    print(json.dumps({
        "cutoff": cutoff,
        "retention_years": args.years,
        "bills_before": total,
        "would_remove" if args.dry_run else "removed": doomed if args.dry_run else removed,
        "remaining": total - (doomed if args.dry_run else removed),
        "removed_by_year": by_year,
    }, indent=2))
    return 0


def cmd_dump_agendas(args: argparse.Namespace) -> int:
    """Save raw agenda text from the Granicus councils (kauai, hawaii) as test
    fixtures, so title-parsing rules can be checked against real documents."""
    import re

    from tracker.legislative.adapters.granicus import GranicusAdapter

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    adapter = GranicusAdapter.for_council(args.council)
    adapter.max_meetings = args.limit
    n = 0
    for mdate, url, text in adapter.iter_raw_agendas():
        if not text.strip():
            continue
        m = re.search(r"(?:clip_id|event_id)=(\d+)", url)
        clip = m.group(1) if m else str(n)
        name = f"{args.council}_{mdate or 'nodate'}_{clip}.txt"
        (out / name).write_text(text)
        print(f"wrote {name} ({len(text)} chars)")
        n += 1
        if n >= args.limit:
            break
    print(f"dumped {n} agendas to {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="tracker.legislative")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scrape", help="scrape one or all councils")
    sp.add_argument("--council", choices=["all", *COUNCILS], default="all")
    sp.add_argument(
        "--since",
        help="YYYY-MM-DD; defaults to the retention window "
        f"({RETENTION_YEARS} years). Older dates are fetched but pruned again.",
    )
    sp.add_argument(
        "--refetch-actions",
        action="store_true",
        help="fetch action history for every bill, not just new/updated "
        "(heavier one-time backfill; single-council only)",
    )
    sp.set_defaults(fn=cmd_scrape)

    dp = sub.add_parser("diff", help="diff against a prior point in time")
    dp.add_argument(
        "--since",
        help="ISO timestamp; if omitted, uses the second-most-recent run",
    )
    dp.add_argument("--output", help="write JSON to this path")
    dp.set_defaults(fn=cmd_diff)

    np = sub.add_parser("notify", help="post a diff to Slack")
    np.add_argument("--diff", required=True, help="path to diff JSON")
    np.add_argument("--webhook", help="Slack webhook URL (or SLACK_WEBHOOK_URL env)")
    np.add_argument("--dry-run", action="store_true")
    np.set_defaults(fn=cmd_notify)

    rp = sub.add_parser(
        "reclassify",
        help="re-tag stored bills with the current keyword rules (no network)",
    )
    rp.add_argument("--dry-run", action="store_true", help="report changes without writing")
    rp.add_argument("--show", type=int, default=20, help="number of example changes to print")
    rp.set_defaults(fn=cmd_reclassify)

    pp = sub.add_parser(
        "prune", help=f"drop records older than the retention window "
        f"({RETENTION_YEARS} years)"
    )
    pp.add_argument("--years", type=int, default=RETENTION_YEARS)
    pp.add_argument(
        "--dry-run", action="store_true", help="report what would be removed"
    )
    pp.set_defaults(fn=cmd_prune)

    da = sub.add_parser(
        "dump-agendas", help="save raw Granicus agenda text as test fixtures"
    )
    da.add_argument("--council", choices=["kauai", "hawaii"], required=True)
    da.add_argument("--out", default="tests/fixtures/agendas")
    da.add_argument("--limit", type=int, default=6)
    da.set_defaults(fn=cmd_dump_agendas)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
