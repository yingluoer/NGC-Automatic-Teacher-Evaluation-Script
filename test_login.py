
"""只测登录链：authserver -> ehall -> 教务系统。不碰评教。用法：python test_login.py"""

import pathlib
import sys

from playwright.sync_api import sync_playwright

import pj
from captcha import CaptchaSolver

ROOT = pathlib.Path(__file__).resolve().parent

def main():
    cfg = pj.load_cfg(str(ROOT / "config.json"))
    headless = "--headed" not in sys.argv
    solver = CaptchaSolver(cfg)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / (cfg["user_data_dir"] + "_test")),
            headless=headless,
            slow_mo=60,
            no_viewport=True,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)
        work = pj.ensure_login(page, ctx, cfg, solver)
        print("LOGIN_RESULT:", work is not None)
        if work is not None:
            print("FINAL_URL:", work.url)
            rows = pj.get_rows(work)
            print("ROWS:", len(rows))
            for r in rows[:5]:
                print("  ", pj.row_label(r), "tjzt=", r.get("tjzt"))
        ctx.close()
        return 0 if work is not None else 1

if __name__ == "__main__":
    sys.exit(main())
