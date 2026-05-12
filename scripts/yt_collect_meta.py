#!/usr/bin/env python3
"""
YouTube Shorts 댓글/답글 메타데이터 수집.

핵심:
- /watch?v={id} 페이지 로드 → ytcfg + ytInitialData 파싱
- POST /youtubei/v1/next (continuation token) 페이지네이션
- frameworkUpdates.entityBatchUpdate.mutations[].payload.commentEntityPayload 파싱
- 답글: commentThreadRenderer.replies.commentRepliesRenderer.subThreads[0].continuationItemRenderer
- raw JSON + sha256 (chain of custody)
- progress.json 스키마는 IG 와 동일 (threads/root/replies, 11컬럼 호환)

Usage:
  python collect_meta.py <video_url_or_id> [--limit N]
"""
import argparse
import asyncio
import json
import random
import re
import sys
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _browser import get_context, safe_close  # noqa: E402

PROJECT = Path.cwd()
USER_DATA_DIR = str(PROJECT / "chrome_session")
VIEWPORT = {"width": 1280, "height": 800}
KST = timezone(timedelta(hours=9))


def parse_video_id(s: str) -> str:
    s = s.strip()
    # raw id
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = re.search(r"(?:/shorts/|[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})", s)
    if not m:
        raise ValueError(f"video_id 추출 실패: {s}")
    return m.group(1)


# ---- in-page JS ---------------------------------------------------------

EXTRACT_CFG_JS = r"""
() => {
    const d = (window.ytcfg && window.ytcfg.data_) || {};
    return {
        api_key: d.INNERTUBE_API_KEY,
        client_name: d.INNERTUBE_CLIENT_NAME,
        client_version: d.INNERTUBE_CLIENT_VERSION,
        context: d.INNERTUBE_CONTEXT,
    };
}
"""

FIND_COMMENTS_CONTINUATION_JS = r"""
() => {
    const data = window.ytInitialData;
    if (!data) return null;
    function walk(o, depth) {
        if (depth > 25 || !o || typeof o !== 'object') return null;
        if (o.continuationItemRenderer) {
            const cmd = o.continuationItemRenderer.continuationEndpoint;
            const tok = cmd && cmd.continuationCommand && cmd.continuationCommand.token;
            if (tok) return tok;
        }
        for (const k of Object.keys(o)) {
            const v = o[k];
            if (Array.isArray(v)) {
                for (const it of v) { const r = walk(it, depth+1); if (r) return r; }
            } else if (typeof v === 'object') {
                const r = walk(v, depth+1); if (r) return r;
            }
        }
        return null;
    }
    for (const p of (data.engagementPanels || [])) {
        const t = walk(p, 0);
        if (t) return t;
    }
    return walk(data, 0);
}
"""

POST_NEXT_JS = r"""
async ([apiKey, body]) => {
    let r;
    try {
        r = await fetch(`/youtubei/v1/next?key=${apiKey}&prettyPrint=false`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
            credentials: 'include',
        });
    } catch (e) { return { __error: 'fetch_threw', message: String(e) }; }
    let j = null;
    try { j = await r.json(); }
    catch (e) { return { __error: 'json_parse', status: r.status }; }
    j.__status = r.status;
    return j;
}
"""


# ---- response parsing ---------------------------------------------------

def extract_continuation_items(res: dict) -> list:
    """
    응답에서 continuation 아이템 리스트 반환.
    - root 첫 호출: reloadContinuationItemsCommand
    - root 다음 페이지 / 답글: appendContinuationItemsAction (Action 임! Command 아님)
    """
    eps = res.get("onResponseReceivedEndpoints") or []
    out = []
    for ep in eps:
        for k in ("reloadContinuationItemsCommand",
                  "appendContinuationItemsAction",
                  "appendContinuationItemsCommand"):  # 혹시 모를 변형
            if k in ep:
                out.extend(ep[k].get("continuationItems") or [])
    return out


def find_next_continuation(items: list) -> str | None:
    """items 끝의 continuationItemRenderer 토큰 반환 (없으면 None = 끝).

    토큰 위치 두 패턴:
    - root 페이지네이션: continuationEndpoint.continuationCommand.token
    - 답글 "답글 더보기" 버튼: button.buttonRenderer.command.continuationCommand.token
    """
    for it in reversed(items):
        cir = it.get("continuationItemRenderer")
        if not cir:
            continue
        # 패턴 1: continuationEndpoint
        cmd = cir.get("continuationEndpoint", {}).get("continuationCommand", {})
        tok = cmd.get("token")
        if tok:
            return tok
        # 패턴 2: button.buttonRenderer.command (답글 더보기)
        btn = cir.get("button", {}).get("buttonRenderer", {})
        cmd2 = btn.get("command", {}).get("continuationCommand", {})
        tok2 = cmd2.get("token")
        if tok2:
            return tok2
    return None


def find_reply_continuation(thread_renderer: dict) -> str | None:
    """commentThreadRenderer.replies.commentRepliesRenderer 의 첫 reply 토큰."""
    crc = thread_renderer.get("replies", {}).get("commentRepliesRenderer", {})
    # subThreads[*] + contents[*] 모든 cir 검사
    for arr_key in ("subThreads", "contents"):
        for st in crc.get(arr_key, []) or []:
            cir = st.get("continuationItemRenderer")
            if not cir:
                continue
            for path in (("continuationEndpoint", "continuationCommand"),
                         ("button", "buttonRenderer", "command", "continuationCommand")):
                cur = cir
                for k in path:
                    cur = cur.get(k, {}) if isinstance(cur, dict) else {}
                tok = cur.get("token") if isinstance(cur, dict) else None
                if tok:
                    return tok
    return None


def collect_all_continuation_tokens(obj, found: list, kind_hint: str = "any") -> None:
    """응답 트리 어디서든 continuationCommand.token 을 모두 추출.
    - continuationEndpoint.continuationCommand
    - buttonRenderer.command.continuationCommand
    - commentThreadRenderer.replies 안의 토큰들 (inner reply expansion)
    """
    if isinstance(obj, dict):
        # 패턴 1
        ce = obj.get("continuationEndpoint")
        if isinstance(ce, dict):
            cc = ce.get("continuationCommand")
            if isinstance(cc, dict):
                tok = cc.get("token")
                if tok:
                    found.append(tok)
        # 패턴 2
        if "buttonRenderer" in obj:
            br = obj["buttonRenderer"]
            if isinstance(br, dict):
                cmd = br.get("command", {})
                cc = cmd.get("continuationCommand") if isinstance(cmd, dict) else None
                if isinstance(cc, dict):
                    tok = cc.get("token")
                    if tok:
                        found.append(tok)
        for v in obj.values():
            collect_all_continuation_tokens(v, found, kind_hint)
    elif isinstance(obj, list):
        for it in obj:
            collect_all_continuation_tokens(it, found, kind_hint)


def index_entity_payloads(res: dict) -> dict[str, dict]:
    """commentEntityPayload 를 commentId 로 인덱싱."""
    out = {}
    fu = res.get("frameworkUpdates", {})
    for m in fu.get("entityBatchUpdate", {}).get("mutations", []) or []:
        cep = m.get("payload", {}).get("commentEntityPayload")
        if cep:
            cid = cep.get("properties", {}).get("commentId")
            if cid and cid not in out:
                out[cid] = cep
    return out


def thread_iter(items: list):
    """items 에서 commentThreadRenderer 만 yield (id, thread_renderer)."""
    for it in items:
        ctr = it.get("commentThreadRenderer")
        if not ctr:
            continue
        cvm = ctr.get("commentViewModel", {}).get("commentViewModel", {})
        cid = cvm.get("commentId")
        if cid:
            yield cid, ctr


def build_record(cep: dict, video_id: str, parent_id: str | None = None) -> dict:
    props = cep.get("properties", {}) or {}
    author = cep.get("author", {}) or {}
    toolbar = cep.get("toolbar", {}) or {}

    cid = props.get("commentId") or ""
    text = (props.get("content") or {}).get("content") or ""
    handle = author.get("displayName") or ""  # @handle 형식
    handle_clean = handle.lstrip("@")
    channel_id = author.get("channelId") or ""
    canonical = author.get("canonicalBaseUrl") or ""  # /@handle

    # profile url: handle 우선, 없으면 channel_id
    if canonical:
        profile_url = f"https://www.youtube.com{canonical}"
    elif handle_clean:
        profile_url = f"https://www.youtube.com/@{handle_clean}"
    elif channel_id:
        profile_url = f"https://www.youtube.com/channel/{channel_id}"
    else:
        profile_url = ""

    # comment url: /shorts/{id}?lc={comment_id}
    comment_url = f"https://www.youtube.com/shorts/{video_id}?lc={cid}" if cid else ""

    # likes: 표시 문자열 그대로 ("5.5천")
    likes_disp = toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked") or "0"
    # 숫자 변환 시도
    likes_num = parse_count(likes_disp)

    return {
        "username": handle,                       # @최만덕-l7y
        "display_name": handle_clean,             # 최만덕-l7y
        "is_private": False,                      # YT 댓글은 항상 공개
        "content": text,
        "comment_url": comment_url,
        "profile_url": profile_url,
        "created_at": props.get("publishedTime") or "",  # "3일 전" 상대 표기
        "likes": likes_num,
        "_cid": cid,
        "_parent": parent_id,
        "_likes_display": likes_disp,
        "_channel_id": channel_id,
        "_reply_count_display": toolbar.get("replyCount") or "0",
    }


def parse_count(s: str) -> int:
    """'5.5천' '1.2만' '123' → int."""
    if not s:
        return 0
    s = s.strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([천만억K M B])?", s)
    if not m:
        try:
            return int(s)
        except Exception:
            return 0
    num = float(m.group(1))
    unit = (m.group(2) or "").strip()
    mult = {"천": 1_000, "만": 10_000, "억": 100_000_000,
            "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(unit, 1)
    return int(num * mult)


# ---- main ---------------------------------------------------------------

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=0,
                    help="root 댓글 수집 상한 (0=무제한)")
    ap.add_argument("--no-replies", action="store_true",
                    help="답글 수집 건너뛰기 (테스트용)")
    ap.add_argument("--cdp", default=None, help="CDP URL (run.py 가 띄운 Chrome 어태치)")
    args = ap.parse_args()

    video_id = parse_video_id(args.url)
    canonical_url = f"https://www.youtube.com/shorts/{video_id}"
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    print(f"[info] video_id={video_id}")
    print(f"[info] canonical_url={canonical_url}")
    print(f"[info] limit={args.limit or '∞'} no_replies={args.no_replies}")

    raw_log: list = []
    started_at = datetime.now(KST).isoformat()

    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as p:
        ctx, owns_ctx = await get_context(
            p, args.cdp, USER_DATA_DIR,
            channel="chrome", headless=False,
            no_viewport=True, locale="ko-KR",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        print(f"\n[step] navigate watch")
        await page.goto(watch_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        cfg = await page.evaluate(EXTRACT_CFG_JS)
        if not cfg.get("api_key"):
            print("[abort] api_key 없음")
            await safe_close(ctx, owns_ctx)
            return 1
        api_key = cfg["api_key"]
        ctx_obj = cfg["context"]
        print(f"[ok] api_key={api_key} client={cfg['client_name']}/{cfg['client_version']}")

        # 댓글 lazy load 트리거
        for _ in range(8):
            await page.mouse.wheel(0, 600)
            await asyncio.sleep(0.5)
        await asyncio.sleep(2)

        token = await page.evaluate(FIND_COMMENTS_CONTINUATION_JS)
        if not token:
            print("[abort] comments continuation token 없음")
            await safe_close(ctx, owns_ctx)
            return 1
        print(f"[ok] init continuation: ...{token[-16:]}")

        # ---- root 댓글 페이지네이션 ----
        roots: dict[str, dict] = {}        # cid → record
        thread_renderers: dict[str, dict] = {}  # cid → renderer (for reply token)
        page_idx = 0
        while token:
            page_idx += 1
            body = {"context": ctx_obj, "continuation": token}
            res = await page.evaluate(POST_NEXT_JS, [api_key, body])
            raw_log.append({
                "phase": "root",
                "iteration": page_idx,
                "continuation_tail": token[-32:],
                "response": res,
            })
            if res.get("__error"):
                print(f"  [root] page {page_idx} error: {res}")
                break

            items = extract_continuation_items(res)
            entities = index_entity_payloads(res)

            new_cnt = 0
            for cid, ctr in thread_iter(items):
                if cid in roots:
                    continue
                cep = entities.get(cid)
                if not cep:
                    continue
                roots[cid] = build_record(cep, video_id, parent_id=None)
                thread_renderers[cid] = ctr
                new_cnt += 1
                if args.limit and len(roots) >= args.limit:
                    break

            print(f"  [root] page {page_idx}: +{new_cnt} → {len(roots)}")

            if args.limit and len(roots) >= args.limit:
                print(f"  [root] limit {args.limit} reached, stop")
                break

            token = find_next_continuation(items)
            if not token:
                print(f"  [root] no more continuation, end")
                break
            await asyncio.sleep(random.uniform(2.0, 3.5))

        print(f"\n[done] root: {len(roots)} comments over {page_idx} pages")

        # ---- 답글 ----
        replies: dict[str, dict] = {}
        if not args.no_replies:
            targets = [(cid, ctr) for cid, ctr in thread_renderers.items()
                       if ctr.get("replies")]
            print(f"\n[step] replies: {len(targets)} root with replies")
            for i, (parent_cid, ctr) in enumerate(targets, 1):
                # 큐 기반 — 응답에서 발견되는 모든 새 토큰을 큐에 추가 (DFS).
                # youtube-comment-downloader 패턴 차용 — 답글 묶음 + nested reply 모두 follow.
                init_tok = find_reply_continuation(ctr)
                if not init_tok:
                    continue
                queue = [init_tok]
                seen_tokens = {init_tok}
                rep_page = 0
                total_added = 0
                while queue:
                    rep_token = queue.pop(0)
                    rep_page += 1
                    body = {"context": ctx_obj, "continuation": rep_token}
                    res = await page.evaluate(POST_NEXT_JS, [api_key, body])
                    raw_log.append({
                        "phase": "reply",
                        "parent": parent_cid,
                        "iteration": rep_page,
                        "continuation_tail": rep_token[-32:],
                        "response": res,
                    })
                    if res.get("__error"):
                        break
                    entities = index_entity_payloads(res)
                    added = 0
                    for cid, cep in entities.items():
                        if cid in replies or cid == parent_cid:
                            continue
                        replies[cid] = build_record(cep, video_id, parent_id=parent_cid)
                        added += 1
                    total_added += added
                    # 응답 어디서든 발견되는 모든 새 토큰을 큐에 추가
                    found = []
                    collect_all_continuation_tokens(res, found)
                    new_tokens = 0
                    for tok in found:
                        if tok not in seen_tokens:
                            seen_tokens.add(tok)
                            queue.append(tok)
                            new_tokens += 1
                    if added or new_tokens or rep_page == 1:
                        print(f"  [reply {i}/{len(targets)}] {parent_cid} p{rep_page}: +{added} comments, +{new_tokens} tokens (queue={len(queue)})")
                    await asyncio.sleep(random.uniform(0.6, 1.2))
                if total_added:
                    print(f"  [reply {i}/{len(targets)}] {parent_cid} TOTAL: {total_added} replies in {rep_page} pages")

        await safe_close(ctx, owns_ctx)

    # ---- raw 저장 ----
    raw_path = PROJECT / "output" / f"_raw_{video_id}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_payload = {
        "video_id": video_id,
        "canonical_url": canonical_url,
        "started_at": started_at,
        "ended_at": datetime.now(KST).isoformat(),
        "limit": args.limit,
        "no_replies": args.no_replies,
        "log": raw_log,
    }
    raw_bytes = json.dumps(raw_payload, ensure_ascii=False, indent=2).encode("utf-8")
    raw_path.write_bytes(raw_bytes)
    raw_hash = sha256(raw_bytes).hexdigest()
    (PROJECT / "output" / f"_raw_{video_id}.sha256").write_text(
        f"{raw_hash}  _raw_{video_id}.json\n", encoding="utf-8")
    print(f"\n[raw] saved: {raw_path}  sha256={raw_hash[:16]}...")

    # ---- threads 빌드 ----
    threads = []
    for i, (cid, root) in enumerate(roots.items(), 1):
        thread_replies = [r for r in replies.values() if r["_parent"] == cid]
        # 정리 — 내부 키 제거
        def clean(r):
            return {k: v for k, v in r.items() if not k.startswith("_")}
        threads.append({
            "index": i,
            "root": clean(root),
            "replies": [clean(r) for r in thread_replies],
        })

    # 산출물 폴더명 (한글 + yymmdd) — 기존 progress 가 있으면 그 안의 folder_name 재사용 (resumable)
    existing_folder_name = None
    progress_check = PROJECT / "output" / f".progress_{video_id}.json"
    if progress_check.exists():
        try:
            existing_folder_name = json.loads(progress_check.read_text(encoding="utf-8")).get("folder_name")
        except Exception:
            pass
    date_yymmdd = datetime.now(KST).strftime("%y%m%d")
    folder_name = existing_folder_name or f"유튜브_{date_yymmdd}_{video_id}"

    out_data = {
        "post_id": video_id,
        "post_url": canonical_url,
        "phase": "meta_collected",
        "collected_at": datetime.now(KST).isoformat(),
        "platform": "youtube",
        "platform_label": "유튜브",
        "folder_name": folder_name,
        "raw_evidence": raw_path.name,
        "raw_sha256": raw_hash,
        "threads": threads,
    }
    out_path = PROJECT / "output" / f".progress_{video_id}.json"
    out_path.write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(1 + len(t["replies"]) for t in threads)
    print(f"\n[done] saved: {out_path}")
    print(f"  root threads: {len(threads)}")
    print(f"  total (root + replies): {total}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
