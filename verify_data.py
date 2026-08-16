"""
Checks the database for gaps after backfilling and scraping: missing
matches, matches with no player stats, and matches with only one
team's stats. Read-only -- never writes or deletes anything.

Usage:
    python verify_data.py                    # every tournament in the DB
    python verify_data.py --tournament-no 1439   # just one tournament
    python verify_data.py --season 2024           # just one season (both genders)
"""

import argparse

from db import get_connection


def check_no_in_tournament_gaps(cur, tournament_no: int, tournament_label: str) -> list[int]:
    """
    VIS match numbers should run 1..N with no gaps. A gap here means
    backfill.py itself missed a match, before the scraper is even
    involved -- worth knowing separately from missing player stats.
    """
    cur.execute(
        "SELECT no_in_tournament FROM matches WHERE tournament_no = %s ORDER BY no_in_tournament",
        (tournament_no,),
    )
    numbers = [row[0] for row in cur.fetchall() if row[0] is not None]
    if not numbers:
        return []
    expected = set(range(1, max(numbers) + 1))
    missing = sorted(expected - set(numbers))
    return missing


def check_missing_player_stats(cur, tournament_no: int) -> list[dict]:
    """Matches with zero player_match_stats rows at all."""
    cur.execute(
        """
        SELECT m.no, m.no_in_tournament, m.team_a_name, m.team_b_name
        FROM matches m
        LEFT JOIN player_match_stats pms ON pms.match_no = m.no
        WHERE m.tournament_no = %s AND pms.id IS NULL
        ORDER BY m.no_in_tournament
        """,
        (tournament_no,),
    )
    return [
        {"no_in_tournament": row[1], "team_a": row[2], "team_b": row[3]}
        for row in cur.fetchall()
    ]


def check_one_sided_stats(cur, tournament_no: int) -> list[dict]:
    """Matches with stats for only one team side, not both -- a partial scrape."""
    cur.execute(
        """
        SELECT m.no_in_tournament, m.team_a_name, m.team_b_name,
               COUNT(DISTINCT pms.team_side) AS sides_present
        FROM matches m
        JOIN player_match_stats pms ON pms.match_no = m.no
        WHERE m.tournament_no = %s
        GROUP BY m.no, m.no_in_tournament, m.team_a_name, m.team_b_name
        HAVING COUNT(DISTINCT pms.team_side) < 2
        ORDER BY m.no_in_tournament
        """,
        (tournament_no,),
    )
    return [
        {"no_in_tournament": row[0], "team_a": row[1], "team_b": row[2], "sides_present": row[3]}
        for row in cur.fetchall()
    ]


def verify_tournament(cur, no: int, code: str, name: str) -> bool:
    """Runs all checks for one tournament and prints a report. Returns True if clean."""
    label = f"{code} ({name})"
    print(f"\n{label}")
    print("-" * len(label))

    cur.execute("SELECT COUNT(*) FROM matches WHERE tournament_no = %s", (no,))
    total_matches = cur.fetchone()[0]
    print(f"  Total matches in DB: {total_matches}")

    gaps = check_no_in_tournament_gaps(cur, no, label)
    if gaps:
        print(f"  ⚠ Missing match numbers (not even in VIS data): {gaps}")
    else:
        print(f"  ✓ No gaps in match numbering")

    missing_stats = check_missing_player_stats(cur, no)
    if missing_stats:
        print(f"  ⚠ {len(missing_stats)} match(es) with NO player stats at all:")
        for m in missing_stats[:10]:
            print(f"      #{m['no_in_tournament']}: {m['team_a']} vs {m['team_b']}")
        if len(missing_stats) > 10:
            print(f"      ... and {len(missing_stats) - 10} more")
    else:
        print(f"  ✓ Every match has player stats")

    one_sided = check_one_sided_stats(cur, no)
    if one_sided:
        print(f"  ⚠ {len(one_sided)} match(es) with only ONE team's stats:")
        for m in one_sided[:10]:
            print(f"      #{m['no_in_tournament']}: {m['team_a']} vs {m['team_b']} "
                  f"(only {m['sides_present']} side present)")
    else:
        print(f"  ✓ Every match with stats has both teams represented")

    return not gaps and not missing_stats and not one_sided


def main(tournament_no: int | None, season: str | None):
    conn = get_connection()
    with conn.cursor() as cur:
        if tournament_no:
            cur.execute("SELECT no, code, name FROM tournaments WHERE no = %s", (tournament_no,))
        elif season:
            cur.execute("SELECT no, code, name FROM tournaments WHERE season = %s ORDER BY gender", (season,))
        else:
            cur.execute("SELECT no, code, name FROM tournaments ORDER BY season, gender")
        tournaments = cur.fetchall()

        if not tournaments:
            print("No matching tournaments found.")
            conn.close()
            return

        clean_count = 0
        for no, code, name in tournaments:
            if verify_tournament(cur, no, code, name):
                clean_count += 1

    conn.close()
    print(f"\n{'=' * 40}")
    print(f"{clean_count}/{len(tournaments)} tournaments fully clean, no issues found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tournament-no", type=int, default=None)
    parser.add_argument("--season", type=str, default=None)
    args = parser.parse_args()
    main(args.tournament_no, args.season)