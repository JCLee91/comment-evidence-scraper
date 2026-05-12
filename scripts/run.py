#!/usr/bin/env python3
"""
comment-evidence-scraper — URL 보고 Instagram / YouTube 자동 분기.

사용법:
  python run.py <url>
  python run.py <url> --limit 20             # 테스트 (IG·YT 공통, root N개만)
  python run.py <url> --no-replies           # 답글 스킵 (테스트, YT)
  python run.py <url> --old-xlsx PATH        # IG 전용: 이전 xlsx 메타 재사용
  python run.py <url> --skip-capture         # 메타만, 캡처 스킵
  python run.py <url> --display 2            # 캡처 모니터 (외부 모니터)

브라우저 라이프사이클:
  run.py 가 시작 시 Chrome 1회 launch (chrome_session/ + CDP :9222) →
  4단계 (preflight·meta·capture) 가 모두 같은 인스턴스에 attach →
  파이프라인 끝나면 종료. 단계 사이에서 절대 종료되지 않음.
"""
import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright
from playwright_stealth import Stealth

SCRIPTS_DIR = Path(__file__).resolve().parent
WORK_DIR = Path.cwd()
PYTHON = sys.executable
CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}"
VIEWPORT = {"width": 1280, "height": 800}


def detect_platform(url: str) -> str:
    if "instagram.com" in url:
        return "ig"
    if "youtube.com" in url or "youtu.be" in url:
        return "yt"
    raise ValueError(f"플랫폼 감지 실패 (instagram.com / youtube.com 만 지원): {url}")


def parse_id(url: str, platform: str) -> str:
    if platform == "ig":
        m = re.search(r"/(p|reel|reels)/([A-Za-z0-9_-]+)", url)
        if not m:
            raise ValueError(f"IG post_id 추출 실패: {url}")
        return m.group(2)
    # YouTube
    s = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    m = re.search(r"(?:/shorts/|[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})", s)
    if not m:
        raise ValueError(f"YT video_id 추출 실패: {url}")
    return m.group(1)


async def wait_for_cdp(url: str = CDP_URL, timeout: float = 20.0) -> bool:
    """Chrome CDP HTTP 엔드포인트가 응답할 때까지 폴링."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            await asyncio.to_thread(
                urllib.request.urlopen, f"{url}/json/version", timeout=1
            )
            return True
        except Exception:
            await asyncio.sleep(0.3)
    return False


async def run_step(name: str, cmd: list, allow_fail: bool = False) -> int:
    print(f"\n{'='*60}\n[STEP] {name}\n{'='*60}")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(WORK_DIR))
    rc = await proc.wait()
    if rc != 0 and not allow_fail:
        print(f"[ABORT] {name} 실패 (exit {rc})")
        sys.exit(rc)
    return rc


def run_step_sync(name: str, cmd: list, allow_fail: bool = False) -> int:
    """브라우저 안 쓰는 단계용 (ig_import_meta, build_excel)."""
    print(f"\n{'='*60}\n[STEP] {name}\n{'='*60}")
    r = subprocess.run(cmd, cwd=str(WORK_DIR))
    if r.returncode != 0 and not allow_fail:
        print(f"[ABORT] {name} 실패 (exit {r.returncode})")
        sys.exit(r.returncode)
    return r.returncode


def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\-가-힣]+", "_", name).strip("_") or "unknown"


def _ent_name(ti: int, ri: int | None, uname: str) -> str:
    """위계 표기: root=NN_user, reply=NN_RR_user"""
    if ri is None:
        return f"{ti:02d}_{safe(uname)}"
    return f"{ti:02d}_{ri:02d}_{safe(uname)}"


def fill_missing(post_dir: Path, data: dict) -> int:
    """같은 username 다른 폴더에서 PNG 복사 (한 사용자 여러 댓글 시)."""
    print(f"\n[STEP] 누락 자동 보정")
    if not post_dir.exists():
        print("  post_dir 없음 (캡처 스킵?)")
        return 0
    filled = 0
    for ti, t in enumerate(data["threads"], start=1):
        # root + replies 평탄화 with ri
        items = [(None, t["root"])] + [(ri, r) for ri, r in enumerate(t.get("replies", []), start=1)]
        for ri, c in items:
            uname = c["username"]
            target = post_dir / _ent_name(ti, ri, uname)
            target.mkdir(parents=True, exist_ok=True)
            for fname in ("댓글.png", "프로필.png"):
                if (target / fname).exists():
                    continue
                sources = [
                    d for d in post_dir.iterdir()
                    if d.is_dir() and not d.name.startswith("_")
                    and d.name.endswith(f"_{safe(uname)}")
                    and d.name != target.name
                    and (d / fname).exists()
                ]
                if sources:
                    shutil.copy2(sources[0] / fname, target / fname)
                    filled += 1
    print(f"  보정: {filled}건")
    return filled


def verify(post_dir: Path, data: dict) -> bool:
    print(f"\n[STEP] 최종 검증")
    expected = set()
    for ti, t in enumerate(data["threads"], start=1):
        expected.add(_ent_name(ti, None, t["root"]["username"]))
        for ri, r in enumerate(t.get("replies", []), start=1):
            expected.add(_ent_name(ti, ri, r["username"]))
    if not post_dir.exists():
        print("  post_dir 없음 — 검증 skip")
        return False
    existing = {d.name for d in post_dir.iterdir() if d.is_dir() and not d.name.startswith("_")}
    missing_folders = expected - existing
    missing_files = []
    for d in post_dir.iterdir():
        if d.is_dir() and not d.name.startswith("_"):
            for f in ("댓글.png", "프로필.png"):
                if not (d / f).exists():
                    missing_files.append(f"{d.name}/{f}")
    print(f"  expected: {len(expected)} / existing: {len(existing)}")
    print(f"  누락 폴더: {len(missing_folders)}, 누락 PNG: {len(missing_files)}")
    for f in sorted(missing_folders)[:5]:
        print(f"    - {f}")
    return not missing_folders and not missing_files


def start_caffeinate() -> subprocess.Popen | None:
    """macOS 에서 작업 동안 디스플레이 sleep / idle sleep 차단.
    안 막으면 잠금화면 / 스크린세이버가 풀스크린 캡처에 그대로 박힘
    (mss 가 OS 레벨이라 위에 뜬 화면을 그대로 잡음)."""
    if sys.platform != "darwin":
        return None
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-di"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[caffeinate] display + idle sleep 차단 (pid {proc.pid})")
        return proc
    except FileNotFoundError:
        print("[warn] caffeinate 없음 — 잠금/스크린세이버 수동 차단 필요")
        return None


def stop_caffeinate(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def sanitize_chrome_session(session_dir: Path) -> None:
    """이전 작업이 비정상 종료(예: pkill)됐을 때 남는 dirty state 를 정리.
    안 정리하면 다음 launch 시 '예기치 못하게 종료' 배너 + '복원' 탭이 떠서
    풀스크린 스크린샷에 그대로 박힘 (법률 자료 무결성 침해)."""
    pref_path = session_dir / "Default" / "Preferences"
    if not pref_path.exists():
        return
    try:
        prefs = json.loads(pref_path.read_text(encoding="utf-8"))
        prof = prefs.setdefault("profile", {})
        if prof.get("exit_type") != "Normal" or prof.get("exited_cleanly") is False:
            print(f"[browser] dirty shutdown 흔적 감지 → 정리")
        prof["exit_type"] = "Normal"
        prof["exited_cleanly"] = True
        pref_path.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception as e:
        print(f"[warn] Preferences 정리 실패 (무시): {e}")


async def run_pipeline(args, platform: str, post_id: str, progress_path: Path):
    """브라우저 기반 단계들 — Chrome 1회 launch, 같은 인스턴스 공유."""
    session_dir = WORK_DIR / "chrome_session"
    sanitize_chrome_session(session_dir)
    stealth = Stealth()
    async with stealth.use_async(async_playwright()) as p:
        print(f"\n[browser] persistent context launch (CDP :{CDP_PORT})")
        ctx = await p.chromium.launch_persistent_context(
            str(session_dir),
            channel="chrome",
            headless=False,
            no_viewport=True,  # maximized 윈도우 그대로 — viewport 강제 X
            locale="ko-KR",
            args=[
                f"--remote-debugging-port={CDP_PORT}",
                "--start-maximized",
                "--disable-session-crashed-bubble",  # dirty shutdown 시 복원 배너 차단
                "--hide-crash-restore-bubble",       # 구버전 대체 플래그
                "--no-default-browser-check",        # 기본 브라우저 확인 다이얼로그 차단
                "--no-first-run",
            ],
        )
        try:
            if not await wait_for_cdp():
                print(f"[ABORT] CDP 응답 없음: {CDP_URL}")
                sys.exit(10)
            print(f"[browser] CDP ready: {CDP_URL}")

            # 1. preflight (IG 만 — YT 는 공개 댓글)
            if platform == "ig" and not args.skip_preflight:
                await run_step(
                    "Pre-flight (IG 세션 점검)",
                    [PYTHON, str(SCRIPTS_DIR / "ig_preflight.py"),
                     args.url, "--cdp", CDP_URL],
                )

            # 2. 메타 수집
            if args.collect_meta or not progress_path.exists():
                if platform == "ig" and args.old_xlsx and not args.collect_meta:
                    # xlsx import 는 브라우저 안 씀
                    run_step_sync(
                        "메타 import (IG xlsx)",
                        [PYTHON, str(SCRIPTS_DIR / "ig_import_meta.py"),
                         args.old_xlsx, post_id],
                    )
                else:
                    cmd = [PYTHON, str(SCRIPTS_DIR / f"{platform}_collect_meta.py"),
                           args.url, "--cdp", CDP_URL]
                    if args.limit:
                        cmd += ["--limit", str(args.limit)]
                    if platform == "yt" and args.no_replies:
                        cmd += ["--no-replies"]
                    await run_step(f"메타 수집 ({platform.upper()} API)", cmd)
            else:
                print(f"\n[SKIP] 메타 이미 있음: {progress_path}")

            # 3. 캡처
            if not args.skip_capture:
                cmd = [PYTHON, str(SCRIPTS_DIR / f"{platform}_capture_individual.py"),
                       str(progress_path), "--display", str(args.display),
                       "--cdp", CDP_URL]
                if args.limit:
                    cmd += ["--limit", str(args.limit)]
                await run_step(
                    f"캡처 ({platform.upper()} UI, display={args.display})",
                    cmd,
                )
        finally:
            print(f"\n[browser] 파이프라인 종료 → context close")
            await ctx.close()


async def main_async():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=0,
                    help="root 댓글 상한 (IG·YT 공통, sanity check 전용)")
    ap.add_argument("--no-replies", action="store_true", help="답글 수집 스킵 (YT 테스트용)")
    ap.add_argument("--skip-preflight", action="store_true")
    ap.add_argument("--skip-capture", action="store_true")
    ap.add_argument("--old-xlsx", default=None, help="(IG 전용) 이전 xlsx 메타 import")
    ap.add_argument("--collect-meta", action="store_true",
                    help="(IG 전용) 강제 메타 재수집")
    ap.add_argument("--display", type=int, default=1,
                    help="캡처할 모니터 (1=주모니터, 2,3=보조). "
                         "외부 모니터 활용 시 캡처 중 본인 모니터로 다른 작업 가능")
    args = ap.parse_args()
    if args.limit < 0:
        ap.error(f"--limit 음수 불가 (입력: {args.limit})")

    platform = detect_platform(args.url)
    post_id = parse_id(args.url, platform)
    progress_path = WORK_DIR / "output" / f".progress_{post_id}.json"

    print(f"\n{'#'*60}")
    print(f"#  comment-evidence-scraper")
    print(f"#  platform: {platform.upper()}   post_id: {post_id}")
    print(f"#  url: {args.url}")
    print(f"#  cwd: {WORK_DIR}")
    print(f"{'#'*60}")

    caffeinate_proc = start_caffeinate()
    try:
        # 브라우저 기반 단계 (preflight + meta + capture) — Chrome 1회만
        await run_pipeline(args, platform, post_id, progress_path)

        # 4. 누락 보정 + 5. 검증 (브라우저 불필요)
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        folder_name = data.get("folder_name") or post_id  # backward-compat
        if args.limit:
            data["threads"] = data["threads"][:args.limit]
        total_count = sum(1 + len(t.get("replies", [])) for t in data["threads"])
        post_dir = WORK_DIR / "output" / folder_name / f"스크린샷({total_count})"
        fill_missing(post_dir, data)
        ok = verify(post_dir, data)

        # 6. 엑셀 빌드 (브라우저 불필요)
        excel_cmd = [PYTHON, str(SCRIPTS_DIR / "build_excel.py"), str(progress_path)]
        if args.limit:
            excel_cmd += ["--limit", str(args.limit)]
        run_step_sync("엑셀 빌드 (11컬럼)", excel_cmd)

        out = WORK_DIR / "output" / folder_name / "result.xlsx"
        print(f"\n{'='*60}\n[DONE] 산출물 폴더: {WORK_DIR / 'output' / folder_name}")
        print(f"        엑셀: {out}\n{'='*60}")
        if not ok:
            print("⚠️  검증 미통과 — 수동 확인 필요 (누락된 폴더/파일 있음)")
            sys.exit(2)
    finally:
        stop_caffeinate(caffeinate_proc)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
