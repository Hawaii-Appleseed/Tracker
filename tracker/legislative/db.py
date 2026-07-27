import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from tracker.legislative import matter_class
from tracker.legislative.adapters.base import ActionRecord, BillRecord

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "bills.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY,
  council TEXT NOT NULL,
  bill_number TEXT NOT NULL,
  title TEXT,
  bill_type TEXT,
  introducer TEXT,
  introduced_date TEXT,
  status TEXT,
  last_action TEXT,
  last_action_date TEXT,
  url TEXT,
  raw_subject TEXT,
  committee TEXT,
  matter_class TEXT NOT NULL DEFAULT 'legislation',
  subjects TEXT NOT NULL DEFAULT '[]',
  classification_confidence REAL,
  first_seen TEXT NOT NULL,
  last_updated TEXT NOT NULL,
  UNIQUE(council, bill_number)
);

CREATE INDEX IF NOT EXISTS idx_bills_council ON bills(council);
CREATE INDEX IF NOT EXISTS idx_bills_introduced_date ON bills(introduced_date);

CREATE TABLE IF NOT EXISTS bill_actions (
  id INTEGER PRIMARY KEY,
  bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  action_date TEXT,
  action TEXT,
  committee TEXT,
  UNIQUE(bill_id, action_date, action)
);

-- Granicus agenda cache. Hawaii County and Kauai have no bill API — their
-- inventory is reconstructed from meeting agendas. Fetching an agenda needs a
-- headless browser, so each one is parsed once and its bill mentions kept
-- here; the adapter then assembles the full since-window from this cache plus
-- whatever agendas are new (or recent enough to still be amended).
CREATE TABLE IF NOT EXISTS agenda_fetches (
  id INTEGER PRIMARY KEY,
  council TEXT NOT NULL,
  agenda_url TEXT NOT NULL,
  meeting_date TEXT,
  fetched_at TEXT NOT NULL,
  UNIQUE(council, agenda_url)
);

CREATE TABLE IF NOT EXISTS agenda_mentions (
  id INTEGER PRIMARY KEY,
  council TEXT NOT NULL,
  agenda_url TEXT NOT NULL,
  meeting_date TEXT,
  bill_number TEXT NOT NULL,
  bill_type TEXT,
  title TEXT,
  summary TEXT,
  stage TEXT
);

CREATE INDEX IF NOT EXISTS idx_agenda_mentions_council
  ON agenda_mentions(council, meeting_date);
CREATE INDEX IF NOT EXISTS idx_agenda_mentions_url
  ON agenda_mentions(council, agenda_url);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  council TEXT,
  bills_seen INTEGER DEFAULT 0,
  bills_new INTEGER DEFAULT 0,
  bills_updated INTEGER DEFAULT 0,
  errors TEXT
);

-- Per-bill change history, written whenever upsert_bill inserts a bill or
-- applies an update. diff_since() only knows a bill changed within the
-- two-run window; this log keeps WHAT changed (old -> new status/action)
-- durably, which is what the Atom feeds are built from.
CREATE TABLE IF NOT EXISTS bill_changes (
  id INTEGER PRIMARY KEY,
  bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
  changed_at TEXT NOT NULL,
  kind TEXT NOT NULL,             -- 'new' | 'update'
  old_status TEXT,
  new_status TEXT,
  old_last_action TEXT,
  new_last_action TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_bill ON bill_changes(bill_id);
CREATE INDEX IF NOT EXISTS idx_changes_at ON bill_changes(changed_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: Path = DEFAULT_DB):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


# Columns added after the original schema shipped. CREATE TABLE IF NOT EXISTS
# is a no-op against an existing table, so an already-populated bills.db needs
# them added explicitly rather than picking them up from SCHEMA.
_ADDED_COLUMNS = (
    ("committee", "TEXT"),
    ("matter_class", "TEXT NOT NULL DEFAULT 'legislation'"),
)

# Indexes over added columns must be created after the ALTERs, not from SCHEMA:
# executescript() runs the whole script against the pre-migration table, so an
# index on matter_class there fails with "no such column" on an existing DB.
_ADDED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_bills_matter_class ON bills(matter_class)",
)


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(bills)")}
    for col, ddl in _ADDED_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE bills ADD COLUMN {col} {ddl}")
    for ddl in _ADDED_INDEXES:
        conn.execute(ddl)


def upsert_bill(
    conn: sqlite3.Connection,
    bill: BillRecord,
    subjects: list[str],
    confidence: float | None,
) -> tuple[int, bool, bool]:
    """Insert or update a bill. Returns (bill_id, is_new, was_updated)."""
    now = _now()
    subjects_json = json.dumps(subjects)
    mclass = matter_class(bill.bill_type)

    existing = conn.execute(
        "SELECT id, status, last_action, last_action_date, title, url, raw_subject, "
        "       committee "
        "FROM bills WHERE council = ? AND bill_number = ?",
        (bill.council, bill.bill_number),
    ).fetchone()

    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO bills (
              council, bill_number, title, bill_type, introducer, introduced_date,
              status, last_action, last_action_date, url,
              raw_subject, committee, matter_class,
              subjects, classification_confidence,
              first_seen, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bill.council, bill.bill_number, bill.title, bill.bill_type,
                bill.introducer, bill.introduced_date, bill.status,
                bill.last_action, bill.last_action_date, bill.url,
                bill.raw_subject, bill.committee, mclass,
                subjects_json, confidence, now, now,
            ),
        )
        conn.execute(
            "INSERT INTO bill_changes (bill_id, changed_at, kind, new_status, new_last_action) "
            "VALUES (?, ?, 'new', ?, ?)",
            (cur.lastrowid, now, bill.status, bill.last_action),
        )
        return cur.lastrowid, True, False

    changed = (
        existing["status"] != bill.status
        or existing["last_action"] != bill.last_action
        or existing["last_action_date"] != bill.last_action_date
        or existing["title"] != bill.title
        or existing["url"] != bill.url
        or existing["raw_subject"] != bill.raw_subject
        or existing["committee"] != bill.committee
    )
    if changed:
        conn.execute(
            """
            UPDATE bills SET
              title = ?, bill_type = ?, introducer = ?, status = ?,
              last_action = ?, last_action_date = ?, url = ?,
              raw_subject = ?, committee = ?, matter_class = ?,
              subjects = ?, classification_confidence = ?,
              last_updated = ?
            WHERE id = ?
            """,
            (
                bill.title, bill.bill_type, bill.introducer, bill.status,
                bill.last_action, bill.last_action_date, bill.url,
                bill.raw_subject, bill.committee, mclass,
                subjects_json, confidence,
                now, existing["id"],
            ),
        )
        # Log only movement a reader would care about (status / latest action);
        # title- or summary-only edits update the bill but make no feed entry.
        if (existing["status"] != bill.status
                or existing["last_action"] != bill.last_action):
            conn.execute(
                "INSERT INTO bill_changes (bill_id, changed_at, kind, "
                "  old_status, new_status, old_last_action, new_last_action) "
                "VALUES (?, ?, 'update', ?, ?, ?, ?)",
                (
                    existing["id"], now,
                    existing["status"], bill.status,
                    existing["last_action"], bill.last_action,
                ),
            )
    return existing["id"], False, changed


def upsert_actions(
    conn: sqlite3.Connection, bill_id: int, actions: Iterable[ActionRecord]
) -> int:
    n = 0
    for a in actions:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO bill_actions (bill_id, action_date, action, committee)
            VALUES (?, ?, ?, ?)
            """,
            (bill_id, a.action_date, a.action, a.committee),
        )
        n += cur.rowcount or 0
    return n


# A record's age is its most recent date, not its introduction date: a bill
# filed four years ago that saw action last month is still live and must
# survive the window. first_seen is the fallback for agenda-derived records
# that carry no dates at all.
_AGE_EXPR = """
    COALESCE(
      NULLIF(MAX(COALESCE(last_action_date, ''), COALESCE(introduced_date, '')), ''),
      first_seen
    )
"""


def expired_count(conn: sqlite3.Connection, cutoff_iso: str) -> int:
    """How many records prune_expired() would remove. For dry runs."""
    return conn.execute(
        f"SELECT COUNT(*) FROM bills WHERE {_AGE_EXPR} < ?", (cutoff_iso,)
    ).fetchone()[0]


def prune_expired(
    conn: sqlite3.Connection, cutoff_iso: str | None = None, years: int = 3
) -> int:
    """Drop records that have aged out of the retention window.

    Sources reach much further back than the tracker keeps, and a scrape run
    with an explicit old --since would otherwise leave that history behind
    permanently. Child rows (actions, changes) go with them via ON DELETE
    CASCADE, which requires PRAGMA foreign_keys — connect() sets it.
    """
    if cutoff_iso is None:
        cutoff_iso = (date.today() - timedelta(days=years * 365)).isoformat()
    cur = conn.execute(f"DELETE FROM bills WHERE {_AGE_EXPR} < ?", (cutoff_iso,))
    return cur.rowcount or 0


def start_run(conn: sqlite3.Connection, council: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO runs (started_at, council) VALUES (?, ?)",
        (_now(), council),
    )
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    seen: int,
    new: int,
    updated: int,
    errors: list[str] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE runs SET completed_at = ?, bills_seen = ?, bills_new = ?,
                       bills_updated = ?, errors = ?
        WHERE id = ?
        """,
        (
            _now(), seen, new, updated,
            json.dumps(errors) if errors else None,
            run_id,
        ),
    )


# An agenda can be amended up to (and shortly after) its meeting; past that
# horizon its text is settled and a cached parse is as good as a re-fetch.
AGENDA_FRESH_DAYS = 7


class AgendaStore:
    """Cache of parsed Granicus agenda mentions for one council.

    `save()` records that an agenda was fetched and replaces its mentions;
    `is_fresh()` says whether an agenda can be skipped this run; `load()`
    returns every cached mention in a date window, shaped exactly like
    GranicusAdapter._parse_agenda output (plus `date`/`url`), so the adapter
    can merge cached and freshly parsed agendas identically.

    `refetch=True` disables skipping — use after changing the agenda-parsing
    rules, since only mentions (not raw agenda text) are cached.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        council: str,
        refetch: bool = False,
        fresh_days: int = AGENDA_FRESH_DAYS,
    ):
        self.conn = conn
        self.council = council
        self.refetch = refetch
        self.fresh_days = fresh_days

    def is_fresh(self, agenda_url: str, meeting_date: str | None) -> bool:
        if self.refetch:
            return False
        if not meeting_date:  # unknown date — can't tell, re-fetch
            return False
        settled = (date.today() - timedelta(days=self.fresh_days)).isoformat()
        if meeting_date >= settled:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM agenda_fetches WHERE council = ? AND agenda_url = ?",
            (self.council, agenda_url),
        ).fetchone()
        return row is not None

    def save(self, agenda_url: str, meeting_date: str | None, mentions: list[dict]) -> None:
        self.conn.execute(
            """
            INSERT INTO agenda_fetches (council, agenda_url, meeting_date, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(council, agenda_url) DO UPDATE SET
              meeting_date = excluded.meeting_date, fetched_at = excluded.fetched_at
            """,
            (self.council, agenda_url, meeting_date or "", _now()),
        )
        self.conn.execute(
            "DELETE FROM agenda_mentions WHERE council = ? AND agenda_url = ?",
            (self.council, agenda_url),
        )
        self.conn.executemany(
            """
            INSERT INTO agenda_mentions
              (council, agenda_url, meeting_date, bill_number, bill_type,
               title, summary, stage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    self.council, agenda_url, m.get("date") or meeting_date or "",
                    m["bill_number"], m.get("bill_type"), m.get("title"),
                    m.get("summary"), m.get("stage"),
                )
                for m in mentions
            ],
        )

    def load(self, since_iso: str | None = None) -> list[dict]:
        q = (
            "SELECT agenda_url, meeting_date, bill_number, bill_type, title, "
            "       summary, stage FROM agenda_mentions WHERE council = ?"
        )
        params: list = [self.council]
        if since_iso:
            q += " AND (meeting_date = '' OR meeting_date >= ?)"
            params.append(since_iso)
        return [
            {
                "bill_number": r["bill_number"],
                "bill_type": r["bill_type"],
                "title": r["title"],
                "summary": r["summary"],
                "stage": r["stage"],
                "date": r["meeting_date"],
                "url": r["agenda_url"],
            }
            for r in self.conn.execute(q, params)
        ]


def last_completed_run(
    conn: sqlite3.Connection, council: str | None = None
) -> sqlite3.Row | None:
    if council:
        return conn.execute(
            "SELECT * FROM runs WHERE council = ? AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1",
            (council,),
        ).fetchone()
    return conn.execute(
        "SELECT * FROM runs WHERE completed_at IS NOT NULL "
        "ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()
