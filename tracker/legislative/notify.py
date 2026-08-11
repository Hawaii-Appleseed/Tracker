"""Slack notifier — posts diff summary to a Slack incoming webhook."""

from __future__ import annotations

import json
import os
import sys

import requests

PAGES_BASE = os.environ.get(
    "TRACKER_PAGES_BASE", "https://hawaii-appleseed.github.io/Tracker"
)

_SUBJECT_LABEL = {
    "tax": "Tax",
    "transportation": "Transportation",
    "food_security": "Food Security",
    "affordable_housing": "Affordable Housing",
}

_COUNCIL_LABEL = {
    "honolulu": "Honolulu",
    "maui": "Maui",
    "hawaii": "Hawaii County",
    "kauai": "Kauai",
}


def _fmt_subjects(subjects: list[str]) -> str:
    if not subjects:
        return "Unclassified"
    return ", ".join(_SUBJECT_LABEL.get(s, s) for s in subjects)


def render_message(diff: dict) -> dict:
    new = diff.get("new", [])
    updated = diff.get("updated", [])
    if not new and not updated:
        return {"text": ":sleeping: No new council bill activity since last run."}

    lines = [
        f":scroll: *County bills update* — {len(new)} new, {len(updated)} status changes"
    ]
    for b in new[:20]:
        council = _COUNCIL_LABEL.get(b["council"], b["council"])
        subj = _fmt_subjects(b.get("subjects") or [])
        title = (b.get("title") or "").strip() or "(no title)"
        lines.append(
            f"• *{council} {b['bill_number']}* (NEW) — {subj}\n  {title}\n  <{b['url']}|source>"
        )
    if len(new) > 20:
        lines.append(f"…and {len(new) - 20} more new.")

    for b in updated[:20]:
        council = _COUNCIL_LABEL.get(b["council"], b["council"])
        subj = _fmt_subjects(b.get("subjects") or [])
        last = b.get("last_action") or b.get("status") or "updated"
        lines.append(
            f"• *{council} {b['bill_number']}* — {subj} — _{last}_\n  <{b['url']}|source>"
        )
    if len(updated) > 20:
        lines.append(f"…and {len(updated) - 20} more changes.")

    lines.append(f"\n<{PAGES_BASE}/|Browse all tracked bills →>")
    return {"text": "\n".join(lines)}


def post(diff: dict, webhook_url: str | None = None, dry_run: bool = False) -> bool:
    msg = render_message(diff)
    if dry_run:
        print(json.dumps(msg, indent=2))
        return True
    webhook_url = webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        print("SLACK_WEBHOOK_URL not set; skipping post.", file=sys.stderr)
        return False
    r = requests.post(webhook_url, json=msg, timeout=15)
    r.raise_for_status()
    return True
