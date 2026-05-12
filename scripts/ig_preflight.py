#!/usr/bin/env python3
"""
Pre-flight check — 본 작업 전에 반드시 1회 실행.

목적:
  인스타가 세션 리미트로 차단했는지 확인. 막혀있으면 본 작업 진행 금지.

동작:
  1) 시스템 Chrome + stealth 로 브라우저 켜기
  2) /p/{POST_ID}/ 또는 인자 URL 로 navigate
  3) viewport screenshot 한 장 찍어서 output/_preflight_{ts}.png 저장
  4) 페이지 상태 자동 진단 (login redirect, "Try again later", 댓글 visible 여부)
  5) 결과 출력 → 사용자가 PNG 직접 보고 판단

Usage:
  /path/to/venv/bin/python preflight.py [post_url]

Default URL: https://www.instagram.com/  (메인 페이지로 sanity check)
"""
import argparse
import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _browser import get_context, safe_close  # noqa: E402

PROJECT = Path.cwd()
USER_DATA_DIR = str(PROJECT / "chrome_session")
DEFAULT_URL = "https://www.instagram.com/"
VIEWPORT = {"width": 1280, "height": 750}


async def is_logged_in(ctx):
    cookies = await ctx.cookies("https://www.instagram.com")
    return any(c["name"] == "sessionid" for c in cookies)


async def diagnose(page):
    """페이지 자동 진단."""
    findings = []
    url = page.url
    if "/accounts/login" in url:
        findings.append(f"REDIRECTED TO LOGIN: {url}")
    if "/challenge/" in url:
        findings.append(f"CHALLENGE PAGE: {url}")
    try:
        body = await page.inner_text("body", timeout=2000)
    except Exception:
        body = ""
    blockers = [
        ("Try again later", "RATE LIMIT / TRY AGAIN LATER"),
        ("잠시 후 다시 시도", "RATE LIMIT (KO): 잠시 후 다시 시도"),
        ("Please wait a few minutes", "RATE LIMIT: Please wait a few minutes"),
        ("We restrict certain activity", "ACTIVITY RESTRICTED"),
        ("Suspicious Login", "SUSPICIOUS LOGIN CHALLENGE"),
    ]
    for needle, label in blockers:
        if needle in body:
            findings.append(f"BLOCK SIGNAL: {label}")
    # 정상 신호: time element 다수 (댓글 있는 포스트면)
    n_times = await page.evaluate("() => document.querySelectorAll('time[datetime]').length")
    findings.append(f"time elements: {n_times}")
    # 헤더 username 체크
    has_username_input = await page.locator('input[name="username"]').first.is_visible(timeout=300)
    findings.append(f"login form visible: {has_username_input}")
    return findings


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", default=DEFAULT_URL)
    ap.add_argument("--cdp", default=None, help="CDP URL (run.py 가 띄운 Chrome 에 어태치)")
    args = ap.parse_args()
    url = args.url

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = PROJECT / "output" / f"_preflight_{ts}.png"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[preflight] target: {url}")
    print(f"[preflight] screenshot will save to: {out_path}")

    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as p:
        ctx, owns_ctx = await get_context(
            p, args.cdp, USER_DATA_DIR,
            channel="chrome", headless=False,
            viewport=VIEWPORT, locale="ko-KR",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if not await is_logged_in(ctx):
            print("[preflight] 로그인 안됨. 직접 로그인. 5분 대기.")
            try:
                await page.goto("https://www.instagram.com/accounts/login/")
            except Exception:
                pass
            for i in range(150):
                await asyncio.sleep(2)
                if await is_logged_in(ctx):
                    print(f"[preflight] 로그인 OK ({(i+1)*2}s)")
                    break
            else:
                print("[preflight] 로그인 timeout")
                await safe_close(ctx, owns_ctx)
                return 1

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[preflight] navigate FAILED: {e}")
            try:
                await page.screenshot(path=str(out_path))
            except Exception:
                pass
            await safe_close(ctx, owns_ctx)
            return 2

        await asyncio.sleep(4)
        await page.screenshot(path=str(out_path))
        print(f"\n[preflight] saved: {out_path}")

        findings = await diagnose(page)
        print("\n=== DIAGNOSIS ===")
        for f in findings:
            print(f"  - {f}")

        # 정지 신호 있으면 비정상 종료 코드
        bad = any(s.startswith(("REDIRECTED", "RATE LIMIT", "ACTIVITY", "BLOCK", "SUSPICIOUS", "CHALLENGE"))
                  for s in findings)
        if bad:
            print("\n⚠️  세션 막힘 감지. 본 작업 진행 금지. 1~2시간 대기 후 재시도.")
            await safe_close(ctx, owns_ctx)
            return 3

        print("\n✓ 정상. 본 작업 진행 가능. (스크린샷 직접 확인 권장)")
        await safe_close(ctx, owns_ctx)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
