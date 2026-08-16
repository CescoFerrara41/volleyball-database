"""
Thin client for 365scores.com's public JSON API.

This is a real, undocumented-but-public API that powers their own website
and mobile app -- confirmed live on 2026-08-13. There's an existing
open-source project (LanusStats, on GitHub) already using the same
endpoint family for football, which is where the query-parameter
pattern below (appTypeId/langId/timezoneName/userCountryId) came from;
the specific volleyball competition/game IDs and response shapes here
were independently confirmed against real VNL data.

VERIFIED endpoints:
  - /web/games/results/?competitions={id}   -- recent results (see note below)
  - /web/game/?gameId={id}                  -- full match detail + roster ("members")
  - /web/game/stats/?games={id}             -- team-level match stats

NOT resolved: bulk historical-season access. Four different parameter
guesses (seasonNum, startDate/endDate, a larger num, a LastGameId
cursor) were all silently ignored by /web/games/results/ -- it appears
to always return the same rolling ~100 most recent games for a
competition, regardless of season/date parameters. This client is
therefore scoped to the current season only; see scores365_scraper.py.
"""

import requests

BASE_URL = "https://webws.365scores.com/web"

# Competition IDs, confirmed via the site's own navigation.
COMPETITION_IDS = {
    "M": 5569,   # Volleyball Nations League (men's)
    "W": 6271,   # Volleyball Nations League (W)
}

# Required boilerplate query params -- appTypeId=5 identifies the web
# client; the rest just needs to be present with plausible values.
COMMON_PARAMS = {
    "appTypeId": 5,
    "langId": 1,
    "timezoneName": "UTC",
    "userCountryId": 1,
}

HEADERS = {"User-Agent": "vnl-stats-portfolio-project/0.1 (personal project)"}


def get_current_season_results(gender: str) -> list[dict]:
    """
    Fetch the current season's results for men's or women's VNL.

    Returns whatever 365scores currently has -- confirmed to be a
    rolling window of the ~100 most recent games for the competition,
    not filterable to a past season (see module docstring).
    """
    params = {**COMMON_PARAMS, "competitions": COMPETITION_IDS[gender], "showOdds": "false"}
    resp = requests.get(f"{BASE_URL}/games/results/", params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("games", [])


def get_game_detail(game_id: int) -> dict:
    """Full match detail, including the 'members' roster list."""
    params = {**COMMON_PARAMS, "gameId": game_id}
    resp = requests.get(f"{BASE_URL}/game/", params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("game", {})


def get_game_team_stats(game_id: int) -> list[dict]:
    """Team-level stats (attack, blocks, serve points, reception % etc.)."""
    params = {**COMMON_PARAMS, "games": game_id}
    resp = requests.get(f"{BASE_URL}/game/stats/", params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("statistics", [])
