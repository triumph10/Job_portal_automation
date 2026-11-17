"""Browser launch (persistent context) and Naukri login/session handling."""
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Playwright
from playwright.sync_api import TimeoutError as PWTimeout

HOME_URL = "https://www.naukri.com/mnjuser/homepage"
LOGIN_URL = "https://www.naukri.com/nlogin/login"

# Any one of these visible => logged in (several fallbacks, Naukri changes its DOM often)
LOGGED_IN_SELECTORS = [
    "div.nI-gNb-drawer__icon",
    "a[href*='/mnjuser/profile']",
    "div.view-profile-wrapper",
    "div.nI-gNb-info",
    "img.nI-gNb-icon-img",
]


def launch_context(pw: Playwright, config: dict) -> BrowserContext:
    """Launch Chromium with a persistent profile so cookies/session survive between runs."""
    b = config["browser"]
    user_data = Path(config["paths"]["user_data_dir"]).resolve()
    user_data.mkdir(parents=True, exist_ok=True)

    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(user_data),
        headless=b.get("headless", False),
        slow_mo=b.get("slow_mo_ms", 0),
        viewport=b.get("viewport", {"width": 1366, "height": 768}),
        locale=b.get("locale", "en-IN"),
        timezone_id=b.get("timezone", "Asia/Calcutta"),
        user_agent=b.get("user_agent") or None,
        extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        ignore_default_args=["--enable-automation"],
    )
    context.set_default_timeout(20000)
    context.set_default_navigation_timeout(45000)
    # Hide the most common automation flag
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    )
    return context


def get_page(context: BrowserContext) -> Page:
    """Reuse the first open tab (persistent contexts start with one) or open a new one."""
    return context.pages[0] if context.pages else context.new_page()


def is_logged_in(page: Page) -> bool:
    """Open the Naukri homepage and decide whether the session is valid."""
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
    except PWTimeout:
        return False
    except Exception:
        return False

    url = page.url.lower()
    if "nlogin" in url or "/login" in url:
        return False

    for sel in LOGGED_IN_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=2500):
                return True
        except Exception:
            continue

    # Fallback: logged-in pages live under /mnjuser/
    return "/mnjuser/" in url


def ensure_login(page: Page, activity_log, max_attempts: int = 3) -> bool:
    """Return True once logged in. Pauses for manual login (OTP/CAPTCHA) if needed."""
    if is_logged_in(page):
        activity_log.log("login", "ok", details="existing session reused")
        return True

    for attempt in range(1, max_attempts + 1):
        activity_log.log("login", "manual_required", details=f"attempt {attempt}/{max_attempts}")
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except Exception:
            pass

        print("\n" + "=" * 60)
        print("You are NOT logged in to Naukri.")
        print("1. Log in manually in the browser window (solve OTP/CAPTCHA).")
        print("2. Wait until you can see your Naukri homepage.")
        input("3. Press Enter here to save the session and continue... ")
        print("=" * 60 + "\n")

        if is_logged_in(page):
            activity_log.log("login", "ok", details="manual login saved to ./user_data")
            return True
        print("Login not detected, please try again.")

    activity_log.log("login", "failed", details="could not verify login")
    return False