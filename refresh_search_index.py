"""
Refreshes the player_search_index materialized view (see db.py).

player_search_index is a static snapshot, not a live query -- see db.py's
comment on why -- so new players, new bios, and updated match counts
don't show up on the live site until this runs. volleystation_scraper.py
and player_bios_scraper.py both call this automatically when they finish,
so running it by hand is only needed after a partial/interrupted run or
if you've changed the data some other way.

REFRESH ... CONCURRENTLY (not a plain REFRESH) keeps the live site
readable while this runs -- a plain REFRESH takes a lock that blocks
reads for its duration. Requires the unique index db.py already creates
on the view, and can't run inside a transaction block, hence autocommit.

Usage:
    python refresh_search_index.py
"""

from db import get_connection


def refresh():
    conn = get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY player_search_index")
    finally:
        conn.close()


if __name__ == "__main__":
    refresh()
    print("player_search_index refreshed.")
