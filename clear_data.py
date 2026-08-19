"""
Wipes data from the database while keeping the schema intact -- for
starting a clean rebuild from scratch.

Deletes in FK-safe order (children before parents): the stat tables
reference matches, which references tournaments.

Usage:
    python clear_data.py               # wipes everything, asks for confirmation first
    python clear_data.py --yes         # skips the confirmation prompt
    python clear_data.py --stats-only  # wipes only player_match_stats/team_match_stats,
                                        # leaving tournaments/matches (and player bios) intact --
                                        # use this before a re-scrape that's only fixing stats,
                                        # since backfill.py's VIS-sourced data isn't what's wrong
"""

import argparse

from db import get_connection

TABLES_IN_DELETE_ORDER = [
    "player_match_stats",
    "team_match_stats",
    "matches",
    "tournaments",
]

STATS_ONLY_TABLES = [
    "player_match_stats",
    "team_match_stats",
]


def main(skip_confirm: bool, stats_only: bool):
    tables = STATS_ONLY_TABLES if stats_only else TABLES_IN_DELETE_ORDER
    conn = get_connection()

    with conn.cursor() as cur:
        counts_before = {}
        for table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts_before[table] = cur.fetchone()[0]

    print("Current row counts:")
    for table, count in counts_before.items():
        print(f"  {table}: {count}")
    if stats_only:
        print("(tournaments, matches, and players are untouched in --stats-only mode)")

    if not skip_confirm:
        answer = input("\nDelete ALL of the above? This cannot be undone. Type 'yes' to confirm: ")
        if answer.strip().lower() != "yes":
            print("Aborted -- nothing was deleted.")
            conn.close()
            return

    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    print(f"\n{'Stat tables' if stats_only else 'All tables'} cleared. Schema is untouched -- ready for a fresh "
          f"{'stats re-scrape (via volleystation_scraper.py)' if stats_only else 'backfill'}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument(
        "--stats-only", action="store_true",
        help="only clear player_match_stats/team_match_stats, keeping tournaments/matches/players",
    )
    args = parser.parse_args()
    main(args.yes, args.stats_only)
