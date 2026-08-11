# VNL Stats — historical Volleyball Nations League data

A small pipeline that pulls historical and ongoing Volleyball Nations
League (VNL) tournament and match data into a local database, on a
recurring schedule.

## Why the FIVB VIS Web Service instead of scraping a website

FIVB publishes a public, documented web service — the [VIS Web
Service](https://www.fivb.org/VisSDK/VisWebService/) — that returns
structured XML for tournaments, matches, teams, and more. This is a
better foundation than scraping volleyballworld.com's rendered
frontend: it's not dependent on the site's JS bundle or HTML structure
staying stable, and it's explicitly meant for third-party read access.

**Known limitation:** the VIS service exposes match *results* (scores,
teams, dates, venues) cleanly, but not individual *player* statistics
(points scored, attack efficiency, blocks, etc.) as structured fields —
those live in FIVB's official PDF match reports instead. This project
currently covers match-level data only; a player-stats layer would
need PDF parsing (or a fallback scrape of volleyballworld.com's stat
pages) as a separate phase.

## How it works

- **`vis_client.py`** — thin wrapper around the VIS web service's
  single XML-request endpoint.
- **`db.py`** — Postgres schema and connection, for a free
  [Supabase](https://supabase.com) project. Matches are keyed on VIS's
  own match number, alongside VIS's `Version` field.
- **`backfill.py`** — one-time historical load. Discovers every VNL
  tournament (men's, women's, all seasons, finals included) by
  pulling the full tournament list and filtering client-side for
  `VNL` in the tournament code, then loads every match for each.
- **`sync.py`** — the recurring job. Re-checks only the current and
  prior season's tournaments (no point re-hitting 2018 every day) and
  upserts anything changed.

### Idempotency

VIS bumps a `Version` number on a match record whenever anything about
it changes server-side — a stat correction, a rescheduled time, the
final score once a match ends. Both scripts use `INSERT ... ON
CONFLICT ... DO UPDATE ... WHERE EXCLUDED.version != matches.version`,
so re-running either script is always safe: unchanged matches are
skipped, changed ones are updated, and nothing is ever duplicated.
This was verified directly against a real Postgres instance —
re-running with identical data is a no-op, and a version-bumped
update correctly overwrites just the changed row.

### Why Postgres/Supabase instead of a local file

The dataset is tiny (a few thousand match rows, well under Supabase's
free 500 MB limit even accounting for future growth), so storage was
never the constraint. The reason to use a real client-server Postgres
database here — rather than a local SQLite file — is that it's a more
representative production setup: credentials via environment variable,
a connection pooler, a schema that could serve a real API or frontend
directly. That's a more useful thing to have on a portfolio project
than a file that only exists on one machine.

## Setup

1. Create a free project at [supabase.com](https://supabase.com).
2. In your project dashboard, click the **Connect** button (near the
   top of the page). In the panel that opens, copy the **Session
   pooler** or **Transaction pooler** connection string (port 6543) —
   not the direct connection. Replace the `[YOUR-PASSWORD]` placeholder
   in it with your actual database password.
3. Copy `.env.example` to `.env` and paste your connection string into
   `DATABASE_URL`.
4. Install dependencies and run the backfill:

```bash
pip install -r requirements.txt
python backfill.py   # run once, creates the schema and seeds all VNL history
```

`db.py` creates the `tournaments` and `matches` tables automatically
on first connection — no separate migration step needed.

## Running the recurring sync

For a daily/weekly job, schedule `sync.py` with cron:

```
# daily at 6am
0 6 * * * cd /path/to/vnl-stats && python sync.py >> sync.log 2>&1
```

## Next steps

- A frontend/API layer to actually browse the data (this repo is
  ingestion-only so far) — Supabase's auto-generated REST API is a
  quick way to get one for free.
- Player-level stats via PDF match-report parsing.
- An application identifier from FIVB (`vis.sdk@fivb.org`) if usage
  grows past casual/personal scale — see the VIS docs for details.
- Free Supabase projects auto-pause after 7 days without a database
  request — worth adding a scheduled ping (or just relying on your
  daily/weekly `sync.py` cron) to keep it active.