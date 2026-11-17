"""Entry point: login check -> profile boost -> job applications."""
import argparse
import sys

from playwright.sync_api import sync_playwright

from auth import ensure_login, get_page, is_logged_in, launch_context
from job_applier import run_job_applier
from logger import ActivityLogger, load_config
from profile_updater import run_profile_update


def main() -> int:
    parser = argparse.ArgumentParser(description="Naukri daily automation agent")
    parser.add_argument("--profile-only", action="store_true", help="only update profile/resume")
    parser.add_argument("--jobs-only", action="store_true", help="only scan and apply to jobs")
    parser.add_argument("--login", action="store_true", help="just log in and save the session")
    args = parser.parse_args()

    cfg = load_config()
    log = ActivityLogger(cfg["paths"]["activity_log"])
    log.log("run", "start")

    try:
        with sync_playwright() as pw:
            context = launch_context(pw, cfg)
            try:
                page = get_page(context)

                # Scheduled runs have no terminal: never block waiting for manual login.
                if not sys.stdin.isatty() and not is_logged_in(page):
                    log.log("login", "error", details="session expired; run `python main.py --login` manually")
                    return 2

                if not ensure_login(page, log):
                    return 2
                if args.login:
                    return 0

                if not args.jobs_only:
                    run_profile_update(page, cfg, log)
                if not args.profile_only:
                    run_job_applier(page, cfg, log)
            finally:
                context.close()
    except KeyboardInterrupt:
        log.log("run", "error", details="interrupted by user")
        return 130
    except Exception as e:
        log.log("run", "error", details=str(e)[:300])
        return 1

    log.log("run", "finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())