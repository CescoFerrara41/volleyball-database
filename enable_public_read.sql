-- Run this once in the Supabase SQL Editor before deploying the frontend.
--
-- The frontend uses your project's public "anon" key, which is safe to
-- expose in client-side code -- but only because Row Level Security (RLS)
-- restricts what that key can actually do. Without these policies, RLS
-- being enabled with no policies would block the anon key from reading
-- anything at all; these two policies open read-only public access to
-- exactly the two tables the frontend needs, and nothing else (in
-- particular, no INSERT/UPDATE/DELETE policy is granted, so the public
-- key can never write).

ALTER TABLE tournaments ENABLE ROW LEVEL SECURITY;
ALTER TABLE matches ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Public read access" ON tournaments
    FOR SELECT USING (true);

CREATE POLICY "Public read access" ON matches
    FOR SELECT USING (true);
