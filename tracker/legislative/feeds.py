"""Generate static Atom feeds from the bill-change log.

Written alongside bills.json at scrape time, so "notifications" need no
backend: any RSS reader (or an RSS-to-email bridge like Blogtrottr) polls
the GitHub Pages URLs.

Layout under site/feeds/:
  all.xml                 every change, site-wide (newest 60)
  {council}.xml           per-county (honolulu/maui/hawaii/kauai, newest 50)
  subject-{subject}.xml   per classified subject (newest 50)
  bill/{slug}.xml         one tiny feed per bill — the unit a reader
                          subscribes to via the site's OPML export

Output is deterministic (feed/entry timestamps come from the change log, not
from "now"), so unchanged bills produce byte-identical files and the scrape
commit only touches feeds that actually changed.
"""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

from tracker.legislative.db import DEFAULT_DB, connect, init_schema

BASE_URL = "https://hawaii-appleseed.github.io/Tracker/"
TAG_AUTHORITY = "tag:hawaii-appleseed.github.io,2026"

COUNCIL_LABEL = {
    "honolulu": "Honolulu",
    "maui": "Maui",
    "hawaii": "Hawaiʻi County",
    "kauai": "Kauaʻi",
}
SUBJECT_LABEL = {
    "tax": "Tax",
    "transportation": "Transportation",
    "food_security": "Food Security",
    "affordable_housing": "Affordable Housing",
}
SITEWIDE_LIMIT = 60
SLICE_LIMIT = 50


def bill_slug(council: str, bill_number: str) -> str:
    """Feed filename slug for one bill. Mirrored in site/app.js (billSlug) —
    the OPML export builds these URLs client-side, so keep both in sync."""
    s = f"{council}-{bill_number}".lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _rfc3339(ts: str) -> str:
    """DB timestamps are already ISO-8601 with offset; Atom wants the same."""
    return ts


def _entry_title(c: dict) -> str:
    label = COUNCIL_LABEL.get(c["council"], c["council"])
    num = c["bill_number"]
    if c["kind"] == "new":
        what = c["new_status"] or c["new_last_action"] or "now tracked"
        return f"New: {num} ({label}) — {what}"
    if (c["old_status"] or c["new_status"]) and c["old_status"] != c["new_status"]:
        return f"{num} ({label}): {c['old_status'] or '—'} → {c['new_status'] or '—'}"
    return f"{num} ({label}): {c['new_last_action'] or 'updated'}"


def _entry_content(c: dict) -> str:
    bits = []
    if c["title"]:
        bits.append(f"<p><strong>{escape(c['title'])}</strong></p>")
    if c["raw_subject"] and c["raw_subject"] != c["title"]:
        bits.append(f"<p>{escape(c['raw_subject'])}</p>")
    if c["kind"] == "update" and c["old_status"] != c["new_status"]:
        bits.append(
            f"<p>Status: {escape(c['old_status'] or '—')} → "
            f"{escape(c['new_status'] or '—')}</p>"
        )
    if c["new_last_action"]:
        bits.append(f"<p>Latest action: {escape(c['new_last_action'])}</p>")
    bits.append(f'<p><a href="{escape(c["url"] or BASE_URL)}">View on the council site</a></p>')
    return "\n".join(bits)


def _entry_xml(c: dict) -> str:
    slug = bill_slug(c["council"], c["bill_number"])
    eid = f"{TAG_AUTHORITY}:{slug}/{c['changed_at']}"
    return f"""  <entry>
    <id>{escape(eid)}</id>
    <title>{escape(_entry_title(c))}</title>
    <updated>{_rfc3339(c['changed_at'])}</updated>
    <link rel="alternate" href="{escape(c['url'] or BASE_URL)}"/>
    <content type="html">{escape(_entry_content(c))}</content>
  </entry>"""


def _feed_xml(title: str, rel_path: str, entries: list[dict], fallback_updated: str) -> str:
    updated = max((e["changed_at"] for e in entries), default=fallback_updated)
    body = "\n".join(_entry_xml(e) for e in entries)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{escape(f"{TAG_AUTHORITY}:{rel_path}")}</id>
  <title>{escape(title)}</title>
  <updated>{_rfc3339(updated)}</updated>
  <link rel="self" href="{escape(BASE_URL + rel_path)}"/>
  <link rel="alternate" href="{escape(BASE_URL)}"/>
{body}
</feed>
"""


def _write_if_changed(path: Path, content: str) -> bool:
    # Bytes, not text: some scraped titles contain \r\n, which read_text()'s
    # universal-newline mode would collapse, making the comparison never match.
    data = content.encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


def build_feeds(db_path: Path = DEFAULT_DB, site_dir: Path | None = None) -> int:
    site_dir = site_dir or Path(__file__).resolve().parents[2] / "site"
    feeds_dir = site_dir / "feeds"
    (feeds_dir / "bill").mkdir(parents=True, exist_ok=True)

    with connect(db_path) as conn:
        init_schema(conn)  # bill_changes may predate this DB's last scrape
        changes = [dict(r) for r in conn.execute(
            """
            SELECT c.kind, c.changed_at, c.old_status, c.new_status,
                   c.old_last_action, c.new_last_action,
                   b.council, b.bill_number, b.title, b.raw_subject,
                   b.url, b.subjects
            FROM bill_changes c JOIN bills b ON b.id = c.bill_id
            WHERE b.matter_class = 'legislation'
            ORDER BY c.changed_at DESC, c.id DESC
            """
        )]
        # Feeds cover legislation only. Communications and committee reports are
        # ingested and searchable on the site, but subscribing to every Maui
        # committee report would make these feeds unreadable.
        bills = [dict(r) for r in conn.execute(
            "SELECT council, bill_number, title, raw_subject, url, status, "
            "       last_action, last_updated FROM bills "
            "WHERE matter_class = 'legislation'"
        )]

    written = 0

    # Site-wide and per-county feeds.
    fallback = max((b["last_updated"] for b in bills), default="1970-01-01T00:00:00+00:00")
    written += _write_if_changed(
        feeds_dir / "all.xml",
        _feed_xml("Hawaiʻi County Bill Tracker — all updates", "feeds/all.xml",
                  changes[:SITEWIDE_LIMIT], fallback),
    )
    for council, label in COUNCIL_LABEL.items():
        subset = [c for c in changes if c["council"] == council][:SLICE_LIMIT]
        written += _write_if_changed(
            feeds_dir / f"{council}.xml",
            _feed_xml(f"Bill Tracker — {label}", f"feeds/{council}.xml", subset, fallback),
        )
    for subject, label in SUBJECT_LABEL.items():
        subset = [c for c in changes if f'"{subject}"' in (c["subjects"] or "")][:SLICE_LIMIT]
        written += _write_if_changed(
            feeds_dir / f"subject-{subject}.xml",
            _feed_xml(f"Bill Tracker — {label} bills", f"feeds/subject-{subject}.xml",
                      subset, fallback),
        )

    # One feed per bill. Bills scraped before the change log existed have no
    # rows yet; seed those feeds with a single "current state" entry so a new
    # subscription isn't empty (its timestamp is the bill's last_updated, so
    # the file is still deterministic).
    by_bill: dict[str, list[dict]] = {}
    for c in changes:
        by_bill.setdefault(bill_slug(c["council"], c["bill_number"]), []).append(c)

    # A few legacy rows are whitespace-duplicates of the same bill ("Bill 153
    # (2024)" twice); they share a slug, so keep only the freshest row per slug.
    per_slug: dict[str, dict] = {}
    for b in bills:
        slug = bill_slug(b["council"], b["bill_number"])
        cur = per_slug.get(slug)
        if cur is None or (b["last_updated"] or "") > (cur["last_updated"] or ""):
            per_slug[slug] = b

    for slug, b in per_slug.items():
        entries = by_bill.get(slug)
        if not entries:
            entries = [{
                "kind": "new", "changed_at": b["last_updated"],
                "old_status": None, "new_status": b["status"],
                "old_last_action": None, "new_last_action": b["last_action"],
                "council": b["council"], "bill_number": b["bill_number"],
                "title": b["title"], "raw_subject": b["raw_subject"],
                "url": b["url"], "subjects": None,
            }]
        label = COUNCIL_LABEL.get(b["council"], b["council"])
        written += _write_if_changed(
            feeds_dir / "bill" / f"{slug}.xml",
            _feed_xml(f"{b['bill_number']} — {label}", f"feeds/bill/{slug}.xml",
                      entries, b["last_updated"]),
        )

    total = 9 + len(per_slug)
    print(f"Feeds: {written} written/updated of {total} ({len(changes)} logged changes)")
    return written
