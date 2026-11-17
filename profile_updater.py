"""Daily profile booster: headline + summary micro-update, resume re-upload."""
import random
import time
from pathlib import Path

from playwright.sync_api import Locator, Page

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"

HEADLINE_EDIT_BTN = [
    "#lazyResumeHead span.edit.icon",
    "#lazyResumeHead .edit",
    "div.resumeHeadline span.edit",
]
HEADLINE_TEXTAREA = [
    "textarea#resumeHeadlineTxt",
    "textarea[name='resumeHeadline']",
    "textarea[placeholder*='headline' i]",
    "textarea:visible",
]
SUMMARY_EDIT_BTN = [
    "#lazyProfileSummary span.edit.icon",
    "#lazyProfileSummary .edit",
]
SUMMARY_TEXTAREA = [
    "textarea#profileSummaryTxt",
    "textarea[name='profileSummary']",
    "textarea[placeholder*='summary' i]",
    "textarea:visible",
]
SAVE_BTN = [
    "button:has-text('Save'):visible",
    "button[type='submit']:visible",
    "xpath=//button[normalize-space()='Save']",
]
RESUME_FILE_INPUT = ["input#attachCV", "input[type='file'][id*='attach' i]", "input[type='file']"]


def _first_visible(page: Page, selectors: list, timeout: int = 4000) -> Locator | None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


def _pause(cfg: dict):
    d = cfg["jobs"]["delay_seconds"]
    time.sleep(random.uniform(d["min"], d["max"]))


def _open_profile(page: Page) -> bool:
    try:
        page.goto(PROFILE_URL, wait_until="domcontentloaded")
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass  # networkidle often never fires on Naukri; continue anyway
    return "/mnjuser/profile" in page.url


def _scroll_through(page: Page):
    """Naukri lazy-loads profile sections, so scroll down to load them, then back up."""
    for _ in range(7):
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(500)
    page.mouse.wheel(0, -6000)
    page.wait_for_timeout(800)


def _dump_debug(page: Page, tag: str):
    """On failure, save a screenshot + list of edit-like elements so selectors can be fixed."""
    try:
        Path("debug").mkdir(exist_ok=True)
        page.screenshot(path=f"debug/{tag}.png", full_page=True)
        info = page.evaluate(
            """() => [...document.querySelectorAll('[class*=edit],[id*=Resume],[id*=Summary],[id*=lazy]')]
                .slice(0, 80).map(e => e.tagName + '#' + e.id + '.' + e.className + ' | '
                + (e.innerText || '').slice(0, 40).replace(/\\n/g, ' '))"""
        )
        Path(f"debug/{tag}.txt").write_text("\n".join(info), encoding="utf-8")
    except Exception:
        pass


def _open_editor(page: Page, title: str, edit_sels: list, box_sels: list) -> Locator | None:
    """Click the section's edit icon and return the textarea. Tries known selectors, then
    finds the icon next to every element whose text is the section title."""
    candidates = []
    for sel in edit_sels:
        candidates.append(page.locator(sel).first)
    heading = page.get_by_text(title, exact=True)
    try:
        for i in range(heading.count()):
            candidates.append(
                heading.nth(i).locator(
                    "xpath=ancestor::*[.//*[contains(@class,'edit')]][1]//*[contains(@class,'edit')]"
                ).first
            )
    except Exception:
        pass

    for loc in candidates:
        try:
            if loc.count() == 0:
                continue
            loc.scroll_into_view_if_needed(timeout=3000)
            loc.click(force=True, timeout=3000)
            box = _first_visible(page, box_sels, 3500)
            if box:
                return box
        except Exception:
            continue
    return None


def _toggle_field(page: Page, cfg: dict, log, name: str, title: str,
                  edit_sels: list, box_sels: list, variants: list) -> bool:
    if len(variants) < 2:
        log.log(f"{name}_update", "skipped", details=f"{name}_variants not configured")
        return False
    try:
        box = _open_editor(page, title, edit_sels, box_sels)
        if not box:
            log.log(f"{name}_update", "failed", details="edit button/textarea not found (see ./debug)")
            _dump_debug(page, name)
            return False
        _pause(cfg)

        current = (box.input_value() or "").strip()
        new_text = variants[1] if current == variants[0].strip() else variants[0]
        box.fill(new_text)
        _pause(cfg)

        save = _first_visible(page, SAVE_BTN, 4000)
        if not save:
            log.log(f"{name}_update", "failed", details="save button not found (see ./debug)")
            _dump_debug(page, name + "_save")
            return False
        save.click()
        page.wait_for_timeout(2500)
        log.log(f"{name}_update", "ok", details=f"{len(new_text)} chars, ends with '{new_text[-1]}'")
        return True
    except Exception as e:
        log.log(f"{name}_update", "error", details=str(e)[:200])
        _dump_debug(page, name + "_error")
        return False


def update_headline(page: Page, cfg: dict, log) -> bool:
    return _toggle_field(page, cfg, log, "headline", "Resume headline",
                         HEADLINE_EDIT_BTN, HEADLINE_TEXTAREA, cfg["profile"]["headline_variants"])


def update_summary(page: Page, cfg: dict, log) -> bool:
    variants = cfg["profile"].get("summary_variants") or []
    try:  # prefer AI-written variants that you approved via `python ai_helper.py approve`
        from ai_helper import approved_texts, pick_for_today

        approved = approved_texts(cfg, "summary")
        if approved:
            today = pick_for_today(approved)
            alt = today.rstrip(".") if today.endswith(".") else today + "."
            variants = [today, alt]
    except Exception as e:
        log.log("summary_update", "info", details=f"AI variants unavailable, using config: {str(e)[:100]}")
    return _toggle_field(page, cfg, log, "summary", "Profile summary",
                         SUMMARY_EDIT_BTN, SUMMARY_TEXTAREA, variants)


def upload_resume(page: Page, cfg: dict, log) -> bool:
    resume = Path(cfg["paths"]["resume"]).resolve()
    if not resume.exists():
        log.log("resume_upload", "error", details=f"file not found: {resume}")
        return False
    try:
        # File inputs are usually hidden, so attach without waiting for visibility.
        for sel in RESUME_FILE_INPUT:
            inp = page.locator(sel).first
            if inp.count() == 0:
                continue
            inp.set_input_files(str(resume))
            page.wait_for_timeout(5000)
            log.log("resume_upload", "ok", details=resume.name)
            return True
        log.log("resume_upload", "failed", details="file input not found")
        return False
    except Exception as e:
        log.log("resume_upload", "error", details=str(e)[:200])
        return False


def run_profile_update(page: Page, cfg: dict, log) -> dict:
    """Entry point used by main.py. Returns a dict of step -> bool."""
    result = {"headline": False, "summary": False, "resume": False}
    if not _open_profile(page):
        log.log("profile_update", "failed", details=f"could not open profile page (url={page.url})")
        return result

    _scroll_through(page)

    result["headline"] = update_headline(page, cfg, log)
    _pause(cfg)

    result["summary"] = update_summary(page, cfg, log)
    _pause(cfg)

    if cfg["profile"].get("update_resume", True):
        result["resume"] = upload_resume(page, cfg, log)

    log.log("profile_update", "done", details=str(result))
    return result