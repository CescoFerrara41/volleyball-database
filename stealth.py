"""
Stealth countermeasures for Playwright, to reduce the chance of being
served degraded/empty content by automation-detection checks.

Context: volleyballworld.com's per-player stat tables reliably showed
zero rows in every automated Playwright run, despite the exact same
selectors correctly finding real data during manual browser inspection.
Investigation ruled out a major third-party bot-management vendor (no
Cloudflare/Akamai/PerimeterX/DataDome/Imperva cookies or scripts found)
but found a concrete, real difference: the manual session reported
`navigator.webdriver: false` with a normal fingerprint, while
Playwright's default `chromium.launch()` sets `navigator.webdriver =
true` and has other automation-shaped fingerprint gaps (empty plugins
list, missing `window.chrome`, etc.) -- a well-documented, common way
sites gate content for automated traffic without needing a dedicated
anti-bot product.

This was NOT confirmed as the actual cause (the sandbox this project
is built in can't reach volleyballworld.com to test directly -- see
network_configuration). It's the strongest available lead, applied
here as a real fix attempt, not a proven fix.

Usage:
    from stealth import STEALTH_LAUNCH_ARGS, STEALTH_CONTEXT_KWARGS, apply_stealth_init_script

    browser = await p.chromium.launch(args=STEALTH_LAUNCH_ARGS)
    context = await browser.new_context(**STEALTH_CONTEXT_KWARGS)
    await apply_stealth_init_script(context)
    page = await context.new_page()
"""

# Disables the most well-known Chromium automation flag that some sites
# check for via the `Runtime.enable` CDP signal or related fingerprints.
STEALTH_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
]

# A realistic, current desktop Chrome fingerprint -- matches a real
# browser's UA/viewport/locale rather than Playwright's bare defaults
# (which report a generic "HeadlessChrome" UA unless overridden).
STEALTH_CONTEXT_KWARGS = {
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "viewport": {"width": 1920, "height": 1080},
    "locale": "en-US",
    "timezone_id": "America/New_York",
}

# Runs before any page script on every new document in the context.
# Patches the specific properties Playwright's default browser exposes
# differently from a real user browser:
#   - navigator.webdriver: Playwright sets this true; real Chrome never has it.
#   - navigator.plugins: empty in a bare automated browser; real Chrome
#     always has a handful (PDF viewer, etc.).
#   - navigator.languages: sometimes empty/inconsistent under automation.
#   - window.chrome: real Chrome always exposes this; headless sometimes doesn't.
#   - navigator.permissions.query: a classic fingerprint mismatch where
#     headless Chrome answers a 'notifications' permission query
#     differently than a real browser with no permission granted yet.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(() => ({ name: 'Chrome PDF Plugin' })),
});

Object.defineProperty(navigator, 'languages', {
  get: () => ['en-US', 'en'],
});

window.chrome = window.chrome || { runtime: {} };

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
  parameters.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : originalQuery(parameters)
);
"""


async def apply_stealth_init_script(context) -> None:
    """Register the stealth patch script on a browser context."""
    await context.add_init_script(_STEALTH_JS)
