#!/usr/bin/env python3
"""
방법 3 — 댓글마다 개별 캡처. 매칭 알고리즘 불필요.

전략:
  1) /p/{POST}/ 1회 navigate
  2) 모든 숨겨진 댓글 펼치기 + 답글 펼치기 (lazy loading 다 해소)
  3) 영상 정지, 마우스 (0,0)
  4) 각 댓글마다:
     a) row 찾기 (DOM 검색, 인스타 트래픽 0)
     b) scrollIntoView({block:'start'}) → 사이드바 맨 위로 강제 정렬
     c) 전체 화면 캡처 (시스템 시계 포함, _fullscreen.py)
     d) 저장 후 1.0~2.5초 jitter
  5) resume: 이미 PNG 있으면 skip

300개 규모 가정:
  - 100개마다 5분 idle (인스타 자동화 탐지 회피)
  - 모든 캡처 끝나면 브라우저 종료
"""
import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fullscreen import fullscreen_capture  # noqa: E402

PROJECT = Path.cwd()
USER_DATA_DIR = str(PROJECT / "chrome_session")
DEFAULT_PROGRESS = PROJECT / "output" / ".progress_dxqb.json"

VIEWPORT = {"width": 1280, "height": 750}
DELAY_AFTER_NAV = 5.0
JITTER_MIN = 1.0
JITTER_MAX = 2.5
IDLE_EVERY_N = 100
IDLE_DURATION = 300
MIN_CAPTURE_KB = 100


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-가-힣]+", "_", name)


async def is_logged_in(ctx):
    cookies = await ctx.cookies("https://www.instagram.com")
    return any(c["name"] == "sessionid" for c in cookies)


async def detect_stop(page):
    try:
        text = await page.inner_text("body", timeout=2000)
    except Exception:
        return None
    for n in ["Please wait a few minutes", "잠시 후 다시 시도",
              "We restrict certain activity", "Try again later"]:
        if n in text:
            return n
    if "/challenge/" in page.url:
        return f"challenge: {page.url}"
    return None


async def click_show_hidden(page):
    for _ in range(5):
        try:
            btn = page.get_by_role("button", name=re.compile(r"숨겨진 댓글|hidden", re.I))
            if await btn.first.is_visible(timeout=1500):
                await btn.first.click()
                await asyncio.sleep(2)
                continue
        except Exception:
            pass
        break


async def click_load_more_comments(page, hard_timeout=900, target_count=None):
    """lazy loading 강제 트리거 — 사람처럼 천천히, 페이지 자체 스크롤 절대 금지.

    - 컨테이너만 200~400px 점진적 스크롤 (max-jump 금지, mouse wheel 금지)
    - 매 라운드마다 window.scrollTo(0, 0) 강제 (페이지 자체는 항상 위)
    - 라운드 사이 2~4초 대기
    - "댓글 더 불러오기" 버튼 보이면 클릭
    - 답글 펼치기는 이 함수에서 안 함 (별도 단계에서)
    - target_count 도달 또는 stale 12회 → 종료
    """
    import time
    clicks = 0
    deadline = time.time() + hard_timeout
    stale_rounds = 0

    while time.time() < deadline and stale_rounds < 12:
        # 매 라운드 시작 시 페이지 자체 스크롤 0 강제
        await page.evaluate("window.scrollTo(0, 0)")

        n_times = await page.evaluate("() => document.querySelectorAll('time[datetime]').length")
        round_progress = False

        if target_count and n_times >= target_count:
            print(f"  [load] target {target_count} 도달 (현재 {n_times})")
            break

        # 1) "댓글 더 불러오기" 텍스트 버튼
        try:
            loc = page.get_by_role("button", name=re.compile(r"댓글 더 불러오기|Load more comments|View more comments", re.I))
            if await loc.first.is_visible(timeout=600):
                await loc.first.click(timeout=2000, force=True)
                clicks += 1
                round_progress = True
                await asyncio.sleep(random.uniform(2.0, 3.5))
                await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        # 2) SVG aria-label
        try:
            svg = page.locator('svg[aria-label*="댓글 더 불러오기"], svg[aria-label*="Load more"]')
            if await svg.first.is_visible(timeout=500):
                btn = svg.first.locator("xpath=ancestor::button[1]")
                await btn.first.click(timeout=2000, force=True)
                clicks += 1
                round_progress = True
                await asyncio.sleep(random.uniform(2.0, 3.5))
                await page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass

        # 3) 컨테이너만 점진적 스크롤 — 페이지 자체는 절대 안 건드림
        await page.evaluate("""(step) => {
            const all = document.querySelectorAll('div');
            for (const el of all) {
                const cs = getComputedStyle(el);
                if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight + 30 &&
                    el.querySelector('time[datetime]')) {
                    el.scrollTop = Math.min(el.scrollTop + step, el.scrollHeight);
                    return;
                }
            }
        }""", random.randint(200, 400))
        # 컨테이너 스크롤 후 페이지 강제 (혹시 같이 내려갔을 경우)
        await page.evaluate("window.scrollTo(0, 0)")
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # 4) mouse wheel 제거 — 컨테이너 max 도달 시 페이지로 전이됨

        new_n = await page.evaluate("() => document.querySelectorAll('time[datetime]').length")
        if new_n > n_times:
            round_progress = True

        if not round_progress:
            stale_rounds += 1
            await asyncio.sleep(random.uniform(2.5, 4.5))
        else:
            stale_rounds = 0
            print(f"  [load] time elements: {n_times} → {new_n}, total clicks: {clicks}")

    # 마지막에 페이지 강제 위로
    await page.evaluate("window.scrollTo(0, 0)")
    return clicks


async def click_view_replies(page, hard_timeout=120):
    pattern = re.compile(
        r"^(답글\s*\d+\s*개?\s*(모두\s*)?(더\s*)?보기"
        r"|—\s*답글\s*\d+\s*개?\s*보기"
        r"|답글\s+더\s+보기"
        r"|View\s+\d+\s*(more\s+)?repl(y|ies)"
        r"|View\s+repl(y|ies))$"
    )
    import time
    deadline = time.time() + hard_timeout
    seen = []
    clicks = 0
    while time.time() < deadline:
        loc = page.get_by_text(pattern, exact=True)
        try:
            n = await loc.count()
        except Exception:
            n = 0
        if n == 0:
            break
        clicked = False
        for i in range(min(n, 6)):
            try:
                el = loc.nth(i)
                if not await el.is_visible(timeout=500):
                    continue
                txt = (await el.inner_text(timeout=500)).strip()
                if seen.count(txt) >= 3:
                    continue
                seen.append(txt)
                await el.scroll_into_view_if_needed(timeout=2000)
                await el.click(timeout=3000, force=True)
                clicks += 1
                clicked = True
                await asyncio.sleep(random.uniform(0.6, 1.2))
            except Exception:
                continue
        if not clicked:
            break
    return clicks


async def scroll_target_to_top(page, username):
    """타겟 댓글 row 를 사이드바 맨 위로.

    페이지 자체 scrollY 는 항상 0 으로 강제 (좌측 영상 잘림 방지).
    댓글 컨테이너만 직접 scrollTop 조정.
    """
    return await page.evaluate(
        """(uname) => {
            const SKIP = new Set(['p','reel','explore','accounts','direct','stories']);

            // 댓글 스크롤 컨테이너 찾기
            let container = null;
            const all = document.querySelectorAll('div');
            for (const el of all) {
                const cs = getComputedStyle(el);
                if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight + 30 &&
                    el.querySelector('time[datetime]')) {
                    container = el;
                    break;
                }
            }

            // 타겟 row 찾기
            const times = document.querySelectorAll('time[datetime]');
            let foundRow = null;
            for (let i = 1; i < times.length; i++) {
                const t = times[i];
                let row = t;
                for (let j = 0; j < 18; j++) {
                    const par = row.parentElement;
                    if (!par) break;
                    if (par.querySelectorAll('time[datetime]').length === 1) row = par;
                    else break;
                }
                const links = row.querySelectorAll('a[href^="/"]');
                let author = null;
                for (const a of links) {
                    const m = (a.getAttribute('href') || '').match(/^\\/([^/?]+)\\/?$/);
                    if (m && !SKIP.has(m[1])) {
                        author = m[1];
                        break;
                    }
                }
                if (author === uname) {
                    foundRow = row;
                    break;
                }
            }

            if (!foundRow) return false;

            // 1) 페이지 자체는 위로 강제
            window.scrollTo(0, 0);

            // 2) 컨테이너 안에서만 row 가 맨 위에 오도록
            if (container) {
                const cRect = container.getBoundingClientRect();
                const rRect = foundRow.getBoundingClientRect();
                const offset = rRect.top - cRect.top;
                container.scrollTop = container.scrollTop + offset;
                // 3) 다시 한 번 페이지 강제 (컨테이너 스크롤이 페이지 스크롤도 트리거할 수 있음)
                window.scrollTo(0, 0);
            } else {
                foundRow.scrollIntoView({block: 'start', inline: 'nearest', behavior: 'instant'});
                window.scrollTo(0, 0);
            }
            return true;
        }""",
        username,
    )


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("progress", nargs="?", default=str(DEFAULT_PROGRESS))
    ap.add_argument("--display", type=int, default=1,
                    help="캡처할 모니터 (mss 인덱스, 1=주모니터, 2,3=보조). 0=전체합본")
    args = ap.parse_args()
    progress_path = Path(args.progress)
    monitor = args.display
    print(f"[info] progress: {progress_path}")
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    post_id = data["post_id"]
    post_url = data["post_url"]
    folder_name = data.get("folder_name") or post_id  # backward-compat
    total_count = sum(1 + len(t.get("replies", [])) for t in data["threads"])
    post_dir = PROJECT / "output" / folder_name / f"스크린샷({total_count})"

    targets = []
    for ti, t in enumerate(data["threads"], start=1):
        # root 먼저
        root = t["root"]
        uname = root["username"]
        out = post_dir / f"{ti:02d}_{safe(uname)}" / "댓글.png"
        targets.append((ti, "원댓글", uname, out))
        # 대댓글
        for ri, c in enumerate(t.get("replies", []), start=1):
            uname = c["username"]
            out = post_dir / f"{ti:02d}_{ri:02d}_{safe(uname)}" / "댓글.png"
            targets.append((ti, "대댓글", uname, out))

    print(f"[info] 총 {len(targets)}개 댓글 캡처 예정")

    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as p:
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            channel="chrome",
            headless=False,
            viewport=VIEWPORT,
            locale="ko-KR",
        )

        async def block_follow(route, request):
            if "/friendships/create/" in request.url or "/friendships/destroy/" in request.url:
                await route.abort()
                return
            await route.continue_()
        await ctx.route("**/api/v1/friendships/**", block_follow)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if not await is_logged_in(ctx):
            print("[로그인 필요] 직접 로그인. 5분 대기.")
            try:
                await page.goto("https://www.instagram.com/accounts/login/")
            except Exception:
                pass
            for i in range(150):
                await asyncio.sleep(2)
                if await is_logged_in(ctx):
                    print(f"[ok] 로그인 ({(i+1)*2}s)")
                    break
            else:
                print("[abort] 로그인 timeout")
                await ctx.close()
                return 1
        else:
            print("[ok] 기존 세션 사용")

        print(f"\n[step] navigate {post_url}")
        await page.goto(post_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(DELAY_AFTER_NAV)

        sig = await detect_stop(page)
        if sig:
            print(f"[abort] 세션 막힘: {sig}")
            await ctx.close()
            return 2

        # 메타 기준 타겟 댓글 수 계산 (lazy load 종료 기준)
        target_comment_count = sum(1 + len(t.get("replies", [])) for t in data["threads"])
        # 답글 펼치기 전이라 댓글만 카운트하면 됨 (시간 element 는 댓글당 1개)
        target_root_count = len(data["threads"])
        print(f"[step] 댓글 더 불러오기 (target={target_root_count}개 원댓글)...")
        more_clicks = await click_load_more_comments(page, target_count=target_root_count)
        print(f"  {more_clicks} 클릭")

        print("[step] 숨겨진 댓글 + 답글 펼치기...")
        await click_show_hidden(page)
        await asyncio.sleep(1.5)
        reply_clicks = await click_view_replies(page)
        print(f"  답글 펼치기: {reply_clicks} 클릭")

        # 답글 펼친 후 한 번 더 댓글 lazy load 시도 (혹시 못 본 부분)
        n_after = await page.evaluate("() => document.querySelectorAll('time[datetime]').length")
        print(f"[step] 펼친 후 time elements: {n_after}, 메타 기대: {target_comment_count + 1} (포스트 1 + 댓글)")
        if n_after < target_comment_count:
            print(f"  추가 lazy load 시도...")
            more_clicks2 = await click_load_more_comments(page, hard_timeout=180, target_count=target_comment_count + 1)
            print(f"  추가 클릭: {more_clicks2}")

        await page.evaluate("""() => {
            document.querySelectorAll('video').forEach(v => {
                try { v.pause(); v.currentTime = 0; } catch (e) {}
            });
        }""")
        await page.mouse.move(0, 0)
        await asyncio.sleep(0.5)

        print(f"\n[step] 개별 캡처 시작 ({len(targets)}개)")
        captured = 0
        skipped = 0
        failed = []
        idle_counter = 0

        for idx, (ti, kind, uname, out) in enumerate(targets, start=1):
            if out.exists() and out.stat().st_size >= MIN_CAPTURE_KB * 1024:
                skipped += 1
                continue

            idle_counter += 1
            if idle_counter >= IDLE_EVERY_N:
                print(f"\n  [idle] {IDLE_DURATION}초 대기 (자동화 회피)...")
                await asyncio.sleep(IDLE_DURATION)
                idle_counter = 0
                await page.evaluate("""() => {
                    document.querySelectorAll('video').forEach(v => v.pause());
                }""")

            ok = await scroll_target_to_top(page, uname)
            if not ok:
                print(f"  [warn] {idx}/{len(targets)} {uname}: row not found")
                failed.append(uname)
                continue
            await asyncio.sleep(0.8)

            await page.mouse.move(0, 0)
            await page.evaluate("""() => {
                document.querySelectorAll('video').forEach(v => {
                    try { v.pause(); } catch (e) {}
                });
            }""")
            await asyncio.sleep(0.3)

            try:
                await fullscreen_capture(page, out, monitor=monitor)
                size_kb = out.stat().st_size / 1024
                print(f"  ✓ {idx}/{len(targets)} {out.parent.name}: {size_kb:.0f} KB")
                captured += 1
            except Exception as e:
                print(f"  ✗ {idx}/{len(targets)} {uname}: {e}")
                failed.append(uname)

            if idx % 10 == 0:
                sig = await detect_stop(page)
                if sig:
                    print(f"\n[STOP] 세션 막힘: {sig}")
                    break

            await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

        print(f"\n[댓글 done] 캡처={captured}, 스킵={skipped}, 실패={len(failed)}")
        if failed:
            print(f"  실패: {failed}")

        # === 프로필 캡처 ===
        print(f"\n[step] 프로필 캡처 시작")
        unique_users = []  # (ti, ri or None, uname) — ri=None 이면 root
        seen = set()
        for ti, t in enumerate(data["threads"], start=1):
            u = t["root"].get("username")
            if u and u not in seen:
                seen.add(u)
                unique_users.append((ti, None, u))
            for ri, r in enumerate(t.get("replies", []), start=1):
                u = r.get("username")
                if u and u not in seen:
                    seen.add(u)
                    unique_users.append((ti, ri, u))

        print(f"  유니크 계정: {len(unique_users)}개")
        p_captured = 0
        p_skipped = 0
        p_failed = []
        for idx, (ti, ri, uname) in enumerate(unique_users, start=1):
            if ri is None:
                ent_name = f"{ti:02d}_{safe(uname)}"
            else:
                ent_name = f"{ti:02d}_{ri:02d}_{safe(uname)}"
            out = post_dir / ent_name / "프로필.png"
            if out.exists() and out.stat().st_size >= MIN_CAPTURE_KB * 1024:
                p_skipped += 1
                continue
            url = f"https://www.instagram.com/{uname}/"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                await asyncio.sleep(3.5)
            except Exception as e:
                print(f"  ✗ {idx}/{len(unique_users)} {uname}: navigate {e}")
                p_failed.append(uname)
                continue

            sig = await detect_stop(page)
            if sig:
                print(f"\n[STOP] 프로필 캡처 중 막힘: {sig}")
                break

            await page.evaluate("window.scrollTo(0, 0)")
            await page.mouse.move(0, 0)
            await asyncio.sleep(0.5)
            try:
                await fullscreen_capture(page, out, monitor=monitor)
                size_kb = out.stat().st_size / 1024
                print(f"  ✓ {idx}/{len(unique_users)} {uname}: {size_kb:.0f} KB")
                p_captured += 1
            except Exception as e:
                print(f"  ✗ {idx}/{len(unique_users)} {uname}: {e}")
                p_failed.append(uname)

            await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

        print(f"\n[프로필 done] 캡처={p_captured}, 스킵={p_skipped}, 실패={len(p_failed)}")
        if p_failed:
            print(f"  실패: {p_failed}")

        await ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
