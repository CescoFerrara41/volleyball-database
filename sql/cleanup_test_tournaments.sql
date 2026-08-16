-- Run this once in the Supabase SQL Editor if you already ran backfill.py
-- before discover_vnl_tournaments() started filtering out test/placeholder
-- tournaments (see backfill.py). Safe to re-run -- it's a no-op once the
-- test rows are gone.
--
-- Matches have a NOT NULL FK to tournaments with no ON DELETE CASCADE, so
-- matches must be deleted before their parent tournament row.

DELETE FROM matches
WHERE tournament_no IN (
    SELECT no FROM tournaments WHERE code !~ '^[MW]VNL[0-9]{2,4}F?$'
);

DELETE FROM tournaments
WHERE code !~ '^[MW]VNL[0-9]{2,4}F?$';
