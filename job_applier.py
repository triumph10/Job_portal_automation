"""Smart job scanner and auto-applier for Naukri (direct 'Apply' only)."""
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import BrowserContext, Locator, Page

CARD_LINKS = [
    "div.srp-jobtuple-wrapper a.title",
    "article.jobTuple a.title",
    "div.cust-job-tuple a.title",
    "a.title[href*='job-listings']",
]
TITLE_SEL = ["h1[class*='jd-header-title']", "h1.jd-header-title", "h1"]
COMPANY_SEL = ["a[class*='jd-header-comp-name']", "div[class*='comp-name'] a", "a[href*='-jobs-careers-']"]
APPLY_BTN = ["button#apply-button", "button[class*='apply-button']", "button:has-text('Apply')"]
EXTERNAL_BTN = ["button#company-site-button", "button:has-text('Apply on company site')",
                "a:has-text('Apply on company site')"]
ALREADY_APPLIED = ["#already-applied", "button:has-text('Already Applied')", "div:has-text('You have already applied')"]
CHATBOT_ROOT = ["div[class*='chatbot_DrawerContentWrapper']", "div[class*='chatbot']", "div.chatbot_Drawer"]
BOT_MSGS = ["div.botMsg span", "li.botItem span", "div[class*='botMsg']", "div[class*='botItem']"]
CHIPS = ["div.chatbot_Chip", "div[class*='Chip']", "label.ssrc__label", "div.ssrc__radio-btn-container label"]
TEXT_INPUT = ["div.textArea[contenteditable='true']", "div[contenteditable='true']",
              "input[type='text']", "textarea"]
SEND_BTN = ["div.sendMsg", "button:has-text('Save')", "button:has-text('Send')", "div[class*='send']"]
SUCCESS_TEXT = ["successfully applied", "applied successfully", "application has been submitted"]


def _first(page: Page, sels: list, timeout: int = 2500) -> Locator | None:
    for s in sels:
        try:
            loc = page.locator(s).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def _text(page: Page, sels: list, default: str = "Unknown") -> str:
    loc = _first(page, sels, 2000)
    try:
        return (loc.inner_text().strip() if loc else default) or default
    except Exception:
        return default


def _delay(cfg: dict):
    d = cfg["jobs"]["delay_seconds"]
    time.sleep(random.uniform(d["min"], d["max"]))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _search_url(keyword: str, location: str, exp: int) -> str:
    return (f"https://www.naukri.com/{_slug(keyword)}-jobs-in-{_slug(location)}"
            f"?k={quote(keyword)}&l={quote(location)}&experience={exp}")


class AppliedStore:
    """Remembers applied job URLs across runs so we never apply twice."""

    def __init__(self, path: str):
        self.path = Path(path)
        try:
            self.ids = set(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            self.ids = set()

    @staticmethod
    def key(url: str) -> str:
        return url.split("?")[0].rstrip("/")

    def has(self, url: str) -> bool:
        return self.key(url) in self.ids

    def add(self, url: str):
        self.ids.add(self.key(url))
        self.path.write_text(json.dumps(sorted(self.ids), indent=1), encoding="utf-8")


def _find_answer(question: str, qa: dict) -> str | None:
    q = question.lower()
    for key in sorted(qa, key=len, reverse=True):
        if key.lower() in q:
            return str(qa[key])
    return None


def _handle_chatbot(page: Page, cfg: dict, log, title: str, company: str) -> str:
    """Walk the questionnaire. Returns 'applied', 'skipped' or 'failed'."""
    qa = cfg.get("qa_answers", {})
    last_q = ""
    for _ in range(20):
        page.wait_for_timeout(2000)
        body = page.inner_text("body").lower()
        if any(t in body for t in SUCCESS_TEXT):
            return "applied"
        if not _first(page, CHATBOT_ROOT, 1500):
            return "applied"  # drawer closed => submitted

        msgs = []
        for s in BOT_MSGS:
            try:
                msgs = page.locator(s).all_inner_texts()
                if msgs:
                    break
            except Exception:
                continue
        question = (msgs[-1] if msgs else "").strip()
        if not question or question == last_q:
            continue
        last_q = question

        answer = _find_answer(question, qa)
        chips = []
        for s in CHIPS:
            try:
                found = page.locator(s).all()
                if found:
                    chips = found
                    break
            except Exception:
                continue

        if answer is None:
            print(f"\n[Unknown question] {title} @ {company}\n  Q: {question}")
            answer = input("  Your answer (or 's' to skip this job): ").strip()
            if answer.lower() == "s":
                log.log("apply", "skipped", title, company, f"unknown question: {question[:80]}")
                return "skipped"

        try:
            if chips:
                pick = next((c for c in chips if answer.lower() in c.inner_text().lower()), None)
                if pick is None:
                    print(f"\n[No matching option] Q: {question}")
                    typed = input("  Type exact option text (or 's' to skip): ").strip()
                    if typed.lower() == "s":
                        return "skipped"
                    pick = next((c for c in chips if typed.lower() in c.inner_text().lower()), None)
                if pick is None:
                    return "skipped"
                pick.click()
                btn = _first(page, SEND_BTN, 1500)
                if btn:
                    btn.click()
            else:
                box = _first(page, TEXT_INPUT, 3000)
                if not box:
                    continue
                box.click()
                box.type(answer, delay=random.randint(20, 60))
                btn = _first(page, SEND_BTN, 2000)
                if btn:
                    btn.click()
                else:
                    box.press("Enter")
            log.log("chatbot", "answered", title, company, f"Q: {question[:60]} | A: {answer}")
        except Exception as e:
            log.log("chatbot", "error", title, company, str(e)[:150])
            return "failed"
    return "failed"


def _apply_to_job(ctx: BrowserContext, url: str, cfg: dict, log, store: AppliedStore):
    """Returns (status, title, company)."""
    page = ctx.new_page()
    title = company = "Unknown"
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        title, company = _text(page, TITLE_SEL), _text(page, COMPANY_SEL)

        if _first(page, ALREADY_APPLIED, 1200):
            store.add(url)
            log.log("apply", "skipped", title, company, "already applied")
            return "skipped", title, company
        if cfg["jobs"].get("skip_external_redirects", True) and _first(page, EXTERNAL_BTN, 1200):
            log.log("apply", "skipped", title, company, "external company site")
            return "skipped", title, company

        btn = _first(page, APPLY_BTN, 4000)
        if not btn or "company site" in (btn.inner_text() or "").lower():
            log.log("apply", "skipped", title, company, "no direct apply button")
            return "skipped", title, company

        btn.scroll_into_view_if_needed()
        btn.click()
        status = _handle_chatbot(page, cfg, log, title, company)
        if status == "applied":
            store.add(url)
            log.log("apply", "applied", title, company, url)
        elif status == "failed":
            log.log("apply", "failed", title, company, "questionnaire did not complete")
        return status, title, company
    except Exception as e:
        log.log("apply", "error", title, company, str(e)[:200])
        return "failed", title, company
    finally:
        try:
            page.close()
        except Exception:
            pass


def _collect_links(page: Page, url: str, log) -> list:
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(1500)
    except Exception as e:
        log.log("job_search", "error", details=f"{url} -> {str(e)[:120]}")
        return []
    for sel in CARD_LINKS:
        try:
            hrefs = page.eval_on_selector_all(sel, "els => els.map(e => e.href)")
            hrefs = [h for h in hrefs if h]
            if hrefs:
                return list(dict.fromkeys(hrefs))
        except Exception:
            continue
    log.log("job_search", "failed", details=f"no job cards found at {url}")
    return []


def run_job_applier(page: Page, cfg: dict, log) -> dict:
    """Entry point used by main.py."""
    j = cfg["jobs"]
    limit = j["max_applies_per_run"]
    store = AppliedStore(cfg["paths"].get("applied_store", "./applied_jobs.json"))
    stats = {"applied": 0, "skipped": 0, "failed": 0}

    for kw in j["keywords"]:
        if stats["applied"] >= limit:
            break
        links = _collect_links(page, _search_url(kw, j["location"], j["experience_years"]), log)
        log.log("job_search", "ok", details=f"'{kw}' -> {len(links)} jobs found")

        for link in links:
            if stats["applied"] >= limit:
                break
            if store.has(link):
                continue
            status, _, _ = _apply_to_job(page.context, link, cfg, log, store)
            stats[status if status in stats else "failed"] += 1
            _delay(cfg)

    log.log("job_applier", "done", details=str(stats))
    return stats