#!/usr/bin/env python3
"""
YouTube Shorts 댓글 개별 캡처.

전략:
  1) /shorts/{video_id} 1회 navigate (사용자 reference 와 동일 UI)
  2) 댓글 버튼 클릭 → 오른쪽 패널 오픈
  3) 각 comment_id 별:
     a) 패널 안에서 a[href*="lc={cid}"] 찾기
     b) 없으면 패널 내부 스크롤해서 lazy load
     c) 찾으면 scrollIntoView (패널 위쪽으로 정렬)
     d) page.screenshot() 전체 viewport — /shorts URL + 플레이어 + 댓글 함께
  4) 프로필: 별도 페이지에서 /@{handle} 또는 /channel/{id} navigate 후 screenshot
  5) resume: PNG 이미 있으면 skip
"""
import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from urllib.parse import unquote
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fullscreen import fullscreen_capture  # noqa: E402

PROJECT = Path.cwd()
USER_DATA_DIR = str(PROJECT / "chrome_session")
VIEWPORT = {"width": 1280, "height": 800}
DELAY_NAV = 4.0
JITTER_MIN = 1.0
JITTER_MAX = 2.0
MIN_KB = 50
MAX_PANEL_SCROLLS = 200  # 안전 상한


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-가-힣]+", "_", name).strip("_") or "unknown"


async def open_comments_panel(page, video_id: str):
    """/shorts navigate + 댓글 버튼 클릭."""
    url = f"https://www.youtube.com/shorts/{video_id}"
    print(f"[nav] {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await asyncio.sleep(DELAY_NAV)
    # 영상 일시정지 (스크린샷 안정화)
    try:
        await page.keyboard.press("k")  # YouTube pause shortcut
    except Exception:
        pass
    await asyncio.sleep(0.5)
    # 댓글 버튼 클릭
    try:
        await page.click('button[aria-label*="댓글"]', timeout=8000)
    except Exception:
        try:
            await page.click('button[aria-label*="omment"]', timeout=4000)
        except Exception as e:
            raise RuntimeError(f"댓글 버튼 클릭 실패: {e}")
    await asyncio.sleep(2.5)
    # 패널이 떴는지 확인
    n = await page.evaluate("() => document.querySelectorAll('ytd-comment-thread-renderer').length")
    print(f"[panel] open. initial threads in DOM: {n}")
    if n == 0:
        raise RuntimeError("댓글 패널 열렸지만 thread 0개")


async def find_thread_in_dom(page, cid: str) -> bool:
    """DOM 에서 해당 comment_id 의 thread 가 있는지 확인."""
    return await page.evaluate(
        r"""(cid) => {
            const a = document.querySelector(`ytd-comment-thread-renderer a[href*="lc=${cid}"]`);
            return !!a;
        }""",
        cid,
    )


async def scroll_panel_until_found(page, cid: str, max_scrolls: int = MAX_PANEL_SCROLLS) -> bool:
    """패널 안 스크롤하면서 cid 가 DOM 에 나타날 때까지."""
    for i in range(max_scrolls):
        if await find_thread_in_dom(page, cid):
            return True
        # 패널 내부 스크롤 — 패널 컨테이너의 scrollTop 증가
        scrolled = await page.evaluate(r"""
            () => {
                // ytd-engagement-panel-section-list-renderer scrollable inner
                const candidates = [
                    'ytd-engagement-panel-section-list-renderer:not([hidden]) #content',
                    'ytd-engagement-panel-section-list-renderer:not([hidden])',
                    'ytd-comments#comments',
                    'ytd-item-section-renderer#sections',
                ];
                for (const sel of candidates) {
                    const el = document.querySelector(sel);
                    if (el && el.scrollHeight > el.clientHeight + 10) {
                        const before = el.scrollTop;
                        el.scrollTop = before + 800;
                        if (el.scrollTop !== before) return {sel, before, after: el.scrollTop};
                    }
                }
                return null;
            }
        """)
        if not scrolled:
            # fallback: keyboard PageDown on focus inside panel
            try:
                # focus inside panel and press End/PageDown
                await page.evaluate(r"""
                    () => {
                        const a = document.querySelector('ytd-comment-thread-renderer a');
                        if (a) a.scrollIntoView({block: 'end'});
                    }
                """)
            except Exception:
                pass
        await asyncio.sleep(random.uniform(0.5, 1.0))
    return await find_thread_in_dom(page, cid)


async def expand_replies_for(page, root_cid: str) -> bool:
    """root 댓글의 '답글 N개' 또는 '답글 더보기' 버튼 모두 클릭해서 답글 펼치기."""
    # 먼저 root 가 DOM 에 있어야 함
    if not await find_thread_in_dom(page, root_cid):
        if not await scroll_panel_until_found(page, root_cid):
            return False
    # root 까지 스크롤
    await page.evaluate(
        r"""(cid) => {
            const a = document.querySelector(`ytd-comment-thread-renderer a[href*="lc=${cid}"]`);
            if (!a) return false;
            const tr = a.closest('ytd-comment-thread-renderer');
            if (tr) tr.scrollIntoView({block: 'center', behavior: 'instant'});
            return !!tr;
        }""",
        root_cid,
    )
    await asyncio.sleep(0.4)
    # 답글 펼치기 버튼 클릭 — viewReplies 같은 텍스트 또는 #more-replies
    for _ in range(15):  # 최대 15번 더보기 클릭
        clicked = await page.evaluate(
            r"""(cid) => {
                const a = document.querySelector(`ytd-comment-thread-renderer a[href*="lc=${cid}"]`);
                if (!a) return 'no-thread';
                const tr = a.closest('ytd-comment-thread-renderer');
                if (!tr) return 'no-tr';
                // 답글 펼치기 / 답글 더보기 버튼 찾기
                const candidates = tr.querySelectorAll('button, ytd-button-renderer button, #more-replies button, #more-replies, ytd-comment-replies-renderer button');
                for (const btn of candidates) {
                    const aria = (btn.getAttribute('aria-label') || '') + ' ' + (btn.textContent || '');
                    if (/답글.*\d|view.*\d.*repl|답글 더보기|Show more rep|more replies/i.test(aria)) {
                        if (btn.offsetParent === null) continue; // not visible
                        btn.click();
                        return aria.trim().slice(0, 60);
                    }
                }
                return null;
            }""",
            root_cid,
        )
        if not clicked or clicked in ("no-thread", "no-tr"):
            break
        await asyncio.sleep(random.uniform(1.0, 1.8))
    return True


async def capture_comment(page, cid: str, dst_png: Path, parent_cid: str | None = None, monitor: int = 1) -> bool:
    """패널에서 해당 댓글을 위쪽으로 정렬한 후 전체 화면 캡처 (시스템 시계 포함)."""
    ok = await find_thread_in_dom(page, cid)
    if not ok:
        # 답글이면 부모 root 의 답글 펼치기 시도
        if parent_cid:
            await expand_replies_for(page, parent_cid)
            await asyncio.sleep(0.5)
            ok = await find_thread_in_dom(page, cid)
        if not ok:
            ok = await scroll_panel_until_found(page, cid)
    if not ok:
        print(f"  [miss] cid={cid} DOM 에서 못 찾음")
        return False
    # scrollIntoView (block: 'start') — 패널 위쪽으로 정렬
    await page.evaluate(
        r"""(cid) => {
            const a = document.querySelector(`ytd-comment-thread-renderer a[href*="lc=${cid}"]`);
            if (!a) return false;
            const tr = a.closest('ytd-comment-thread-renderer');
            if (!tr) return false;
            tr.scrollIntoView({block: 'start', behavior: 'instant'});
            return true;
        }""",
        cid,
    )
    await asyncio.sleep(0.7)
    # 마우스 이동 (hover artifact 제거)
    await page.mouse.move(0, 0)
    await asyncio.sleep(0.3)
    await fullscreen_capture(page, dst_png, monitor=monitor)
    if dst_png.stat().st_size < MIN_KB * 1024:
        print(f"  [tiny] {dst_png.name} {dst_png.stat().st_size}B")
        return False
    return True


async def capture_profile(page, profile_url: str, dst_png: Path, monitor: int = 1) -> bool:
    print(f"  [profile] → {profile_url}")
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        print(f"    nav err: {e}")
        return False
    await asyncio.sleep(3)
    try:
        await fullscreen_capture(page, dst_png, monitor=monitor)
    except Exception as e:
        print(f"    screenshot err: {e}")
        return False
    return dst_png.stat().st_size >= MIN_KB * 1024


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("progress")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--display", type=int, default=1,
                    help="캡처할 모니터 (mss 인덱스, 1=주모니터, 2,3=보조). 0=전체합본")
    args = ap.parse_args()

    progress_path = Path(args.progress).resolve()
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    video_id = data["post_id"]
    threads = data["threads"]
    if args.limit:
        threads = threads[:args.limit]

    folder_name = data.get("folder_name") or video_id  # backward-compat
    # 스크린샷(N) — N = root + replies 합산
    total_count = sum(1 + len(t.get("replies", [])) for t in threads)
    screenshot_dir_name = f"스크린샷({total_count})"
    out_dir = PROJECT / "output" / folder_name / screenshot_dir_name
    out_dir.mkdir(parents=True, exist_ok=True)

    # 캡처 대상: root + reply 평탄화 (위계 표기: 원댓글 NN, 대댓글 NN_RR)
    targets = []
    for t in threads:
        r = t["root"]
        rm = re.search(r"lc=([A-Za-z0-9_.-]+)", r.get("comment_url", ""))
        root_cid = rm.group(1) if rm else None
        ti = t["index"]
        # root: ("원댓글", thread_idx, reply_idx=None, record, parent_cid)
        targets.append(("원댓글", ti, None, r, None))
        for ri, rep in enumerate(t.get("replies", []), start=1):
            targets.append(("대댓글", ti, ri, rep, root_cid))
    print(f"[plan] {len(targets)} 개 대상 → {out_dir}")

    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as p:
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, channel="chrome", headless=False,
            viewport=VIEWPORT, locale="ko-KR",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        prof_page = await ctx.new_page()  # 프로필 캡처 전용 탭

        await open_comments_panel(page, video_id)

        success = 0
        miss = 0
        for kind, ti, ri, rec, parent_cid in targets:
            uname = safe(rec.get("username") or "unknown")
            if ri is None:
                folder_name_ent = f"{ti:02d}_{uname}"            # 원댓글: 01_user1
            else:
                folder_name_ent = f"{ti:02d}_{ri:02d}_{uname}"   # 대댓글: 01_01_user2
            folder = out_dir / folder_name_ent
            cmt_png = folder / "댓글.png"
            prof_png = folder / "프로필.png"

            # comment_url 에서 cid 추출
            m = re.search(r"lc=([A-Za-z0-9_.-]+)", rec.get("comment_url", ""))
            if not m:
                print(f"  [skip] {folder_name_ent}: comment_url 에 lc 없음")
                miss += 1
                continue
            cid = m.group(1)

            # 댓글 캡처
            if cmt_png.exists() and cmt_png.stat().st_size > MIN_KB * 1024:
                print(f"  [skip] {folder_name_ent}/댓글.png exists")
            else:
                ok = await capture_comment(page, cid, cmt_png, parent_cid=parent_cid, monitor=args.display)
                if ok:
                    print(f"  [ok] {folder_name_ent}/댓글.png")
                else:
                    miss += 1
                await asyncio.sleep(random.uniform(JITTER_MIN, JITTER_MAX))

            # 프로필 캡처
            if prof_png.exists() and prof_png.stat().st_size > MIN_KB * 1024:
                pass
            else:
                purl = rec.get("profile_url", "")
                if purl:
                    ok = await capture_profile(prof_page, purl, prof_png, monitor=args.display)
                    if ok:
                        print(f"  [ok] {folder_name_ent}/프로필.png")
                    else:
                        print(f"  [miss-prof] {folder_name_ent}")
                await asyncio.sleep(random.uniform(0.5, 1.0))

            success += 1

        print(f"\n[done] {success}/{len(targets)} processed, miss={miss}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
