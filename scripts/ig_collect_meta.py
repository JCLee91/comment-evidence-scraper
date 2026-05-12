#!/usr/bin/env python3
"""
인스타 게시글 댓글/대댓글 메타데이터 명시적 수집 (옵션 A).

핵심:
- 인스타 사설 API `/api/v1/media/{id}/comments/` 직접 호출
- next_min_id (headload, 최신→과거) + next_max_id (forward, 과거→최신) 양방향 cursor 완전 소진
- in-page fetch (Playwright + 로그인 쿠키) 로 same-origin 호출
- 답글: /child_comments/ API 양방향 페이지네이션
- raw JSON 별도 저장 (법적 증거용 chain of custody)
- 모든 cursor / endpoint 호출 로그 보존

Usage:
  python collect_meta.py <post_url>
"""
import argparse
import asyncio
import json
import random
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _browser import get_context, safe_close  # noqa: E402

PROJECT = Path.cwd()
USER_DATA_DIR = str(PROJECT / "chrome_session")
VIEWPORT = {"width": 1280, "height": 750}
KST = timezone(timedelta(hours=9))
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


def shortcode_to_media_id(s: str) -> str:
    m = 0
    for c in s:
        m = m * 64 + ALPHABET.index(c)
    return str(m)


def parse_post(url: str) -> str:
    m = re.search(r"/(p|reel|reels)/([A-Za-z0-9_-]+)", url)
    if not m:
        raise ValueError(f"post_id 추출 실패: {url}")
    return m.group(2)


def fmt_kst(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def build_record(raw: dict, post_url_base: str) -> dict:
    user = raw.get("user") or raw.get("owner") or {}
    pk = str(raw.get("pk") or raw.get("id") or "")
    ts = raw.get("created_at_utc") or raw.get("created_at") or 0
    uname = user.get("username") or ""
    return {
        "username": uname,
        "display_name": user.get("full_name") or "",
        "is_private": bool(user.get("is_private", False)),
        "content": raw.get("text") or "",
        "comment_url": f"{post_url_base.rstrip('/')}/c/{pk}" if pk else "",
        "profile_url": f"https://www.instagram.com/{uname}/" if uname else "",
        "created_at": fmt_kst(ts),
        "likes": int(raw.get("comment_like_count") or 0),
        "_pk": pk,
        "_ts": int(ts) if ts else 0,
    }


async def is_logged_in(ctx):
    cookies = await ctx.cookies("https://www.instagram.com")
    return any(c["name"] == "sessionid" for c in cookies)


# in-page fetch helpers ---------------------------------------------------

ROOT_FETCH_JS = r"""
async ([mediaId, mode, cursor]) => {
    const csrf = document.cookie.split('; ').find(r => r.startsWith('csrftoken='))?.split('=')[1] || '';
    const headers = {
        'X-IG-App-ID': '936619743392459',
        'X-CSRFToken': csrf,
        'X-Requested-With': 'XMLHttpRequest',
    };
    let qs = '?can_support_threading=true';
    if (mode === 'min' && cursor) qs += `&min_id=${encodeURIComponent(cursor)}`;
    if (mode === 'max' && cursor) qs += `&max_id=${encodeURIComponent(cursor)}`;
    let r;
    try {
        r = await fetch(`/api/v1/media/${mediaId}/comments/${qs}`,
                        { headers, credentials: 'include' });
    } catch (e) {
        return { __error: 'fetch_threw', message: String(e) };
    }
    const status = r.status;
    let body = null;
    try { body = await r.json(); }
    catch (e) { return { __error: 'json_parse', status }; }
    body.__status = status;
    return body;
}
"""

CHILD_FETCH_JS = r"""
async ([mediaId, commentId, mode, cursor]) => {
    const csrf = document.cookie.split('; ').find(r => r.startsWith('csrftoken='))?.split('=')[1] || '';
    const headers = {
        'X-IG-App-ID': '936619743392459',
        'X-CSRFToken': csrf,
        'X-Requested-With': 'XMLHttpRequest',
    };
    let qs = '';
    if (mode === 'min') qs = cursor ? `?min_id=${encodeURIComponent(cursor)}` : '?min_id=';
    else if (mode === 'max' && cursor) qs = `?max_id=${encodeURIComponent(cursor)}`;
    let r;
    try {
        r = await fetch(`/api/v1/media/${mediaId}/comments/${commentId}/child_comments/${qs}`,
                        { headers, credentials: 'include' });
    } catch (e) {
        return { __error: 'fetch_threw', message: String(e) };
    }
    let body = null;
    try { body = await r.json(); }
    catch (e) { return { __error: 'json_parse', status: r.status }; }
    body.__status = r.status;
    return body;
}
"""


async def fetch_all_root_comments(page, media_id: str, raw_log: list) -> list:
    """양방향 cursor 완전 소진하여 root 댓글 모두 수집."""
    collected: dict[str, dict] = {}
    expected = None

    def merge(comments):
        nonlocal collected
        for c in comments or []:
            pk = str(c.get("pk") or c.get("id") or "")
            if pk and pk not in collected:
                collected[pk] = c

    # 초기 호출
    print("  [root] init call")
    res = await page.evaluate(ROOT_FETCH_JS, [media_id, "init", ""])
    raw_log.append({"phase": "root_init", "url_qs": "?can_support_threading=true", "response": res})
    if res.get("__error"):
        print(f"  [root] init error: {res}")
        return []
    expected = res.get("comment_count")
    merge(res.get("comments"))
    print(f"  [root] init: collected={len(collected)} expected={expected} "
          f"has_more={res.get('has_more_comments')} has_more_headload={res.get('has_more_headload_comments')}")

    # forward (max_id, 더 오래된 댓글)
    cursor = res.get("next_max_id") if res.get("has_more_comments") else None
    direction = "max"
    iteration = 0
    while cursor and iteration < 100:
        iteration += 1
        await asyncio.sleep(random.uniform(2.0, 3.5))
        r = await page.evaluate(ROOT_FETCH_JS, [media_id, direction, cursor])
        raw_log.append({"phase": f"root_{direction}", "iteration": iteration, "cursor": cursor, "response": r})
        if r.get("__error"):
            print(f"  [root] {direction} err iter={iteration}: {r}")
            break
        before = len(collected)
        merge(r.get("comments"))
        print(f"  [root] {direction} iter={iteration}: +{len(collected)-before} → {len(collected)}/{expected}")
        if not r.get("has_more_comments"):
            break
        cursor = r.get("next_max_id")
        if not cursor:
            break

    # backward (min_id, headload, 더 최신 댓글)
    cursor = res.get("next_min_id") if res.get("has_more_headload_comments") else None
    direction = "min"
    iteration = 0
    while cursor and iteration < 100:
        iteration += 1
        await asyncio.sleep(random.uniform(2.0, 3.5))
        r = await page.evaluate(ROOT_FETCH_JS, [media_id, direction, cursor])
        raw_log.append({"phase": f"root_{direction}", "iteration": iteration, "cursor": cursor, "response": r})
        if r.get("__error"):
            print(f"  [root] {direction} err iter={iteration}: {r}")
            break
        before = len(collected)
        merge(r.get("comments"))
        print(f"  [root] {direction} iter={iteration}: +{len(collected)-before} → {len(collected)}/{expected}")
        if not r.get("has_more_headload_comments"):
            break
        cursor = r.get("next_min_id")
        if not cursor:
            break

    print(f"  [root] DONE: {len(collected)}/{expected}")
    return list(collected.values())


async def fetch_child_comments(page, media_id: str, parent_pk: str, raw_log: list) -> list:
    """답글: max_id + min_id 양방향."""
    collected: dict[str, dict] = {}

    def merge(items):
        for c in items or []:
            pk = str(c.get("pk") or c.get("id") or "")
            if pk and pk not in collected:
                collected[pk] = c

    for direction, has_more_key, next_key in (
        ("max", "has_more_tail_child_comments", "next_max_child_cursor"),
        ("min", "has_more_head_child_comments", "next_min_child_cursor"),
    ):
        cursor = None
        for it in range(40):
            r = await page.evaluate(CHILD_FETCH_JS, [media_id, parent_pk, direction, cursor or ""])
            raw_log.append({
                "phase": f"child_{direction}",
                "parent_pk": parent_pk,
                "iteration": it,
                "cursor": cursor,
                "response": r,
            })
            if r.get("__error"):
                break
            merge(r.get("child_comments"))
            if not r.get(has_more_key):
                break
            cursor = r.get(next_key)
            if not cursor:
                break
            await asyncio.sleep(random.uniform(0.4, 0.9))
    return list(collected.values())


# main --------------------------------------------------------------------

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=0,
                    help="root 댓글 상한 (0=무제한, sanity check 전용 — 법률 자료 금지)")
    ap.add_argument("--cdp", default=None, help="CDP URL (run.py 가 띄운 Chrome 어태치)")
    args = ap.parse_args()
    raw_url = args.url.split("?")[0].rstrip("/")
    shortcode = parse_post(raw_url)
    media_id = shortcode_to_media_id(shortcode)
    # /p/ 정규화 (Reels 도 마찬가지로 /p/ 가 댓글 API 잘 응답)
    canonical_post_url = f"https://www.instagram.com/p/{shortcode}/"

    print(f"[info] shortcode={shortcode}")
    print(f"[info] media_id={media_id}")
    print(f"[info] canonical_url={canonical_post_url}")

    raw_log: list = []
    started_at = datetime.now(KST).isoformat()

    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as p:
        ctx, owns_ctx = await get_context(
            p, args.cdp, USER_DATA_DIR,
            channel="chrome", headless=False,
            viewport=VIEWPORT, locale="ko-KR",
        )

        async def block_follow(route, request):
            if "/friendships/create/" in request.url or "/friendships/destroy/" in request.url:
                await route.abort()
                return
            await route.continue_()
        await ctx.route("**/api/v1/friendships/**", block_follow)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if not await is_logged_in(ctx):
            print("[로그인 필요] 직접 로그인 후 진행 (최대 5분 대기)")
            for _ in range(150):
                await asyncio.sleep(2)
                if await is_logged_in(ctx):
                    break
            else:
                print("[abort] 로그인 미완료")
                await safe_close(ctx, owns_ctx)
                return 1
        print("[ok] logged in")

        # /p/ 페이지 navigate (CSRF 활성화 + same-origin fetch 허용)
        await page.goto(canonical_post_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # root 댓글 양방향 cursor 호출
        print("\n[step] root comments API call")
        root_raw = await fetch_all_root_comments(page, media_id, raw_log)

        if args.limit and len(root_raw) > args.limit:
            print(f"[limit] root 댓글 {len(root_raw)} → {args.limit}개로 제한 (sanity check)")
            root_raw = root_raw[:args.limit]

        # 답글 보강
        print(f"\n[step] child_comments 보강")
        captured: dict[str, dict] = {}
        parent_links: dict[str, str] = {}
        for c in root_raw:
            pk = str(c.get("pk") or c.get("id") or "")
            if pk:
                captured[pk] = c

        targets = [(str(c.get("pk")), c.get("child_comment_count", 0))
                   for c in root_raw if c.get("child_comment_count", 0) > 0]
        print(f"  대상 댓글: {len(targets)}개 (총 답글 {sum(n for _, n in targets)})")

        for i, (pk, expected) in enumerate(targets, 1):
            try:
                children = await fetch_child_comments(page, media_id, pk, raw_log)
                added = 0
                for child in children:
                    cpk = str(child.get("pk") or child.get("id") or "")
                    if cpk and cpk not in captured:
                        captured[cpk] = child
                        parent_links[cpk] = pk
                        added += 1
                print(f"  [{i}/{len(targets)}] {pk}: 받은={len(children)} 신규={added} (expected {expected})")
            except Exception as e:
                print(f"  [{i}/{len(targets)}] {pk}: error {e}")
            await asyncio.sleep(random.uniform(0.3, 0.8))

        await safe_close(ctx, owns_ctx)

    # raw JSON 보존 (법적 증거)
    raw_path = PROJECT / "output" / f"_raw_{shortcode}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = {
        "shortcode": shortcode,
        "media_id": media_id,
        "canonical_url": canonical_post_url,
        "started_at": started_at,
        "ended_at": datetime.now(KST).isoformat(),
        "log": raw_log,
    }
    raw_bytes = json.dumps(raw_payload, ensure_ascii=False, indent=2).encode("utf-8")
    raw_path.write_bytes(raw_bytes)
    raw_hash = sha256(raw_bytes).hexdigest()
    (PROJECT / "output" / f"_raw_{shortcode}.sha256").write_text(
        f"{raw_hash}  _raw_{shortcode}.json\n", encoding="utf-8"
    )
    print(f"\n[raw] saved: {raw_path}  sha256={raw_hash[:16]}...")

    # flat → threaded 변환
    records: dict[str, dict] = {}
    for pk, raw in captured.items():
        rec = build_record(raw, canonical_post_url.rstrip("/"))
        if rec["_pk"]:
            records[pk] = rec

    roots: dict[str, dict] = {}
    replies_buf: list[tuple[str, dict]] = []
    for pk, rec in records.items():
        parent = parent_links.get(pk)
        if parent and parent in records:
            replies_buf.append((parent, rec))
        else:
            roots[pk] = rec

    sorted_roots = sorted(roots.values(), key=lambda r: r["_ts"])
    threads = []
    for i, root in enumerate(sorted_roots, start=1):
        thread_replies = sorted(
            (r for ppk, r in replies_buf if ppk == root["_pk"]),
            key=lambda r: r["_ts"],
        )
        def clean(r):
            r.pop("_pk", None)
            r.pop("_ts", None)
            return r
        threads.append({
            "index": i,
            "root": clean(root),
            "replies": [clean(r) for r in thread_replies],
        })

    # 산출물 폴더명 (한글 + yymmdd) — 기존 progress 가 있으면 그 안의 folder_name 재사용 (resumable)
    existing_folder_name = None
    progress_check = PROJECT / "output" / f".progress_{shortcode}.json"
    if progress_check.exists():
        try:
            existing_folder_name = json.loads(progress_check.read_text(encoding="utf-8")).get("folder_name")
        except Exception:
            pass
    date_yymmdd = datetime.now(KST).strftime("%y%m%d")
    folder_name = existing_folder_name or f"인스타_{date_yymmdd}_{shortcode}"

    out_data = {
        "post_id": shortcode,
        "post_url": canonical_post_url,
        "phase": "meta_collected",
        "collected_at": datetime.now(KST).isoformat(),
        "platform": "instagram",
        "platform_label": "인스타",
        "folder_name": folder_name,
        "media_id": media_id,
        "raw_evidence": str(raw_path.name),
        "raw_sha256": raw_hash,
        "threads": threads,
    }
    out_path = PROJECT / "output" / f".progress_{shortcode}.json"
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(1 + len(t["replies"]) for t in threads)
    orphans = sum(1 for ppk, _ in replies_buf if ppk not in roots)
    print(f"\n[done] saved: {out_path}")
    print(f"  threads (root): {len(threads)}")
    print(f"  total comments (root + replies): {total}")
    print(f"  raw captured: {len(captured)}")
    if orphans:
        print(f"  ⚠️  orphan replies: {orphans}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
