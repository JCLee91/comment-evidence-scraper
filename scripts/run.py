#!/usr/bin/env python3
"""
comment-evidence-scraper — URL 보고 Instagram / YouTube 자동 분기.

사용법:
  python run.py <url>
  python run.py <url> --limit 20             # 테스트 (root N개만, YT)
  python run.py <url> --no-replies           # 답글 스킵 (테스트, YT)
  python run.py <url> --old-xlsx PATH        # IG 전용: 이전 xlsx 메타 재사용
  python run.py <url> --skip-capture         # 메타만, 캡처 스킵

산출물 (cwd 기준):
  output/.progress_{POST_ID}.json
  output/_raw_{POST_ID}.json + .sha256
  output/{POST_ID}/{NN}_원댓글_{username}/{댓글,프로필}.png
  output/{POST_ID}/{NN}_대댓글_{username}/{댓글,프로필}.png
  output/result_{POST_ID}.xlsx (11컬럼 엑셀)
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
WORK_DIR = Path.cwd()
PYTHON = sys.executable


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


def run_step(name: str, cmd: list, allow_fail: bool = False) -> int:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--limit", type=int, default=0, help="root 댓글 상한 (YT 테스트용)")
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

    platform = detect_platform(args.url)
    post_id = parse_id(args.url, platform)
    progress_path = WORK_DIR / "output" / f".progress_{post_id}.json"

    print(f"\n{'#'*60}")
    print(f"#  comment-evidence-scraper")
    print(f"#  platform: {platform.upper()}   post_id: {post_id}")
    print(f"#  url: {args.url}")
    print(f"#  cwd: {WORK_DIR}")
    print(f"{'#'*60}")

    # 1. preflight (IG 만 — YT 는 공개 댓글이라 wall 위험 적음)
    if platform == "ig" and not args.skip_preflight:
        run_step("Pre-flight (IG 세션 점검)",
                 [PYTHON, str(SCRIPTS_DIR / "ig_preflight.py"), args.url])

    # 2. 메타 수집
    if args.collect_meta or not progress_path.exists():
        if platform == "ig" and args.old_xlsx and not args.collect_meta:
            run_step("메타 import (IG xlsx)",
                     [PYTHON, str(SCRIPTS_DIR / "ig_import_meta.py"), args.old_xlsx, post_id])
        else:
            cmd = [PYTHON, str(SCRIPTS_DIR / f"{platform}_collect_meta.py"), args.url]
            if platform == "yt":
                if args.limit:
                    cmd += ["--limit", str(args.limit)]
                if args.no_replies:
                    cmd += ["--no-replies"]
            run_step(f"메타 수집 ({platform.upper()} API)", cmd)
    else:
        print(f"\n[SKIP] 메타 이미 있음: {progress_path}")

    # 3. 캡처
    if not args.skip_capture:
        run_step(f"캡처 ({platform.upper()} UI, display={args.display})",
                 [PYTHON, str(SCRIPTS_DIR / f"{platform}_capture_individual.py"),
                  str(progress_path), "--display", str(args.display)])

    # 4. 누락 보정 + 5. 검증
    data = json.loads(progress_path.read_text(encoding="utf-8"))
    folder_name = data.get("folder_name") or post_id  # backward-compat
    total_count = sum(1 + len(t.get("replies", [])) for t in data["threads"])
    post_dir = WORK_DIR / "output" / folder_name / f"스크린샷({total_count})"
    fill_missing(post_dir, data)
    ok = verify(post_dir, data)

    # 6. 엑셀 빌드 — output/{folder_name}/result.xlsx 로 자동 저장
    run_step("엑셀 빌드 (11컬럼)",
             [PYTHON, str(SCRIPTS_DIR / "build_excel.py"), str(progress_path)])

    out = WORK_DIR / "output" / folder_name / "result.xlsx"
    print(f"\n{'='*60}\n[DONE] 산출물 폴더: {WORK_DIR / 'output' / folder_name}")
    print(f"        엑셀: {out}\n{'='*60}")
    if not ok:
        print("⚠️  검증 미통과 — 수동 확인 필요 (누락된 폴더/파일 있음)")
        sys.exit(2)


if __name__ == "__main__":
    main()
