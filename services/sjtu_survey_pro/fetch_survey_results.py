#!/usr/bin/env python3
"""
Fetch latest survey results from wj.sjtu.edu.cn API.
Uses Playwright with Chrome profile for JAccount authentication.

Usage:
    python3 fetch_survey_results.py [--output sjtu_survey_data.json] [--page 1]

This fetches the response data from the survey, saves to JSON,
and then the feedback_server and email_feedback can consume it.
"""

import json
import os
import sys
import time
import argparse
from playwright.sync_api import sync_playwright

# 问卷 ID 与登录态配置全部来自环境变量，仓库内不保留真实 ID 与本地个人路径。
# 部署时在 app.yaml 里设置 survey_platforms.scale.survey_id（→ WJX_SURVEY_ID）；
# 本脚本保留 SURVEY_ID 作为历史别名（优先读已声明的 WJX_SURVEY_ID）。
SURVEY_ID = os.environ.get("WJX_SURVEY_ID") or os.environ.get("SURVEY_ID", "__YOUR_SURVEY_ID__")
API_BASE = os.environ.get("WJX_SURVEY_API_BASE", "https://wj.sjtu.edu.cn/api/v1/public/result")
_WJX_ROOT = API_BASE.split("/api/")[0] if "/api/" in API_BASE else "https://wj.sjtu.edu.cn"
FETCH_URL = f"{_WJX_ROOT}/api/survey/{SURVEY_ID}/results"
STAT_URL = f"{_WJX_ROOT}/api/survey/{SURVEY_ID}/stat"
CHROME_USER_DATA = os.environ.get("CHROME_USER_DATA", "")

DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "sjtu_survey_data_page1.json",
)


def fetch_with_logged_in_browser() -> dict:
    """
    Launch headless Chrome with existing profile, try API fetch.
    If not logged in, fall back to visible browser for manual login.
    Returns the JSON response.
    """
    with sync_playwright() as pw:
        # Try headless first
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=CHROME_USER_DATA,
            headless=True,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-extensions", "--no-first-run",
                  "--no-default-browser-check", "--disable-sync",
                  "--disable-background-networking"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Navigate to the survey's results page to establish auth
        print("[*] Checking survey API access...")
        try:
            page.goto(FETCH_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception:
            pass

        body = page.evaluate("() => document.body.innerText || ''")
        has_data = False

        # Try to parse JSON response
        json_text = page.evaluate("() => document.body.textContent || ''")

        try:
            data = json.loads(json_text)
            if data.get("success") or data.get("data"):
                has_data = True
                print("[✓] API data fetched successfully (headless)")
                ctx.close()
                return data
        except json.JSONDecodeError:
            pass

        # Check if we need to login
        body_lower = body.lower()
        if "登录" in body_lower or "jaccount" in body_lower or "login" in body_lower:
            print("[!] Not logged in. Need manual login...")
            ctx.close()

            # Open visible browser for login
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=CHROME_USER_DATA,
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Go to survey page first to trigger JAccount redirect
            page.goto(FETCH_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            print("  ╔════════════════════════════════════════════════╗")
            print("  ║  ⏳ 请在浏览器中登录 JAccount                ║")
            print("  ║  登录后脚本自动获取数据                      ║")
            print("  ╚════════════════════════════════════════════════╝")

            # Wait for login, up to 120 seconds
            for i in range(240):
                time.sleep(0.5)
                try:
                    body = page.evaluate("() => document.body.innerText || ''")
                    json_text = page.evaluate("() => document.body.textContent || ''")
                    try:
                        data = json.loads(json_text)
                        if data.get("success") or data.get("data"):
                            has_data = True
                            break
                    except json.JSONDecodeError:
                        pass
                    if "登录" not in body:
                        # Maybe we're on the results page
                        break
                except Exception:
                    pass
                if i % 40 == 0:
                    print(f"     waiting... ({i//2}s)")

            if not has_data:
                # Try one more time after login
                page.wait_for_timeout(3000)
                json_text = page.evaluate("() => document.body.textContent || ''")
                try:
                    data = json.loads(json_text)
                    has_data = data.get("success") or data.get("data")
                except json.JSONDecodeError:
                    pass

            if not has_data:
                print("[✗] Could not fetch survey data.")
                ctx.close()
                return None

            print("[✓] Logged in and data fetched!")

        ctx.close()

        if has_data:
            return data

        return None


def fetch_survey_results(page=1, output_path=None) -> dict:
    """
    Main entry point: fetch survey results and save to file.
    Returns the fetched data.
    """
    data = fetch_with_logged_in_browser()
    if not data:
        print("[✗] Failed to fetch survey data")
        return None

    # Save to output file
    out_path = output_path or DEFAULT_OUTPUT
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Count rows
    rows = data.get("data", {}).get("rows", [])
    print(f"[✓] Saved {len(rows)} response(s) to {out_path}")

    return data


def main():
    parser = argparse.ArgumentParser(
        description="Fetch survey results from wj.sjtu.edu.cn"
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--page", type=int, default=1, help="Page number (default: 1)"
    )
    args = parser.parse_args()

    print("=" * 60)
    print("SJTU Survey Results Fetcher")
    print(f"Survey: {SURVEY_ID}")
    print(f"Output: {args.output}")
    print("=" * 60)

    data = fetch_survey_results(args.page, args.output)

    if data:
        rows = data.get("data", {}).get("rows", [])
        print(f"\n✅ Success! {len(rows)} response(s) fetched.")
        return 0
    else:
        print(f"\n❌ Failed to fetch data.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
