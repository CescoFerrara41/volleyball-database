"""
One-time historical backfill: discover every VNL tournament (men's and
women's, all seasons) and load all of their matches.

Run this once to seed the database:

    python backfill.py
"""

from datetime import datetime, timezone

from db import get_connection
from vis_client import get_tournament_list, get_match_list

MATCH_FIELDS = (
    "No NoInTournament DateLocal TeamAName TeamBName "
    "MatchPointsA MatchPointsB PointsTeamASet1 PointsTeamBSet1"
)


def discover_vnl_tournaments() -> list[dict]:
    """
    Pull the full tournament list and keep only VNL entries.

    VNL codes follow a 'MVNL<year>' / 'WVNL<year>' pattern for the main
    season, with a 'F' suffix for Finals (e.g. 'MVNL24F'). We keep both —
    Finals matches are real VNL matches, just a separate VIS tournament.
    """
    all_tournaments = get_tournament_list(fields="No Code Name Season")
    return [t for t in all_tournaments if "VNL" in t["Code"]]


def load_tournament(conn, tournament: dict) -> None:
    gender = "W" if tournament["Code"].startswith("W") else "M"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO tournaments (no, code, name, season, gender)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (no) DO UPDATE SET
                code=EXCLUDED.code, name=EXCLUDED.name,
                season=EXCLUDED.season, gender=EXCLUDED.gender
            """,
            (int(tournament["No"]), tournament["Code"], tournament["Name"], tournament["Season"], gender),
        )


def load_matches(conn, tournament_no: int) -> int:
    matches = get_match_list(tournament_no, fields=MATCH_FIELDS)
    now = datetime.now(timezone.utc).isoformat()

    with conn.cursor() as cur:
        for m in matches:
            cur.execute(
                """
                INSERT INTO matches (
                    no, tournament_no, no_in_tournament, date_local,
                    team_a_name, team_b_name, match_points_a, match_points_b,
                    points_team_a_set1, points_team_b_set1, version, last_synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (no) DO UPDATE SET
                    no_in_tournament=EXCLUDED.no_in_tournament,
                    date_local=EXCLUDED.date_local,
                    team_a_name=EXCLUDED.team_a_name,
                    team_b_name=EXCLUDED.team_b_name,
                    match_points_a=EXCLUDED.match_points_a,
                    match_points_b=EXCLUDED.match_points_b,
                    points_team_a_set1=EXCLUDED.points_team_a_set1,
                    points_team_b_set1=EXCLUDED.points_team_b_set1,
                    version=EXCLUDED.version,
                    last_synced_at=EXCLUDED.last_synced_at
                WHERE EXCLUDED.version != matches.version
                """,
                (
                    int(m["No"]),
                    tournament_no,
                    int(m["NoInTournament"]) if m.get("NoInTournament") else None,
                    m.get("DateLocal") or None,
                    m.get("TeamAName") or None,
                    m.get("TeamBName") or None,
                    int(m["MatchPointsA"]) if m.get("MatchPointsA") else None,
                    int(m["MatchPointsB"]) if m.get("MatchPointsB") else None,
                    int(m["PointsTeamASet1"]) if m.get("PointsTeamASet1") else None,
                    int(m["PointsTeamBSet1"]) if m.get("PointsTeamBSet1") else None,
                    int(m["Version"]),
                    now,
                ),
            )
    return len(matches)


def main():
    conn = get_connection()
    tournaments = discover_vnl_tournaments()
    print(f"Found {len(tournaments)} VNL tournaments (all seasons, men's + women's, finals included).")

    total_matches = 0
    for t in tournaments:
        load_tournament(conn, t)
        n = load_matches(conn, int(t["No"]))
        total_matches += n
        print(f"  {t['Code']:12s} {t['Name']:50s} -> {n} matches")

    conn.commit()
    conn.close()
    print(f"\nBackfill complete: {len(tournaments)} tournaments, {total_matches} matches.")


if __name__ == "__main__":
    main()
