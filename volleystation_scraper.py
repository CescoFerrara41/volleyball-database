"""
Scrapes per-player match statistics for VNL matches from
volleyballworld.com's own match pages.

An earlier version of this script drove a headless browser through a
separate embedded widget from VolleyStation (widgets.volleystation.com),
based on an iframe seen during initial inspection. That turned out to be
a red herring: **the iframe is transient**. On full page load,
volleyballworld.com swaps it out for the same data rendered natively,
same-origin, directly on the match page -- confirmed live on
2026-08-13 by loading a real 2024 match page and finding zero
`vbw-widget-iframe` elements but 85 real `<table>` elements containing
the exact same per-player stats, with real CSS classes to key off of.
This version scrapes those tables directly. No cross-origin navigation,
no widget IDs, no tab-clicking required -- every stat category and set
combination is already present in the DOM simultaneously (toggled via
a `hidden` class, which this script ignores since the data exists
either way).

Bridging two ID systems is still required:
  - VIS match number (`no`)          -- what backfill.py/sync.py store
  - volleyballworld.com schedule ID  -- e.g. '18953' in a match page URL

There's no direct lookup between these. Matches are linked by comparing
VIS's `no_in_tournament` against the tournament-wide "#N" match number
volleyballworld.com shows on its schedule listing (e.g. "Semi-Finals
#101") -- confirmed correct for a real match (VIS tournament 1439's
match #101 correctly linked to Poland vs France in Łódź, Poland, VNL
2024's real Finals venue). Team names are still cross-checked as a
safety gate before anything is written.

VERIFIED (via live browser inspection on 2026-08-13, against a real
2024 match page, not just the accessibility tree):
  - Season-scoped schedule listing lives at
    /volleyball-nations-league/{season}/schedule/, e.g. '2024/'. This
    IS filtered by season (confirmed via a different Finals venue --
    Łódź for 2024 vs Ningbo for 2026 -- not just a different page
    title).
  - That listing shows only ~9 matches at a time (a one-week window),
    with "previous week" / "next week" pagination. Getting a full
    season requires stepping through multiple weeks.
  - Match detail pages live at
    /volleyball-nations-league/{season}/schedule/{id}/ -- the season
    prefix is required in this path too (the previous version omitted
    it for detail pages, which was a bug).
  - Player-stat tables use the class pattern
    `vbw-match-player-statistic-table vbw-stats-{category} vbw-set-{set}`
    where category is one of scoring/attack/block/serve/reception/dig/set
    and set is 'all' or '1'-'5'. Two tables share each (category, set)
    combination -- one per team -- distinguished only by DOM order
    (first = Team A), not a team-specific class. This DOM-order
    assumption held for the one real match checked (Poland listed
    first in both the URL and the first table's roster) but wasn't
    checked against more than one match.
  - Each table has a real `<thead>` with `<th>` column headers, and
    player rows link to `/players/{id}` with the numeric ID confirmed
    to resolve correctly.

Usage:
    pip install playwright
    playwright install chromium
    python volleystation_scraper.py --tournament-no 1439 --season-path 2024/
"""

import argparse
import asyncio
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import psycopg2.extras
from playwright.async_api import async_playwright

from db import get_connection
from refresh_search_index import refresh as refresh_search_index
from stealth import STEALTH_CONTEXT_KWARGS, STEALTH_LAUNCH_ARGS, apply_stealth_init_script

VOLLEYBALLWORLD_BASE = "https://en.volleyballworld.com/volleyball/competitions/volleyball-nations-league"

# A single isolated request reliably works (confirmed: --debug-match
# extracted 1176 real rows from schedule ID 18953), but a run of ~28+
# sequential requests in one session reliably returned zero rows for
# everything, even after fixing the fingerprint issue that was the
# earlier suspect. That pattern points to something volume/session-
# based rather than a per-request check: rate limiting, or a heuristic
# that escalates the longer a session looks automated. Two mitigations
# for that specific shape of problem:
REQUEST_DELAY_RANGE = (4.0, 9.0)   # randomized delay instead of a fixed interval, so traffic doesn't look like a metronome
MATCHES_PER_CONTEXT = 8            # start a fresh browser context (new fingerprint, no accumulated session state) every N matches


async def polite_delay():
    await asyncio.sleep(random.uniform(*REQUEST_DELAY_RANGE))


MAX_SCHEDULE_WEEKS = 20      # safety cap when paging through a season's weekly schedule windows


class RotatingPage:
    """
    Hands out a page, refreshing to a brand-new browser context (new
    fingerprint, no accumulated cookies/session state) every N calls.

    This exists because the fix only covered the match-scraping loop
    the first time around -- get_schedule_matches() makes its own long
    sequence of ~11 sequential requests (one per week of the season)
    in a single unbroken session, and a real run showed it silently
    stops finding new matches partway through that sequence (28 of 104
    matches found, a similar ceiling to what match-scraping hit before
    ITS fix was applied). Routing every page request in the whole
    script -- schedule discovery included -- through one shared
    rotator means the refresh threshold is measured in total requests
    made, not just requests within one particular function.
    """

    def __init__(self, browser, requests_per_context: int = MATCHES_PER_CONTEXT):
        self.browser = browser
        self.requests_per_context = requests_per_context
        self.context = None
        self.page = None
        self.count = 0

    async def get_page(self):
        if self.page is None or self.count % self.requests_per_context == 0:
            if self.context is not None:
                await self.context.close()
                print(f"  (refreshed browser context after {self.count} requests)")
            self.context = await self.browser.new_context(**STEALTH_CONTEXT_KWARGS)
            await apply_stealth_init_script(self.context)
            self.page = await self.context.new_page()
        self.count += 1
        return self.page

    async def close(self):
        if self.context is not None:
            await self.context.close()


def normalize_team_name(name: str) -> str:
    """Loose normalization so 'Türkiye' and 'Turkiye' compare equal
    across sites that spell the same team slightly differently."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return n.strip().lower()


def _parse_match_links(hrefs: list[str]) -> list[dict]:
    """Shared regex parsing for schedule-listing hrefs -- used by both the
    tournament-wide default page and per-team schedule pages."""
    results = []
    for href in hrefs:
        m = re.search(r"/schedule/(\d+)\??.*?match=([\w-]+)-vs-([\w-]+)", href or "")
        if not m:
            continue
        schedule_id, team_a, team_b = m.groups()
        results.append({
            "schedule_id": int(schedule_id),
            "team_a": team_a.replace("-", " "),
            "team_b": team_b.replace("-", " "),
        })
    return results


async def _verify_gender(rotator: "RotatingPage", season_path: str, schedule_id: int, expected_gender: str) -> bool:
    """
    Visit a candidate match's own page and confirm its title's Men/Women
    component matches what's expected, before trusting it as an anchor.
    Returns True if verified OR if the title didn't parse (benefit of
    the doubt when gender isn't determinable at all) -- False only on a
    confirmed mismatch.
    """
    check_page = await rotator.get_page()
    check_url = f"{VOLLEYBALLWORLD_BASE}/{season_path}schedule/{schedule_id}/"
    await check_page.goto(check_url, wait_until="domcontentloaded")
    await dismiss_cookie_consent(check_page)
    check_title = await check_page.title()
    gender_match = re.search(r"\b(Men|Women)\b", check_title)
    if not gender_match:
        return True
    found_gender = "M" if gender_match.group(1) == "Men" else "W"
    return found_gender == expected_gender


async def _collect_candidate_offsets(
    rotator: "RotatingPage", season_path: str, page_matches: list[dict],
    vis_matches: list[dict], tournament_gender: str | None,
) -> list[tuple[int, dict, dict]]:
    """
    Check every (VIS match, page match) team-pair match, gender-verify
    each one, and return ALL resulting (offset, vis_match, page_match)
    tuples -- not just the first found.

    This exists because a SINGLE verified anchor was confirmed, on a
    real run (VNL 2021 women's), to still produce a wrong offset even
    after passing its own team-name AND gender check -- every match
    predicted from it came back completely wrong. Suspected cause:
    some tournaments (2021 was a COVID-era single-city "bubble" format,
    played in pools -- e.g. "Pool 5 - Preliminary Phase - Women #112")
    may not assign schedule IDs in the same simple sequential order as
    VIS's own match numbering, the way VNL 2024 (verified working)
    does. One passing anchor isn't proof the whole-tournament linear
    relationship actually holds; requiring two independent anchors to
    agree is a real, cheap consistency check that would have caught
    this exact failure before scraping 100+ wrong matches.
    """
    candidates = []
    for vm in vis_matches:
        vis_pair = {normalize_team_name(vm["team_a"]), normalize_team_name(vm["team_b"])}
        for pm in page_matches:
            pm_pair = {normalize_team_name(pm["team_a"]), normalize_team_name(pm["team_b"])}
            if vis_pair != pm_pair:
                continue
            if tournament_gender and not await _verify_gender(rotator, season_path, pm["schedule_id"], tournament_gender):
                continue
            offset = pm["schedule_id"] - (vm["no_in_tournament"] - 1)
            candidates.append((offset, vm, pm))
    return candidates


def _resolve_offset(candidates: list[tuple[int, dict, dict]]) -> tuple[int | None, list]:
    """
    Given collected (offset, vis_match, page_match) candidates, require
    at least 2 INDEPENDENT ones (different VIS matches) to agree on the
    same offset value before trusting it. Returns (offset, disagreeing
    candidates) -- offset is None if no value has 2+ independent
    agreement; disagreeing candidates are included so the caller can
    print a clear diagnostic instead of a bare failure.
    """
    from collections import defaultdict
    by_offset: dict[int, list] = defaultdict(list)
    for offset, vm, pm in candidates:
        # Independence: only count once per distinct VIS match number,
        # so the same anchor found twice (e.g. from two different team
        # pages) doesn't fake a second, independent confirmation.
        if not any(existing_vm["no_in_tournament"] == vm["no_in_tournament"] for existing_vm, _ in by_offset[offset]):
            by_offset[offset].append((vm, pm))

    agreed = [offset for offset, evidence in by_offset.items() if len(evidence) >= 2]
    if agreed:
        return agreed[0], []
    return None, list(by_offset.items())


async def _find_offset_via_tournament_page(
    rotator: "RotatingPage", season_path: str, vis_matches: list[dict], tournament_gender: str | None
) -> tuple[list[tuple[int, dict, dict]], str]:
    """
    Primary source: the tournament-wide schedule page's default
    (no-hash) view reliably shows the LATEST matches for a season --
    but confirmed live (2026-08-15) that this view ONLY EVER shows
    MEN'S matches, with no working gender toggle found. Returns every
    gender-verified candidate found (see _collect_candidate_offsets),
    not just one -- the caller decides whether enough independent
    candidates agree to trust an offset.
    """
    page = await rotator.get_page()
    url = f"{VOLLEYBALLWORLD_BASE}/{season_path}schedule/"
    await page.goto(url, wait_until="domcontentloaded")

    if season_path and "not found" in (await page.title()).lower():
        print(f"  Note: {url} returned Page Not Found -- this season likely hasn't been "
              f"archived under a year-prefixed URL yet. Falling back to the current-season URL.")
        season_path = ""
        url = f"{VOLLEYBALLWORLD_BASE}/{season_path}schedule/"
        await page.goto(url, wait_until="domcontentloaded")

    await dismiss_cookie_consent(page)
    try:
        await page.wait_for_selector('a[href*="/schedule/"][href*="match="]', timeout=15000)
    except Exception:
        return [], season_path
    await page.wait_for_timeout(1000)

    anchors = await page.query_selector_all('a[href*="/schedule/"][href*="match="]')
    hrefs = [await a.get_attribute("href") for a in anchors]
    default_view_matches = _parse_match_links(hrefs)

    candidates = await _collect_candidate_offsets(rotator, season_path, default_view_matches, vis_matches, tournament_gender)
    return candidates, season_path


async def _find_offset_via_team_pages(
    rotator: "RotatingPage", season_path: str, vis_matches: list[dict], tournament_gender: str,
    max_teams: int = 4,
) -> list[tuple[int, dict, dict]]:
    """
    Secondary source, tried when the tournament-wide page doesn't
    produce enough independent agreement on its own: per-TEAM schedule
    pages, which ARE genuinely gender-scoped
    ('.../teams/women/{team_id}/schedule/') -- confirmed live to show
    real women's matches, unlike the tournament-wide page. Team IDs are
    discovered from the standings page. Checks up to `max_teams` teams,
    accumulating candidates from all of them (not stopping at the
    first team with any match at all), since agreement needs two
    independent anchors.
    """
    gender_path = "women" if tournament_gender == "W" else "men"

    page = await rotator.get_page()
    standings_url = f"{VOLLEYBALLWORLD_BASE}/{season_path}standings/{gender_path}/"
    await page.goto(standings_url, wait_until="domcontentloaded")
    await dismiss_cookie_consent(page)
    try:
        await page.wait_for_selector(f'a[href*="/teams/{gender_path}/"][href*="/schedule/"]', timeout=15000)
    except Exception:
        return []
    await page.wait_for_timeout(800)

    team_links = await page.query_selector_all(f'a[href*="/teams/{gender_path}/"][href*="/schedule/"]')
    team_ids: list[str] = []
    for a in team_links:
        href = await a.get_attribute("href")
        m = re.search(rf"/teams/{gender_path}/(\d+)/schedule", href or "")
        if m and m.group(1) not in team_ids:
            team_ids.append(m.group(1))

    all_candidates = []
    for team_id in team_ids[:max_teams]:
        page = await rotator.get_page()
        team_url = f"{VOLLEYBALLWORLD_BASE}/{season_path}teams/{gender_path}/{team_id}/schedule/"
        await page.goto(team_url, wait_until="domcontentloaded")
        await dismiss_cookie_consent(page)
        try:
            await page.wait_for_selector('a[href*="/schedule/"][href*="match="]', timeout=10000)
        except Exception:
            continue
        await page.wait_for_timeout(800)

        anchors = await page.query_selector_all('a[href*="/schedule/"][href*="match="]')
        hrefs = [await a.get_attribute("href") for a in anchors]
        team_view_matches = _parse_match_links(hrefs)
        if not team_view_matches:
            continue

        candidates = await _collect_candidate_offsets(
            rotator, season_path, team_view_matches, vis_matches, tournament_gender
        )
        all_candidates.extend(candidates)

    return all_candidates


async def find_schedule_id_offset(
    rotator: "RotatingPage", season_path: str, vis_matches: list[dict], tournament_gender: str | None
) -> tuple[int | None, str]:
    """
    Determine the constant offset between VIS's `no_in_tournament` and
    volleyballworld.com's schedule ID for this tournament, then every
    match's schedule ID is just simple arithmetic -- no pagination
    needed at all.

    This replaces an earlier week-by-week pagination approach entirely,
    which turned out to rest on a broken assumption. Confirmed live
    (2026-08-14): the site's '#fromDate=' URL hash is IGNORED on a
    fresh page load. What DOES work, confirmed with two independent
    real matches for VNL 2024 men's: schedule IDs are perfectly
    sequential with VIS's own match order.

    IMPORTANT CAVEAT, confirmed by a real failure: that linear
    relationship does not necessarily hold for every tournament. VNL
    2021 women's (a COVID-era single-city "bubble" format, played in
    pools) produced a single, gender-verified, team-name-verified
    anchor that still predicted completely wrong matches for every
    other match in the tournament. So this now requires at least TWO
    INDEPENDENT anchor matches to agree on the same offset value before
    trusting it at all -- see _resolve_offset. If fewer than two agree,
    this returns None rather than guessing, even if exactly one
    "looked" valid.

    Gender handling: the tournament-wide schedule page's default view
    ONLY EVER shows men's matches (confirmed live, no working gender
    toggle exists) -- reliable for men's tournaments, but for women's
    ones this falls back to per-team schedule pages
    ('.../teams/women/{team_id}/schedule/'), which are genuinely
    gender-scoped.

    On `season_path`: confirmed live (2026-08-15) that
    '.../2026/schedule/' returns "Page Not Found" for a season that
    hasn't been archived under a year-prefixed URL yet -- handled with
    an automatic fallback to the year-less URL.
    """
    candidates, season_path = await _find_offset_via_tournament_page(rotator, season_path, vis_matches, tournament_gender)

    offset, disagreements = _resolve_offset(candidates)
    if offset is not None:
        return offset, season_path

    if tournament_gender:
        print(f"  No two independent anchors agreed via the tournament-wide page "
              f"({len(candidates)} candidate(s) found) -- trying per-team schedule "
              f"pages for gender={tournament_gender!r}.")
        team_candidates = await _find_offset_via_team_pages(rotator, season_path, vis_matches, tournament_gender)
        offset, disagreements = _resolve_offset(candidates + team_candidates)
        if offset is not None:
            return offset, season_path

    if disagreements:
        print(f"  Found candidate offset(s) but none had two independent matches "
              f"agreeing -- not trusting any of them:")
        for off, evidence in disagreements[:5]:
            vm, pm = evidence[0]
            print(f"    offset {off}: from {vm['team_a']} vs {vm['team_b']} "
                  f"(#{vm['no_in_tournament']}) -> schedule_id {pm['schedule_id']}")

    return None, season_path

async def dismiss_cookie_consent(page):
    """
    Dismiss the OneTrust cookie-consent banner if present. Confirmed via
    a live browser session that this site uses OneTrust with the
    standard element IDs below, and that a session with the consent
    cookie already set has the banner removed from the DOM entirely
    (not just hidden). A fresh Playwright session starts with no
    cookies, so it very likely sees this banner on first load --
    strongly suspected to be why an earlier run found the right page
    but zero stat tables: many sites don't finish rendering below-the-
    fold widgets until consent is resolved. Safe to call on every page
    load; it's a no-op if the banner isn't there.
    """
    try:
        accept_btn = page.locator("#onetrust-accept-btn-handler")
        if await accept_btn.count():
            await accept_btn.first.click(timeout=5000)
            await page.wait_for_timeout(500)
    except Exception:
        pass  # banner not present, or already dismissed -- fine either way


async def scrape_match_player_stats(
    page, schedule_id: str, season_path: str, debug: bool = False, return_teams: bool = False
):
    """
    Load a match's volleyballworld.com detail page directly and parse
    every player-stat table already present in the DOM -- no widget
    navigation, no tab clicking. Reads each table's own header row to
    build a {header: value} dict per player, since column meaning
    genuinely differs by category (confirmed: 'dig' has no Attempts
    column, unlike 'attack'/'serve').

    Note on the URL hash: the site's real current tabs are '#teamstats'
    and '#boxscore' (confirmed live 2026-08-14) -- an earlier version of
    this used '#advancedteamstats', which isn't a real tab on the
    current site. In manual browser testing both hashes returned the
    same 84 tables regardless, so this probably wasn't the cause of the
    zero-rows problem, but '#boxscore' is the actually-correct hash and
    used here going forward regardless.

    If `debug=True`, saves a screenshot and prints detailed diagnostics
    instead of failing silently -- meant for a single one-off run to
    see what's actually happening, not routine use (see --debug-match).

    If `return_teams=True`, returns (rows, team_names, gender) instead
    of just rows -- team_names is a 2-tuple and gender is 'M'/'W',
    both parsed from the page's own title (confirmed format: "Team
    A-Team B Men VNL 2024 DD.MM.YYYY"), or None/None if the title
    didn't parse. This is the safety check for
    find_schedule_id_offset()'s computed IDs: confirms a given
    schedule_id actually landed on the expected match -- not just the
    right teams, but the right GENDER too. Confirmed necessary, not
    theoretical: men's and women's VNL share enough country names
    (and sometimes near-identical Finals matchups, e.g. Poland/Slovenia
    contesting a bronze medal in both) that a team-pair match alone
    produced a real false-positive cross-gender anchor on a live run.
    """
    url = f"{VOLLEYBALLWORLD_BASE}/{season_path}schedule/{schedule_id}/#boxscore"
    await page.goto(url, wait_until="domcontentloaded")
    await dismiss_cookie_consent(page)

    selector_found = True
    try:
        await page.wait_for_selector("table.vbw-match-player-statistic-table", timeout=20000)
    except Exception:
        selector_found = False

    title = await page.title()
    actual_teams = None
    actual_gender = None
    title_match = re.match(r"^(.+?)-(.+?)\s+(Men|Women)\s+VNL", title)
    if title_match:
        actual_teams = (title_match.group(1).strip(), title_match.group(2).strip())
        actual_gender = "M" if title_match.group(3) == "Men" else "W"

    if debug:
        title = await page.title()
        body_text_sample = (await page.locator("body").inner_text())[:500]
        table_count = await page.locator("table.vbw-match-player-statistic-table").count()
        print("\n--- DEBUG: scrape_match_player_stats diagnostics ---")
        print(f"  URL: {url}")
        print(f"  Page title: {title!r}")
        print(f"  wait_for_selector succeeded: {selector_found}")
        print(f"  table.vbw-match-player-statistic-table count: {table_count}")
        print(f"  First 500 chars of visible body text:\n{body_text_sample!r}")
        screenshot_path = f"debug_{schedule_id}.png"
        await page.screenshot(path=screenshot_path, full_page=False)
        print(f"  Screenshot saved to: {screenshot_path}")
        print("--- end diagnostics ---\n")

    if not selector_found:
        return ([], actual_teams, actual_gender) if return_teams else []  # stats genuinely not published for this match yet (or something blocked rendering -- see debug output)

    tables = page.locator("table.vbw-match-player-statistic-table")
    table_count = await tables.count()

    # Group tables by (category, set) -- confirmed exactly two tables share
    # each combination, one per team, distinguished only by DOM order.
    combo_order: list[str] = []
    combo_first_seen: dict[str, int] = {}
    rows_out = []

    for i in range(table_count):
        table = tables.nth(i)
        class_attr = await table.get_attribute("class") or ""
        cat_match = re.search(r"vbw-stats-(\w+)", class_attr)
        set_match = re.search(r"vbw-set-(\w+)", class_attr)
        if not cat_match or not set_match:
            continue
        category, set_id = cat_match.group(1), set_match.group(1)
        combo_key = f"{category}:{set_id}"

        team_side = "A" if combo_key not in combo_first_seen else "B"
        combo_first_seen.setdefault(combo_key, i)

        header_cells = await table.locator("thead th").all_inner_texts()
        headers = [h.strip() for h in header_cells if h.strip()]

        body_rows = table.locator("tbody tr")
        row_count = await body_rows.count()
        for r in range(row_count):
            row = body_rows.nth(r)
            player_link = row.locator('a[href*="/players/"]')
            if not await player_link.count():
                continue
            player_name = (await player_link.first.inner_text()).strip()
            player_href = await player_link.first.get_attribute("href")
            pid_match = re.search(r"/players/(\d+)", player_href or "")
            player_vw_id = int(pid_match.group(1)) if pid_match else None

            cell_texts = [c.strip() for c in await row.locator("td").all_inner_texts()]
            raw_values = dict(zip(headers, cell_texts)) if headers else {"cells": cell_texts}

            rows_out.append({
                "team_side": team_side,
                "stat_category": category,
                "set": set_id,
                "player_name": player_name,
                "player_vw_id": player_vw_id,
                "raw_values": raw_values,
            })

    return (rows_out, actual_teams, actual_gender) if return_teams else rows_out


def store_player_stats(conn, match_no: int, player_rows: list[dict]):
    now = datetime.now(timezone.utc).isoformat()
    with conn.cursor() as cur:
        for r in player_rows:
            cur.execute(
                """
                INSERT INTO player_match_stats (
                    match_no, player_vw_id, player_name, team_side,
                    stat_category, set_id, raw_values, scraped_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (match_no, player_name, team_side, stat_category, set_id) DO UPDATE SET
                    raw_values=EXCLUDED.raw_values,
                    scraped_at=EXCLUDED.scraped_at
                """,
                (
                    match_no,
                    r["player_vw_id"],
                    r["player_name"],
                    r["team_side"],
                    r["stat_category"],
                    # Each category is scraped once per set scope ('all', '1'-'5');
                    # set_id has to be part of the conflict target, or every scope
                    # collapses onto the same row and whichever is written last
                    # (previously silently) wins -- see db.py's migration note.
                    r["set"],
                    psycopg2.extras.Json(r["raw_values"]),
                    now,
                ),
            )


async def run(tournament_no: int, season_path: str = "", only_match_numbers: set[int] | None = None):
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT gender FROM tournaments WHERE no = %s", (tournament_no,))
        gender_row = cur.fetchone()
        tournament_gender = gender_row[0] if gender_row else None

        cur.execute(
            "SELECT no, no_in_tournament, team_a_name, team_b_name, date_local FROM matches "
            "WHERE tournament_no = %s ORDER BY no_in_tournament",
            (tournament_no,),
        )
        vis_matches = [
            {"no": row[0], "no_in_tournament": row[1], "team_a": row[2], "team_b": row[3], "date_local": row[4]}
            for row in cur.fetchall()
            if row[1] is not None
        ]

    print(f"Loaded {len(vis_matches)} VIS matches for tournament {tournament_no} (gender={tournament_gender!r}).")

    # The offset lookup below (find_schedule_id_offset) needs the FULL
    # vis_matches list regardless of --matches -- it anchors against
    # whichever real matches it can find, which may not be the ones
    # you're targeting. Filtering happens after the offset is known,
    # right before the scraping loop, so re-running for a handful of
    # missing matches still gets a correctly-computed offset.
    if only_match_numbers is not None:
        matches_to_scrape = [vm for vm in vis_matches if vm["no_in_tournament"] in only_match_numbers]
        found_numbers = {vm["no_in_tournament"] for vm in matches_to_scrape}
        missing_requested = only_match_numbers - found_numbers
        if missing_requested:
            print(f"Note: requested match number(s) not found in this tournament's VIS data: "
                  f"{sorted(missing_requested)}")
        print(f"Targeting {len(matches_to_scrape)} of {len(vis_matches)} matches: "
              f"{sorted(found_numbers)}")
    else:
        matches_to_scrape = vis_matches

    async with async_playwright() as p:
        # Stealth measures: see stealth.py's docstring for why. Not a
        # confirmed fix (couldn't be tested against the live site from
        # this sandbox), but a real, verified difference in fingerprint
        # -- default Playwright reports navigator.webdriver=true and a
        # "HeadlessChrome" user agent; this patches both.
        browser = await p.chromium.launch(args=STEALTH_LAUNCH_ARGS)

        # One rotator shared across the offset lookup AND match scraping,
        # so the context-refresh threshold is measured across the whole
        # run's total requests -- see RotatingPage's docstring.
        rotator = RotatingPage(browser)

        offset, season_path = await find_schedule_id_offset(
            rotator, season_path, vis_matches, tournament_gender
        )
        if offset is None:
            print("Could not determine the VIS-to-schedule-ID offset (no matching "
                  "team pair found between VIS's last few matches and the site's "
                  "default schedule view). Nothing to scrape -- stopping.")
            await browser.close()
            conn.close()
            return
        print(f"Schedule ID offset found: {offset} (schedule_id = offset + no_in_tournament - 1, "
              f"season_path={season_path!r})")

        matched, skipped = 0, 0
        for i, vis_match in enumerate(matches_to_scrape):
            schedule_id = offset + (vis_match["no_in_tournament"] - 1)
            label = f"{vis_match['team_a']} vs {vis_match['team_b']} (#{vis_match['no_in_tournament']})"
            try:
                page = await rotator.get_page()
                player_rows, actual_teams, actual_gender = await scrape_match_player_stats(
                    page, str(schedule_id), season_path, return_teams=True
                )
                await polite_delay()

                # Safety check: confirm the computed schedule_id actually landed on
                # the expected match before trusting/storing anything from it --
                # protects against any gap or off-by-one in the ID sequence, AND
                # against the wrong gender (confirmed necessary on a real run --
                # see find_schedule_id_offset's docstring).
                if actual_teams is not None:
                    expected = {normalize_team_name(vis_match["team_a"]), normalize_team_name(vis_match["team_b"])}
                    found = {normalize_team_name(t) for t in actual_teams}
                    if expected and found and expected != found:
                        print(f"  {label}: schedule_id {schedule_id} shows different teams "
                              f"({actual_teams}) -- offset may not hold here, skipping.")
                        skipped += 1
                        continue
                if actual_gender is not None and tournament_gender is not None and actual_gender != tournament_gender:
                    print(f"  {label}: schedule_id {schedule_id} shows {actual_gender}'s match, "
                          f"expected {tournament_gender} -- offset may not hold here, skipping.")
                    skipped += 1
                    continue

                if not player_rows:
                    print(f"  {label}: no player stats found (may not be published for this match).")
                    skipped += 1
                    continue

                store_player_stats(conn, vis_match["no"], player_rows)
                conn.commit()
                matched += 1
                print(f"  {label}: {len(player_rows)} player-stat rows saved.")
            except Exception as e:
                conn.rollback()
                print(f"  {label}: failed ({type(e).__name__}: {str(e)[:150]}), skipping.")
                skipped += 1

        await browser.close()

    conn.close()

    if matched:
        # player_search_index is a materialized snapshot (see db.py), not a
        # live query -- new/changed data doesn't reach the site otherwise.
        refresh_search_index()
    print(f"\nDone: {matched} matches scraped, {skipped} skipped.")


async def debug_single_match(schedule_id: str, season_path: str):
    """
    Run diagnostics against a single match page and exit -- no VIS
    lookup, no database writes, no team-pair matching. Meant for
    narrowing down exactly what's happening on one page (e.g. schedule
    ID 18953, a match independently confirmed via manual browser
    inspection to have 84 real stat tables) rather than running the
    full multi-hour tournament loop just to get one data point.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=STEALTH_LAUNCH_ARGS)
        context = await browser.new_context(**STEALTH_CONTEXT_KWARGS)
        await apply_stealth_init_script(context)
        page = await context.new_page()

        fingerprint = await page.evaluate(
            "() => ({webdriver: navigator.webdriver, ua: navigator.userAgent, plugins: navigator.plugins.length})"
        )
        print(f"Stealth fingerprint check: {fingerprint}")

        rows = await scrape_match_player_stats(page, schedule_id, season_path, debug=True)
        print(f"Result: {len(rows)} player-stat rows extracted.")

        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tournament-no", type=int, help="VIS tournament number, e.g. 1439")
    parser.add_argument("--season-path", default="", help="e.g. '2024/' for a past season; blank for the current one")
    parser.add_argument(
        "--matches", metavar="N,N,...",
        help="Comma-separated VIS match numbers (no_in_tournament, e.g. from verify_data.py's "
             "output) to scrape instead of the whole tournament -- e.g. --matches 2,5,12. "
             "The offset is still computed from the full tournament's data for accuracy; "
             "only the actual scraping is limited to these matches.",
    )
    parser.add_argument(
        "--debug-match", metavar="SCHEDULE_ID",
        help="Run diagnostics against a single volleyballworld.com schedule ID and exit "
             "(e.g. --debug-match 18953 --season-path 2024/). Saves a screenshot and "
             "prints page title, selector status, and table count -- use this first "
             "before running a full tournament backfill.",
    )
    args = parser.parse_args()

    if args.debug_match:
        asyncio.run(debug_single_match(args.debug_match, args.season_path))
    elif args.tournament_no:
        match_numbers = None
        if args.matches:
            try:
                match_numbers = {int(n.strip()) for n in args.matches.split(",") if n.strip()}
            except ValueError:
                parser.error(f"--matches must be comma-separated integers, got: {args.matches!r}")
        asyncio.run(run(args.tournament_no, args.season_path, match_numbers))
    else:
        parser.error("Provide either --tournament-no (full run) or --debug-match SCHEDULE_ID (diagnostic).")