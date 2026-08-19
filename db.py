"""
Postgres connection + schema, for a free Supabase project.

Design notes (same idempotency strategy as before, now on Postgres):

- Matches are keyed on VIS's own match number (`no`), alongside VIS's
  `version` field. VIS bumps `version` whenever a match record changes
  server-side. We only overwrite a row if the incoming version differs
  from what's stored -- Postgres's ON CONFLICT ... DO UPDATE ... WHERE
  syntax is essentially identical to SQLite's, so this logic carries
  over unchanged from the original SQLite version of this project.
- Connection info comes from a single DATABASE_URL environment
  variable rather than being hardcoded, so credentials never end up
  in source control. See .env.example.
- Every script in this project imports get_connection from here, which
  makes this the one place to fix Windows' console encoding for all of
  them at once (see below) rather than repeating it in each entry point.
"""

import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

# Windows' console defaults to the cp1252 codepage, which can't encode a
# lot of real data this project prints -- team/player names with
# diacritics (e.g. "Turkiye", "Poreba"), or even a stray symbol in a
# scraped page's own text. Confirmed live: without this, a script crashes
# with UnicodeEncodeError the first time one of those reaches print(),
# which (for a script mid-loop, e.g. the scrapers) silently truncates
# whatever hadn't been reported yet -- looks like "an error" but is really
# just this. Every script imports get_connection from here, so fixing it
# in one place covers all of them.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DATABASE_URL = os.environ.get("DATABASE_URL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    no      BIGINT PRIMARY KEY,    -- VIS tournament number
    code    TEXT NOT NULL,         -- e.g. 'MVNL2024'
    name    TEXT NOT NULL,
    season  TEXT NOT NULL,
    gender  TEXT NOT NULL          -- 'M' or 'W', derived from code prefix
);

CREATE TABLE IF NOT EXISTS matches (
    no                  BIGINT PRIMARY KEY,   -- VIS match number
    tournament_no       BIGINT NOT NULL REFERENCES tournaments(no),
    no_in_tournament    INTEGER,
    date_local          TEXT,
    team_a_name         TEXT,
    team_b_name         TEXT,
    match_points_a      INTEGER,
    match_points_b      INTEGER,
    points_team_a_set1  INTEGER,
    points_team_b_set1  INTEGER,
    version             BIGINT NOT NULL,      -- VIS's own change-tracking version
    last_synced_at      TIMESTAMPTZ NOT NULL
);

-- Added after the table already existed in deployed projects, so these
-- use ADD COLUMN IF NOT EXISTS rather than living in the CREATE TABLE
-- above -- CREATE TABLE IF NOT EXISTS is a no-op against a table that's
-- already there, so new columns need an explicit migration step like
-- this to reach an existing database.
ALTER TABLE matches ADD COLUMN IF NOT EXISTS pool_code TEXT;
ALTER TABLE matches ADD COLUMN IF NOT EXISTS pool_name TEXT;

CREATE INDEX IF NOT EXISTS idx_matches_tournament ON matches(tournament_no);

-- Player-level stats, scraped from VolleyStation's team-stats widget
-- (see volleystation_scraper.py) since VIS itself doesn't expose these.
-- One row per (match, player, team side, stat category) -- e.g. a
-- player's "attack" row and "serve" row for the same match are two
-- separate rows, not two columns, since which numeric columns are
-- meaningful varies by category (attack has Points/Errors/Attempts/
-- Total/Efficiency; dig has Digs/Errors/Total/Efficiency; etc).
-- raw_values keeps the widget's own column headers as keys (e.g.
-- {"Point": "5", "Errors": "1", "Efficiency %": "27.78"}) instead of
-- forcing every category into the same fixed set of numbered columns.
CREATE TABLE IF NOT EXISTS player_match_stats (
    id              BIGSERIAL PRIMARY KEY,
    match_no        BIGINT NOT NULL REFERENCES matches(no),
    player_vw_id    BIGINT,     -- numeric player id from whichever source scraped this row
    player_name     TEXT NOT NULL,
    team_side       TEXT NOT NULL,   -- 'A' or 'B', matches matches.team_a_name / team_b_name
    stat_category   TEXT NOT NULL,   -- e.g. 'scoring', 'attack' (volleyballworld) or 'overall' (365scores box score)
    raw_values      JSONB NOT NULL,
    scraped_at      TIMESTAMPTZ NOT NULL,
    UNIQUE (match_no, player_name, team_side, stat_category)
);

CREATE INDEX IF NOT EXISTS idx_player_stats_match ON player_match_stats(match_no);

-- Which source produced a player_match_stats row -- added after rows from
-- an earlier volleyballworld.com-based scraper may already exist, so
-- existing rows default to that source rather than being left ambiguous.
ALTER TABLE player_match_stats ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'volleyballworld';

-- volleystation_scraper.py scrapes six table variants per stat category --
-- 'all' (the whole match) plus one per individual set ('1'-'5') -- but the
-- original UNIQUE constraint above didn't include which one a row came
-- from. Every set variant collided on the same (match_no, player_name,
-- team_side, stat_category) key and overwrote the others on conflict, so
-- whichever set the scraper happened to process last silently clobbered
-- the real match total (confirmed live: a player's stored 'scoring' row
-- matched their Set 5 total, not their All-Sets total). Existing rows
-- can't be retroactively attributed to a real set (that information was
-- already lost when they were overwritten), so they're marked 'unknown'
-- rather than mislabeled 'all' -- see README for the required re-scrape.
ALTER TABLE player_match_stats ADD COLUMN IF NOT EXISTS set_id TEXT NOT NULL DEFAULT 'unknown';

DO $$
DECLARE
    old_constraint text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'player_match_stats_unique_row') THEN
        SELECT conname INTO old_constraint
        FROM pg_constraint
        WHERE conrelid = 'player_match_stats'::regclass AND contype = 'u';

        IF old_constraint IS NOT NULL THEN
            EXECUTE format('ALTER TABLE player_match_stats DROP CONSTRAINT %I', old_constraint);
        END IF;

        ALTER TABLE player_match_stats ADD CONSTRAINT player_match_stats_unique_row
            UNIQUE (match_no, player_name, team_side, stat_category, set_id);
    END IF;
END $$;

-- Team-level match stats (attack, blocks, serve points, reception % etc.),
-- from 365scores.com's clean JSON API -- see scores365_scraper.py. Kept
-- separate from player_match_stats since it's a different grain (one row
-- per team per match, not per player).
CREATE TABLE IF NOT EXISTS team_match_stats (
    id          BIGSERIAL PRIMARY KEY,
    match_no    BIGINT NOT NULL REFERENCES matches(no),
    team_side   TEXT NOT NULL,   -- 'A' or 'B'
    raw_values  JSONB NOT NULL,
    scraped_at  TIMESTAMPTZ NOT NULL,
    source      TEXT NOT NULL DEFAULT '365scores',
    UNIQUE (match_no, team_side)
);

CREATE INDEX IF NOT EXISTS idx_team_stats_match ON team_match_stats(match_no);

-- Real player identity/bio, scraped once per unique player from their own
-- volleyballworld.com profile page (see player_bios_scraper.py) -- match
-- box-score tables only ever show a bare last name, not first+last, so
-- this is a separate scrape keyed on the same player_vw_id used in
-- player_match_stats. Replaces an earlier version of this project's
-- `players`, which was just a VIEW deriving a name from player_match_stats
-- (i.e. that same bare last name) -- a real table with real full names is
-- strictly better, so the view is dropped in favor of it. Guarded by
-- relkind so this is a no-op once `players` is already the real table
-- (DROP VIEW errors if the name now refers to a table, not a view).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'players' AND relkind = 'v'
    ) THEN
        DROP VIEW players;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS players (
    player_vw_id  BIGINT PRIMARY KEY,
    full_name     TEXT NOT NULL,
    first_name    TEXT,   -- derived from full_name minus last_name; null if that split didn't cleanly apply
    last_name     TEXT,
    position      TEXT,
    nationality   TEXT,
    birth_date    DATE,
    height_cm     INTEGER,
    scraped_at    TIMESTAMPTZ NOT NULL
);

-- Search-index view combining real names with a match count -- kept
-- separate from the `players` table itself so the frontend can fetch a
-- small, ready-to-search list without joining/aggregating client-side.
-- security_invoker so it respects the querying role's own RLS grants
-- (see sql/enable_public_read.sql) rather than the view owner's, per
-- Postgres 15+ view/RLS semantics.
--
-- FULL OUTER JOIN (not a simple join from `players`) is deliberate:
-- player_bios_scraper.py runs as its own multi-hour pass, separate from
-- (and likely lagging behind) volleystation_scraper.py's match-stats
-- scrape -- a player with match stats but no bio row yet still needs to
-- show up in search, just under their bare scraped surname instead of a
-- full name, rather than silently vanishing until every player's been
-- bio-scraped. full_name upgrades to the real one automatically as
-- player_bios_scraper.py fills in `players` over time.
CREATE OR REPLACE VIEW player_search_index WITH (security_invoker = true) AS
SELECT
    COALESCE(p.player_vw_id, m.player_vw_id) AS player_vw_id,
    COALESCE(p.full_name, m.player_name) AS full_name,
    p.position,
    COALESCE(m.matches_played, 0) AS matches_played
FROM players p
FULL OUTER JOIN (
    SELECT
        player_vw_id,
        (array_agg(player_name ORDER BY scraped_at DESC))[1] AS player_name,
        COUNT(DISTINCT match_no) FILTER (WHERE set_id = 'all') AS matches_played
    FROM player_match_stats
    WHERE player_vw_id IS NOT NULL
    GROUP BY player_vw_id
) m ON m.player_vw_id = p.player_vw_id;
"""


def get_connection():
    """
    Return a psycopg2 connection with the schema already ensured.

    Requires DATABASE_URL to be set (see .env.example) -- grab this from
    your Supabase project via the "Connect" button on the project
    dashboard (Session pooler or Transaction pooler tab). Prefer the
    pooler over the direct connection if you'll be running this from a
    serverless/scheduled context (e.g. GitHub Actions), since pooled
    connections handle short-lived scripts better.
    """
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in "
            "your Supabase connection string."
        )

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
    conn.commit()
    return conn
