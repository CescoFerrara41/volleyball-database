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

## Full historical rebuild (starting fresh)

To wipe everything and rebuild from scratch, one season at a time:

1. **Clear the database.** This deletes all rows but leaves the schema
   intact — you don't need to re-run any Supabase setup steps after.

   ```bash
   python clear_data.py          # asks for confirmation first
   ```

2. **Backfill VIS data for every season at once.** This part is fast
   and reliable (pure API calls, no browser scraping), so there's no
   need to split it by season:

   ```bash
   python backfill.py
   ```

3. **List the tournaments you now have**, to get the exact
   `--tournament-no` value for each season/gender:

   ```bash
   python list_tournaments.py
   ```

4. **Run the player-stats scraper one tournament at a time.** This is
   the step worth doing cautiously — each run takes a while (roughly
   10-15+ minutes per tournament, given the deliberate delays between
   requests) and it's the part that took the most iteration to get
   right. Confirm each season's results look right before moving to
   the next:

   ```bash
   python volleystation_scraper.py --tournament-no <no> --season-path <season>/
   ```

   Omit `--season-path` only for the current season. VNL 2020 was
   cancelled, so there's no tournament for it. Each season has both a
   men's and a women's tournament — that's two runs per season, ~16-18
   runs total for a full 2018-2026 rebuild.

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

## Frontend (GitHub Pages)

The `docs/index.html` file is a self-contained static page — no build
step — that reads live match data straight from Supabase's public API
and renders it as a browsable archive, filterable by gender and
season. It's designed to sit in `docs/` so GitHub Pages can serve it
with zero extra config.

**Why this works without exposing your database credentials:** the
frontend never touches `DATABASE_URL` (that stays a GitHub Actions
secret, used only by the recurring sync job below). Instead it uses
Supabase's public **publishable key** — which is meant to be visible in
client-side code — combined with Row Level Security policies that
only allow reads. Nothing with write access is ever shipped to the
browser.

### Deploy it

1. **Enable public read access** — in the Supabase SQL Editor, run
   `sql/enable_public_read.sql` once. This turns on Row Level Security
   and adds read-only policies for `tournaments` and `matches`.
2. **Get your public API values** — Settings → API Keys (not the
   "Legacy API Keys" tab; click "Create new API Keys" once if you
   don't see a Publishable key yet). Copy the **Project URL** and the
   **Publishable key** (`sb_publishable_...`) — not the Secret key.
3. **Fill them into `docs/index.html`** — near the bottom of the file,
   replace `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` with those values.
4. **Enable GitHub Pages** — in your repo: Settings → Pages → set
   Source to "Deploy from a branch", branch `main`, folder `/docs`.
5. Push. Your site will be live at
   `https://<your-username>.github.io/<repo-name>/` within a minute or
   two.

### Keeping the recurring sync running

Since GitHub Pages can't run Python, `.github/workflows/sync.yml`
runs `sync.py` on a schedule instead (`workflow_dispatch` also lets
you trigger it manually from the Actions tab). To enable it:

1. In your repo: Settings → Secrets and variables → Actions → New
   repository secret.
2. Name it `DATABASE_URL`, value = the same pooler connection string
   from your local `.env`.
3. That's it — the workflow will run on the schedule defined in the
   file (daily by default; edit the cron expression to change it).

This also conveniently solves Supabase's free-tier auto-pause: a
daily sync run counts as database activity, so the project won't go
idle.

## Player-level stats

**Two sources, two different scopes:**
- `scores365_scraper.py` — clean, reliable, but **current season only**
  (365scores' results API has no working way to reach past seasons —
  see its section below).
- `volleystation_scraper.py` — scrapes volleyballworld.com directly,
  the only source with **full historical data**. Its box-score
  extraction reliably returned zero rows in automated runs despite
  working correctly in manual browser testing, across several rounds
  of fixes. The latest fix (see "Stealth measures" below) targets a
  concrete, verified cause — a very blatant automation fingerprint in
  Playwright's default browser — but **has not yet been confirmed
  against the live site** (this project's sandbox can't reach
  volleyballworld.com to test directly). Run it and check the results.

### volleyballworld.com (historical)

`volleystation_scraper.py` scrapes volleyballworld.com's own match
pages directly for per-player stat tables — see the file's docstring
for the full verified DOM structure (table classes, header parsing).

```bash
pip install -r requirements.txt
playwright install chromium
python volleystation_scraper.py --tournament-no 1439 --season-path 2024/
```

**The real bug, found after several wrong turns:** match discovery
used to page through the season week by week via a `#fromDate=`
URL hash, based on the site's own "next week" navigation. That hash
turned out to be **completely ignored on a fresh page load** —
confirmed directly: `#fromDate=2024-05-20` and `#fromDate=2024-06-20`
returned identical results, and the site's own "current week" widget
state was totally decoupled from the URL. Every earlier fix attempt
(stealth patches, rate-limit mitigation, wider date buffers) was
chasing symptoms of a discovery mechanism that was never actually
advancing — every run just re-fetched the same ~28-32 match batch, no
matter how well-behaved the requests looked.

**What actually works:** volleyballworld.com's schedule IDs are
perfectly sequential with VIS's own match order. Confirmed with two
independent real matches for VNL 2024 men's: match #1 → schedule ID
18853, match #101 → 18953 (exactly +100), match #104 → 18956 (exactly
+103, title-verified as "Japan-France", the correct pairing). So
`find_schedule_id_offset()` loads the site's default (no-hash)
schedule page once — which reliably shows the tournament's last few
matches — matches it against VIS's own last few matches by team pair,
and computes a constant offset from that single anchor. Every other
match's schedule ID is then just `offset + (no_in_tournament - 1)`.
No pagination, no week-by-week discovery, no dependency on a UI
mechanism that turned out not to work as assumed.

**Safety check:** after scraping each computed schedule ID, the page's
own title (e.g. "Poland-France Men VNL 2024 29.06.2024") is parsed and
compared against VIS's expected teams before anything is trusted or
stored — protects against any gap or off-by-one in the ID sequence,
same principle as the team-name checks used elsewhere in this project.

The stealth measures (`stealth.py`) and rate-limit mitigations
(`RotatingPage`, jittered delays) documented below are still in place
and still real, verified fixes for what they address — they just
weren't the actual cause of the discovery problem. Worth keeping: a
long, unbroken sequence of automated requests is still a reasonable
thing to avoid regardless.

### 365scores.com (current season)

`scores365_scraper.py` pulls team-level stats and full rosters from
365scores.com's clean JSON API, plus per-player box scores via a
headless browser (the box score itself isn't exposed as JSON, only
discoverable through the rendered page).

```bash
pip install -r requirements.txt
playwright install chromium
python scores365_scraper.py --tournament-no 1616 --gender M
```

**Important scope limit:** this only works for the **current season**.
365scores' results API (`/web/games/results/`) returns a rolling
window of the ~100 most recent games for a competition — four
different parameter attempts to reach past seasons (a season number, a
date range, a larger page size, a pagination cursor) were all silently
ignored. There's no known way to bulk-discover a historical season's
matches through this API. Run this once each season, going forward,
against that season's VIS tournament — it's not a backfill tool.

Matches are linked to VIS the same way as the volleyballworld.com
scraper: by team pair (normalized names), with a repeat pairing
resolved by matching both sides' occurrences in chronological order.

Data lands in two tables:
- `team_match_stats` — one row per team per match (attack, blocks,
  serve points, reception % etc.), tagged `source='365scores'`.
- `player_match_stats` — one row per player per match (all stat
  categories nested together under `stat_category='overall'`, unlike
  the volleyballworld.com approach's one-row-per-category), also
  tagged `source='365scores'`. A `source` column was added so rows
  from different scrapers coexist without ambiguity.

**Verified vs. not:** the JSON parsing, URL construction, and database
writes were all tested directly against real captured API responses
and a real Postgres instance. The one thing genuinely untested end-to-
end is the live browser interaction (clicking the Box Score tab and
team switcher) — the sandbox this was built in can't reach
365scores.com either, so that part needs a real run to confirm.

### Background: how the volleyballworld.com scraper was built

VIS doesn't expose individual player statistics, but volleyballworld.com's
own match pages render them natively — detailed per-player numbers
(scoring, attack, block, serve, reception, dig, set), broken out by
team and by set. `volleystation_scraper.py` scrapes those tables
directly with a headless browser.

An earlier version of this script drove a headless browser through a
separate embedded widget from VolleyStation
(`widgets.volleystation.com`), based on an iframe seen during initial
page inspection. That turned out to be a red herring: the iframe is
transient — on full page load, volleyballworld.com swaps it out for
the same data rendered natively, same-origin, directly on the match
page. Confirmed live against a real 2024 match page: zero
`vbw-widget-iframe` elements present, but 85 real `<table>` elements
containing the same stats with real, discoverable CSS classes
(`vbw-match-player-statistic-table vbw-stats-{category} vbw-set-{set}`).
This version scrapes those tables directly — no cross-origin
navigation, no widget IDs, no tab-clicking required, since every stat
category and set combination is already present in the DOM
simultaneously.

```bash
pip install -r requirements.txt
playwright install chromium
python volleystation_scraper.py --tournament-no 1439 --season-path 2024/
```

`--season-path` matters: omit it only for the current season. Every
past season needs its year, e.g. `2024/`. The schedule listing is also
date-windowed to about one week at a time, so the scraper pages
through the season's weeks automatically to build a full match list —
this happens once per run and adds some time up front.

### How matches get linked across two ID systems

VIS and volleyballworld.com each use their own, unrelated ID for the
same match, and there's no direct lookup between them. An earlier
version of this linked records by comparing VIS's `no_in_tournament`
against the "#N" match number volleyballworld.com shows on its
schedule listing (e.g. "Semi-Finals #101"), on the assumption it was a
tournament-wide running count. **That assumption was wrong** —
confirmed by a real run where VIS match #1 (Bulgaria vs France)
silently collided with an unrelated Bulgaria vs Netherlands match that
happened to show the same "#1" label in a different week. The label is
evidently scoped some other way (per week or per pool, not
determined), not tournament-wide.

Matches are now linked by **team pair** instead — the set of both team
names, normalized for accent/casing differences. Two specific teams
essentially never meet more than once or twice across a season, so
this is a far more reliable key than a label that's already been shown
to repeat. On the rare occasion two teams do meet twice (a real
possibility in VNL's pool format), both sides' matches are sorted by
their own tournament order and paired up in that same order — the
first meeting links to the first meeting, the second to the second,
without depending on the unreliable match-number label at all.

**Team side (A/B) is inferred from DOM order** — the first table for a
given (category, set) combination is treated as Team A, the second as
Team B, since there's no team-specific CSS class distinguishing them.
This held for the one real match checked (Poland listed first in both
the URL and the roster) but wasn't checked against more than one
match — worth spot-checking a few results against the site.

**Before running this at scale:** run it against one tournament first
and spot-check a few rows in `player_match_stats` against what the
site actually shows. The table-parsing logic (class-name parsing,
header-driven column mapping) was tested directly against a
locally-mocked copy of the real verified table structure; the
team-pair matching and rematch-ordering logic was tested directly
against the real collision this bug produced, plus a simulated rare
rematch case. What's *not*
independently verified: the DOM-order team-side assumption above,
and whether every historical season's schedule pages behave
identically to 2024's (older seasons may have a different layout).

This isn't a documented public API, so the scraper adds a ~1.5s delay
between requests — be patient with it, and don't remove that delay.

## Next steps

- An application identifier from FIVB (`vis.sdk@fivb.org`) if usage
  grows past casual/personal scale — see the VIS docs for details.
- Full set-by-set scores in the main `matches` table (currently only
  Set 1 is pulled) — extend `MATCH_FIELDS` in `backfill.py` with the
  remaining set fields once their exact VIS field names are confirmed.
- Break player-stat `set` (currently scraped but folded into
  `raw_values`) into its own `player_match_stats` column, so per-set
  splits are queryable directly rather than just "All Sets".
- A recurring/scheduled version of the player-stats scraper for newly
  finished matches, similar to `sync.py`.
