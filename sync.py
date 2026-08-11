"""
Recurring incremental sync: re-check active VNL tournaments for new or
updated matches. Meant to be run on a schedule (daily/weekly via cron)
once backfill.py has been run at least once.

    python sync.py

Only tournaments from the current season (and the prior one, in case a
Finals tournament is still being finalized) are re-checked — no point
re-hitting 2018's tournament every day, since VIS's own `version`
tracking on old matches shouldn't change. This keeps the job fast and
polite to FIVB's server.
"""

from datetime import datetime, timezone

from backfill import MATCH_FIELDS, discover_vnl_tournaments, load_matches, load_tournament
from db import get_connection

CURRENT_SEASON = str(datetime.now(timezone.utc).year)
PRIOR_SEASON = str(int(CURRENT_SEASON) - 1)


def main():
    conn = get_connection()
    tournaments = discover_vnl_tournaments()
    active = [t for t in tournaments if t["Season"] in (CURRENT_SEASON, PRIOR_SEASON)]

    print(f"[{datetime.now(timezone.utc).isoformat()}] Syncing {len(active)} active-season tournaments...")

    for t in active:
        load_tournament(conn, t)
        n_seen = load_matches(conn, int(t["No"]))
        print(f"  {t['Code']:12s} checked {n_seen} matches")

    conn.commit()
    conn.close()
    print(f"Sync complete: {len(active)} tournaments checked.")


if __name__ == "__main__":
    main()
