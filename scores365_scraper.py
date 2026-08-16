"""
Pulls current-season VNL match stats from 365scores.com: team-level
stats and rosters via clean JSON, and per-player box scores via a
headless browser (the box score isn't exposed as JSON -- see
scrape_box_score below).

Scoped to the CURRENT season only. 365scores' results API returns a
rolling window of recent games with no working way found to reach past
seasons (see scores365_client.py's docstring) -- this is meant to be
re-run each season going forward, not backfilled against history. For
historical seasons, matches stay on VIS-only data (backfill.py/sync.py).

Matches are linked to VIS by team pair, the same proven approach as
volleystation_scraper.py: 365scores' own team names are compared
against VIS's, normalized for accent/casing differences. A repeat
pairing (two teams meeting twice) is resolved by matching both sides'
occurrences in their own chronological order.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python scores365_scraper.py --tournament-no 1616 --gender M
"""

import argparse
import asyncio
import re
import unicodedata
from datetime import datetime, timezone

import psycopg2.extras
from playwright.async_api import async_playwright

from db import get_connection
from scores365_client import (
    COMPETITION_IDS,
    get_current_season_results,
    get_game_detail,
    get_game_team_stats,
)

REQUEST_DELAY_SECONDS = 1.0
STAT_GROUPS = ["Points", "Serve", "Reception", "Attack", "Blocks", "Set"]


def normalize_team_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.strip().lower()


def build_match_url(game: dict) -> str:
    """
    Construct a 365scores match page URL from a /web/games/results/
    entry. Confirmed pattern from a real captured URL:
    '.../match/volleyball-nations-league-5569/poland-usa-13310-13311-5569#id=4801738'
    -- away team's slug first, then home's, then home id, then away id,
    then competition id again, with the game id as a hash fragment.
    """
    comp_id = game["competitionId"]
    home = game["homeCompetitor"]
    away = game["awayCompetitor"]
    home_slug = home.get("nameForURL") or re.sub(r"\s+", "-", home["name"].lower())
    away_slug = away.get("nameForURL") or re.sub(r"\s+", "-", away["name"].lower())
    return (
        f"https://www.365scores.com/en-us/volleyball/match/volleyball-nations-league-{comp_id}/"
        f"{away_slug}-{home_slug}-{home['id']}-{away['id']}-{comp_id}#id={game['id']}"
    )


async def dismiss_cookie_consent(page):
    """Best-effort cookie-consent dismissal -- a safe no-op if no banner is present."""
    for selector in ["#onetrust-accept-btn-handler", "button:has-text('Accept')"]:
        try:
            btn = page.locator(selector)
            if await btn.count():
                await btn.first.click(timeout=3000)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def scrape_box_score(page, match_url: str, home_name: str, away_name: str) -> list[dict]:
    """
    Load a match page, open the Box Score tab, and extract per-player
    stats for both teams.

    Confirmed real structure (via live browser inspection, 2026-08-13):
      - No <table> elements -- a custom widget instead. Player name/
        position: `[class*="row_primary_cells_row_container"]` spans
        (first child of the container is a header, not a player).
      - Stats grid: `[class*="expandable_table_stats"]` -> children
        are rows (`box-scores-widget_table_row...`), each row has 6
        cells (one per STAT_GROUPS entry) with class containing
        "grouped_values". A cell's innerText is newline-separated
        sub-values (e.g. Attack -> "33\\n8\\n18\\n55%").
      - The header row (index 0 of the same grid) has the same 6-cell
        shape, where each cell's first line is the group name (e.g.
        "Attack") and the remaining lines are that group's sub-labels
        (e.g. "TOT","ERR","PTS","ATT%") -- used to build a
        {sub_label: value} dict per group instead of hardcoding
        column meaning.
      - Only one team's roster is shown at a time; a team-switcher
        control with the literal team name text (e.g. "USA") toggles
        which one is visible.
    """
    await page.goto(match_url, wait_until="domcontentloaded")
    await dismiss_cookie_consent(page)

    try:
        box_score_tab = page.locator("text=Box Score").first
        await box_score_tab.click(timeout=10000)
    except Exception:
        return []  # tab not present -- box score likely unavailable for this match

    await page.wait_for_timeout(1000)

    rows_out = []
    for team_side, team_name in [("A", home_name), ("B", away_name)]:
        try:
            team_btn = page.locator(f"text={team_name}").first
            if await team_btn.count():
                await team_btn.click(timeout=5000)
                await page.wait_for_timeout(700)
        except Exception:
            pass  # team switcher not found -- fall through and try to read whatever is showing

        try:
            grid = page.locator('[class*="expandable_table_stats"]').first
            if not await grid.count():
                continue

            header_row = grid.locator("> *").nth(0)
            header_cells_text = await header_row.locator('[class*="grouped_values"]').all_inner_texts()
            # First line of each header cell is the group name; the rest are sub-labels.
            group_sub_labels = []
            for cell_text in header_cells_text:
                lines = cell_text.split("\n")
                group_sub_labels.append(lines[1:] if len(lines) > 1 else [lines[0]] if lines else [])

            names_container = page.locator('[class*="row_primary_cells_container"]').first
            name_spans = names_container.locator('[class*="row_primary_cells_row_container"]')
            name_count = await name_spans.count()

            data_rows = grid.locator("> *")
            row_count = await data_rows.count()

            # Row 0 is the header; player rows follow in the same order as name_spans.
            for i in range(1, row_count):
                if i - 1 >= name_count:
                    break
                name_text = (await name_spans.nth(i - 1).inner_text()).strip()
                if "\n" in name_text:
                    player_name, position = name_text.split("\n", 1)
                else:
                    player_name, position = name_text, None

                cell_texts = await data_rows.nth(i).locator('[class*="grouped_values"]').all_inner_texts()
                raw_values = {}
                for group_name, sub_labels, cell_text in zip(STAT_GROUPS, group_sub_labels, cell_texts):
                    values = cell_text.split("\n") if cell_text else []
                    raw_values[group_name] = dict(zip(sub_labels, values)) if sub_labels else values

                rows_out.append({
                    "team_side": team_side,
                    "player_name": player_name.strip(),
                    "position": position,
                    "raw_values": raw_values,
                })
        except Exception:
            continue  # this team's roster failed to parse -- keep whatever the other team produced

    return rows_out


def store_team_stats(conn, match_no: int, game: dict, team_stats: list[dict]):
    now = datetime.now(timezone.utc).isoformat()
    by_side = {game["homeCompetitor"]["id"]: "A", game["awayCompetitor"]["id"]: "B"}
    grouped: dict[str, dict] = {}
    for row in team_stats:
        side = by_side.get(row.get("competitorId"))
        if not side:
            continue
        grouped.setdefault(side, {})[row["name"]] = row.get("value")

    with conn.cursor() as cur:
        for side, values in grouped.items():
            cur.execute(
                """
                INSERT INTO team_match_stats (match_no, team_side, raw_values, scraped_at, source)
                VALUES (%s, %s, %s, %s, '365scores')
                ON CONFLICT (match_no, team_side) DO UPDATE SET
                    raw_values=EXCLUDED.raw_values, scraped_at=EXCLUDED.scraped_at
                """,
                (match_no, side, psycopg2.extras.Json(values), now),
            )


def store_player_stats(conn, match_no: int, player_rows: list[dict]):
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        for r in player_rows:
            values = dict(r["raw_values"])
            if r.get("position"):
                values["Position"] = r["position"]
            cur.execute(
                """
                INSERT INTO player_match_stats (
                    match_no, player_vw_id, player_name, team_side,
                    stat_category, raw_values, scraped_at, source
                ) VALUES (%s, NULL, %s, %s, 'overall', %s, %s, '365scores')
                ON CONFLICT (match_no, player_name, team_side, stat_category) DO UPDATE SET
                    raw_values=EXCLUDED.raw_values, scraped_at=EXCLUDED.scraped_at, source=EXCLUDED.source
                """,
                (match_no, r["player_name"], r["team_side"], psycopg2.extras.Json(values), now),
            )


async def run(tournament_no: int, gender: str):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT no, team_a_name, team_b_name FROM matches WHERE tournament_no = %s",
            (tournament_no,),
        )
        vis_matches = [{"no": row[0], "team_a": row[1], "team_b": row[2]} for row in cur.fetchall()]

    print(f"Loaded {len(vis_matches)} VIS matches for tournament {tournament_no}.")

    results = get_current_season_results(gender)
    print(f"365scores returned {len(results)} current-season games "
          f"(competition {COMPETITION_IDS[gender]}).")

    # Index 365scores games by normalized team pair
    by_pair: dict[frozenset, list[dict]] = {}
    for g in results:
        pair = frozenset({
            normalize_team_name(g["homeCompetitor"]["name"]),
            normalize_team_name(g["awayCompetitor"]["name"]),
        })
        by_pair.setdefault(pair, []).append(g)

    vis_by_pair: dict[frozenset, list[dict]] = {}
    for vm in vis_matches:
        pair = frozenset({normalize_team_name(vm["team_a"]), normalize_team_name(vm["team_b"])})
        vis_by_pair.setdefault(pair, []).append(vm)

    matched, skipped = 0, 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        for pair_key, vis_group in vis_by_pair.items():
            candidates = by_pair.get(pair_key, [])
            if not candidates:
                skipped += len(vis_group)
                continue

            for vis_match, game in zip(vis_group, candidates):
                label = f"{vis_match['team_a']} vs {vis_match['team_b']}"
                try:
                    team_stats = get_game_team_stats(game["id"])
                    store_team_stats(conn, vis_match["no"], game, team_stats)

                    match_url = build_match_url(game)
                    player_rows = await scrape_box_score(
                        page, match_url, game["homeCompetitor"]["name"], game["awayCompetitor"]["name"]
                    )
                    if player_rows:
                        store_player_stats(conn, vis_match["no"], player_rows)

                    conn.commit()
                    matched += 1
                    print(f"  {label}: team stats + {len(player_rows)} player rows saved.")
                    await asyncio.sleep(REQUEST_DELAY_SECONDS)
                except Exception as e:
                    conn.rollback()
                    print(f"  {label}: failed ({type(e).__name__}: {str(e)[:150]}), skipping.")
                    skipped += 1

        await browser.close()

    conn.close()
    print(f"\nDone: {matched} matches processed, {skipped} skipped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tournament-no", type=int, required=True, help="VIS tournament number for the CURRENT season")
    parser.add_argument("--gender", choices=["M", "W"], required=True)
    args = parser.parse_args()
    asyncio.run(run(args.tournament_no, args.gender))
