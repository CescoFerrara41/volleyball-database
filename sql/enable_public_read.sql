-- Run this once in the Supabase SQL Editor before deploying the frontend
-- (safe to re-run any time -- e.g. after pulling in new tables -- since
-- every policy is dropped and recreated rather than assumed to be new).
--
-- The frontend uses your project's public "anon"/publishable key, which is
-- safe to expose in client-side code -- but only because Row Level
-- Security (RLS) restricts what that key can actually do. Without these
-- policies, RLS being enabled with no policies would block the key from
-- reading anything at all; these policies open read-only public access to
-- exactly the tables/views the frontend needs, and nothing else (in
-- particular, no INSERT/UPDATE/DELETE policy is granted, so the public
-- key can never write). The `player_search_index` view (see db.py) reads
-- through to players' and player_match_stats' own policies below, since
-- it's declared security_invoker.

ALTER TABLE tournaments ENABLE ROW LEVEL SECURITY;
ALTER TABLE matches ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_match_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE team_match_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE players ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Public read access" ON tournaments;
CREATE POLICY "Public read access" ON tournaments
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read access" ON matches;
CREATE POLICY "Public read access" ON matches
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read access" ON player_match_stats;
CREATE POLICY "Public read access" ON player_match_stats
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read access" ON team_match_stats;
CREATE POLICY "Public read access" ON team_match_stats
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "Public read access" ON players;
CREATE POLICY "Public read access" ON players
    FOR SELECT USING (true);
