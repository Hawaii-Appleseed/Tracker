# Tracker

Civic-data tracking tools.

## Modules

### Legislative Tracker

A searchable archive of what Hawaii's four county councils — Honolulu, Maui, Hawaii County, Kauai — publish: bills, resolutions, ordinances, and the communications and committee reports around them, over a rolling **3-year window**.

- Daily scrape of all four councils
- Full-text search across every county, ranked by relevance
- Bills additionally tagged into four subject areas (**Tax**, **Transportation**, **Food Security**, **Affordable Housing**) via keyword rules, as an optional filter
- New bills and status changes posted to Slack; per-bill and per-subject Atom feeds
- Browsable static dashboard: <https://dtomkatsu.github.io/Tracker/>

#### What gets ingested

Everything each source publishes is stored, then bucketed by `matter_class` so the dashboard can default to legislation without discarding the rest:

| Class | Examples | In default view |
|---|---|---|
| `legislation` | Bill, Resolution, Ordinance | yes |
| `communication` | County Communication, Direct Referral, Rule 7(B) | no — opt in via **Records** |
| `procedural` | Committee Report, Minutes, Ceremonial Resolution | no — opt in via **Records** |

Unknown matter types fall back to `legislation` deliberately: an unrecognized type is more likely a council-specific name for real legislation than it is noise, and the cost of guessing wrong is a stray row rather than a silently missing bill.

#### Stack

- Python 3.11+ (requests, beautifulsoup4, pydantic, playwright)
- SQLite for storage (`data/bills.db`)
- Vanilla JS static dashboard, hosted on GitHub Pages
- Search runs entirely in the browser: `site_build.py` emits a minified corpus and the front-end builds a field-weighted inverted index on load. No search backend.

#### Site payload

`site_build.py` writes two artifacts, split because the search corpus must arrive before the table can render at all:

- `site/bills.json` — every bill, minified, **without** action histories
- `site/actions/<council>.json` — action timelines, fetched lazily when a row is expanded (and prefetched on idle)

Both must be committed for the live site to work — see the `git add` line in `scripts/daily_scrape.sh`.

#### Quick start

```bash
uv sync                                          # or: pip install -e .
python -m tracker.legislative scrape --council maui
python -m tracker.legislative scrape --council all   # all four, then prunes
python -m tracker.legislative prune --dry-run    # preview the retention window
python -m tracker.legislative diff --since 2026-01-01
python -m tracker.legislative reclassify         # re-tag subjects, no network
python site_build.py
python -m http.server -d site 8000               # preview dashboard
```

#### Retention

`RETENTION_YEARS = 3` (in `tracker/legislative/scrape.py`) is the single source of truth. It bounds both ends:

- **Fetching** — `scrape` defaults `--since` to the window, so adapters never pull older records.
- **Keeping** — `scrape --council all` runs `prune_expired()` afterwards, deleting anything that has aged out. Passing an older `--since` explicitly will fetch more, but the next prune drops it again.

A record's age is its *most recent* date, not its introduction date, so a bill filed four years ago that saw action last month survives. Child rows (actions, changes) go with it via `ON DELETE CASCADE`.

The sources reach much further back than this — Maui's Legistar API to Nov 2015, Honolulu's browse index to 2017 — but 3 years covers the sitting council's term plus the one before it, which is the horizon people actually search.

#### Data sources

All are bounded by the 3-year retention window, not by what the source holds.

| Council | Source | Method |
|---|---|---|
| Maui | `webapi.legistar.com/v1/mauicounty/` | Legistar InSite JSON API |
| Honolulu | `hnldoc.ehawaii.gov/hnldoc` | Undocumented JSON browse endpoints + HTML measure pages for action history |
| Hawaii County | `records.hawaiicounty.gov` (Laserfiche) + `hawaiicounty.granicus.com` | Laserfiche WebLink 11 JSON API for metadata, titles borrowed from Granicus agenda PDFs |
| Kauai | `kauai.granicus.com` | Granicus agenda HTML parsed via headless Chromium |

Notes:

- Hawaii County and Kauai have no bill API (their Legistar tenants are unprovisioned), so their inventory comes from council meeting agendas. For Hawaii County the Laserfiche document index is the spine and agendas supply titles; Kauai is agendas only.
- Hawaii County's Laserfiche template carries no title field (titles live only in scanned PDFs), so titles are joined in from Granicus agendas. Records that never hit an agenda are still ingested, without a title.
- Hawaii County reuses bill and resolution numbers every council term, so its keys are term-qualified (`Bill 148 (2024-2026)`) to keep them unique and stable across a term rollover.
- The two WAFs are inverted: Laserfiche (Barracuda) blocks headless browsers but allows plain `requests`; Granicus blocks bare HTTP but tolerates real Chromium.
- The authoritative daily scrape runs from a local launchd job on a residential IP, **not** GitHub Actions — Hawaii County's WAF blocks datacenter IPs. See `.github/workflows/scrape.yml`.

#### Agenda cache

Rendering a Granicus agenda needs a headless browser, and Hawaii County has ~450 in a 3-year window — an hour of cold PDF renders. So parsed agendas are cached in the DB (`agenda_fetches` / `agenda_mentions`): each is parsed once, and a run only re-reads agendas that are new or recent enough to still be amended. The window is then assembled from cache plus fresh parses, so the result matches a cold crawl.

A bill's agenda appearances double as its action history for these councils. After changing the agenda-parsing rules, re-run with `--refetch-agendas` to rebuild the cache — only parsed mentions are stored, not raw agenda text.

All council bill data is public record.
