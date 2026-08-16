"""
Wipes all data from the database while keeping the schema intact --
for starting a clean full historical backfill from scratch.

Deletes in FK-safe order (children before parents): the stat tables
reference matches, which references tournaments.

Usage:
    python clear_data.py            # asks for confirmation first
    python clear_data.py --yes      # skips the confirmation prompt
"""

import argparse

from db import get_connection

TABLES_IN_DELETE_ORDER = [
    "player_match_stats",
    "team_match_stats",
    "matches",
    "tournaments",
]


def main(skip_confirm: bool):
    conn = get_connection()

    with conn.cursor() as cur:
        counts_before = {}
        for table in TABLES_IN_DELETE_ORDER:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            counts_before[table] = cur.fetchone()[0]

    print("Current row counts:")
    for table, count in counts_before.items():
        print(f"  {table}: {count}")

    if not skip_confirm:
        answer = input("\nDelete ALL of the above? This cannot be undone. Type 'yes' to confirm: ")
        if answer.strip().lower() != "yes":
            print("Aborted -- nothing was deleted.")
            conn.close()
            return

    with conn.cursor() as cur:
        for table in TABLES_IN_DELETE_ORDER:
            cur.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    print("\nAll tables cleared. Schema is untouched -- ready for a fresh backfill.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    main(args.yes)
