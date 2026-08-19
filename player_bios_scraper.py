"""
Scrapes each player's full name and bio (position, nationality, age,
birth date, height) from their own volleyballworld.com profile page.

Why this is a separate script: match box-score tables (see
volleystation_scraper.py) only ever show a bare last name -- e.g. "Ran",
"Nishida" -- never a first name. Confirmed live that the individual
player page at .../players/{id} has both: a full name (h1/`.vbw-player-name`,
e.g. "Yuji Nishida") and a "Player Bio" section with real semantic markup
(`.vbw-player-bio-col` > `.vbw-player-bio-head` + `.vbw-player-bio-text`).

This visits each DISTINCT player_vw_id exactly once, not once per match --
confirmed ~1500-2000 unique players total vs. ~260k player_match_stats
rows, so this is a much shorter run than the match-stats scrape. Already-
scraped players (present in the `players` table) are skipped by default,
so this is safe to re-run to pick up newly-discovered players after a
fresh match-stats scrape.

Usage:
    pip install playwright
    playwright install chromium
    python player_bios_scraper.py
    python player_bios_scraper.py --limit 50     # test run
    python player_bios_scraper.py --rescrape-all  # ignore existing rows
"""

import argparse
import asyncio
import random
import re
from datetime import datetime, timezone

from playwright.async_api import async_playwright

from db import get_connection
from refresh_search_index import refresh as refresh_search_index
from stealth import STEALTH_LAUNCH_ARGS
from volleystation_scraper import RotatingPage, dismiss_cookie_consent

VOLLEYBALLWORLD_BASE = "https://en.volleyballworld.com/volleyball/competitions/volleyball-nations-league"

REQUEST_DELAY_RANGE = (3.0, 6.0)   # shorter than the match scraper's -- a bio page is much lighter than a box score
PLAYERS_PER_CONTEXT = 15           # same rotation-for-politeness approach as volleystation_scraper.py


async def polite_delay():
    await asyncio.sleep(random.uniform(*REQUEST_DELAY_RANGE))


def parse_height_cm(text: str) -> int | None:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else None


def parse_birth_date(text: str) -> str | None:
    """'30/01/2000' (DD/MM/YYYY, as the site renders it) -> '2000-01-30'."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", text or "")
    if not m:
        return None
    day, month, year = m.groups()
    return f"{year}-{month}-{day}"


async def scrape_player_bio(page, player_vw_id: int) -> dict | None:
    url = f"{VOLLEYBALLWORLD_BASE}/players/{player_vw_id}"
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await dismiss_cookie_consent(page)

    try:
        await page.wait_for_selector(".vbw-player-bio-wrap", timeout=15000)
    except Exception:
        return None  # page didn't render the bio section -- player may not have a public profile

    # .text_content() (raw DOM text), not .inner_text() (CSS-rendered text)
    # -- confirmed live that .vbw-player-lastname has an uppercase
    # text-transform applied, so .inner_text() returned "NISHIDA" instead
    # of "Nishida", which silently broke the full_name.endswith(last_name)
    # check below (and would display shouting-case names in the UI).
    name_el = page.locator("span.vbw-player-name")
    if not await name_el.count():
        return None
    full_name = ((await name_el.first.text_content()) or "").strip()

    last_name_el = page.locator("div.vbw-player-lastname")
    last_name = ((await last_name_el.first.text_content()) or "").strip() if await last_name_el.count() else None

    first_name = None
    if last_name and full_name.endswith(last_name):
        first_name = full_name[: -len(last_name)].strip() or None

    # Each bio field is a labeled column; some (Position, Nationality) also
    # render an abbreviated `--mobile` duplicate alongside the full text --
    # confirmed live that Age/Birth date/Height have no such duplicate at
    # all, so ":not(.--mobile)" has to work for both shapes. Also confirmed
    # live (player 112366) that the site's own desktop text is sometimes an
    # unfilled "TBD" placeholder while the mobile abbreviation is correct
    # (e.g. "BR") -- a real site-side data gap, not a scrape bug -- so an
    # empty or "TBD" desktop value falls back to the mobile one rather than
    # storing the placeholder verbatim.
    bio: dict[str, str] = {}
    cols = page.locator(".vbw-player-bio-col:not(.--player-name)")
    for i in range(await cols.count()):
        col = cols.nth(i)
        head_el = col.locator(".vbw-player-bio-head")
        if not await head_el.count():
            continue
        head = ((await head_el.first.text_content()) or "").strip().lower()

        desktop_el = col.locator(".vbw-player-bio-text:not(.--mobile)")
        desktop_text = ((await desktop_el.first.text_content()) or "").strip() if await desktop_el.count() else ""

        if not desktop_text or desktop_text.upper() == "TBD":
            mobile_el = col.locator(".vbw-player-bio-text.--mobile")
            mobile_text = ((await mobile_el.first.text_content()) or "").strip() if await mobile_el.count() else ""
            bio[head] = mobile_text or desktop_text
        else:
            bio[head] = desktop_text

    return {
        "player_vw_id": player_vw_id,
        "full_name": full_name,
        "first_name": first_name,
        "last_name": last_name,
        "position": bio.get("position") or None,
        "nationality": bio.get("nationality") or None,
        "birth_date": parse_birth_date(bio.get("birth date")),
        "height_cm": parse_height_cm(bio.get("height")),
    }


def store_player_bio(conn, bio: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO players (
                player_vw_id, full_name, first_name, last_name,
                position, nationality, birth_date, height_cm, scraped_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (player_vw_id) DO UPDATE SET
                full_name=EXCLUDED.full_name,
                first_name=EXCLUDED.first_name,
                last_name=EXCLUDED.last_name,
                position=EXCLUDED.position,
                nationality=EXCLUDED.nationality,
                birth_date=EXCLUDED.birth_date,
                height_cm=EXCLUDED.height_cm,
                scraped_at=EXCLUDED.scraped_at
            """,
            (
                bio["player_vw_id"], bio["full_name"], bio["first_name"], bio["last_name"],
                bio["position"], bio["nationality"], bio["birth_date"], bio["height_cm"], now,
            ),
        )


async def run(limit: int | None, rescrape_all: bool):
    conn = get_connection()
    with conn.cursor() as cur:
        if rescrape_all:
            cur.execute(
                "SELECT DISTINCT player_vw_id FROM player_match_stats "
                "WHERE player_vw_id IS NOT NULL ORDER BY player_vw_id"
            )
        else:
            cur.execute(
                "SELECT DISTINCT pms.player_vw_id FROM player_match_stats pms "
                "LEFT JOIN players p ON p.player_vw_id = pms.player_vw_id "
                "WHERE pms.player_vw_id IS NOT NULL AND p.player_vw_id IS NULL "
                "ORDER BY pms.player_vw_id"
            )
        player_ids = [row[0] for row in cur.fetchall()]

    if limit:
        player_ids = player_ids[:limit]

    print(f"{len(player_ids)} player(s) to scrape"
          f"{' (already-scraped players skipped)' if not rescrape_all else ''}.")
    if not player_ids:
        conn.close()
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=STEALTH_LAUNCH_ARGS)
        rotator = RotatingPage(browser, requests_per_context=PLAYERS_PER_CONTEXT)

        done, failed = 0, 0
        for i, player_vw_id in enumerate(player_ids):
            try:
                page = await rotator.get_page()
                bio = await scrape_player_bio(page, player_vw_id)
                await polite_delay()

                if bio is None:
                    print(f"  [{i + 1}/{len(player_ids)}] {player_vw_id}: no bio page found, skipping.")
                    failed += 1
                    continue

                store_player_bio(conn, bio)
                conn.commit()
                done += 1
                print(f"  [{i + 1}/{len(player_ids)}] {player_vw_id}: {bio['full_name']!r} "
                      f"({bio['position']}, {bio['nationality']})")
            except Exception as e:
                conn.rollback()
                print(f"  [{i + 1}/{len(player_ids)}] {player_vw_id}: failed "
                      f"({type(e).__name__}: {str(e)[:150]}), skipping.")
                failed += 1

        await rotator.close()
        await browser.close()

    conn.close()

    if done:
        # player_search_index is a materialized snapshot (see db.py), not a
        # live query -- new/changed data doesn't reach the site otherwise.
        refresh_search_index()
    print(f"\nDone: {done} bios saved, {failed} skipped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Only scrape the first N players (for a test run)")
    parser.add_argument(
        "--rescrape-all", action="store_true",
        help="Re-scrape every player found in player_match_stats, including ones already in `players`",
    )
    args = parser.parse_args()
    asyncio.run(run(args.limit, args.rescrape_all))
