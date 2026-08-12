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
"""

import os

import psycopg2
from dotenv import load_dotenv

load_dotenv()

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
    pool_code           TEXT,                 -- VIS's phase/pool code, e.g. 'F1-2', 'F3-4', 'QF'
    pool_name           TEXT,
    version             BIGINT NOT NULL,      -- VIS's own change-tracking version
    last_synced_at      TIMESTAMPTZ NOT NULL
);

-- Added after the initial release; ALTER instead of adding to the CREATE
-- TABLE above so this still applies to databases that already ran it.
ALTER TABLE matches ADD COLUMN IF NOT EXISTS pool_code TEXT;
ALTER TABLE matches ADD COLUMN IF NOT EXISTS pool_name TEXT;

CREATE INDEX IF NOT EXISTS idx_matches_tournament ON matches(tournament_no);
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