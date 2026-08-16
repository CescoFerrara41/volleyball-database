"""
Lists every tournament currently in the database -- run this after
backfill.py to get the --tournament-no values needed for running
volleystation_scraper.py one season (and gender) at a time.

Usage:
    python list_tournaments.py
"""

from db import get_connection


def main():
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT no, code, name, season, gender FROM tournaments "
            "ORDER BY season, gender"
        )
        rows = cur.fetchall()
    conn.close()

    if not rows:
        print("No tournaments found -- run backfill.py first.")
        return

    print(f"{'no':>6}  {'code':<12} {'season':<8} gender  name")
    print("-" * 60)
    for no, code, name, season, gender in rows:
        print(f"{no:>6}  {code:<12} {season:<8} {gender:<6}  {name}")

    print(f"\n{len(rows)} tournaments total.")
    print("\nFor each one you want player stats for, run:")
    print("  python volleystation_scraper.py --tournament-no <no> --season-path <season>/")
    print("(omit --season-path only for the current season)")


if __name__ == "__main__":
    main()
