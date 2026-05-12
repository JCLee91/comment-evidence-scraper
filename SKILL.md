---
name: comment-evidence-scraper
description: 인스타그램 게시물 또는 유튜브 영상/Shorts URL 1개에서 모든 댓글·답글과 작성자 프로필을 캡처하여 법률 증거자료용 폴더(한글명+날짜+위계표기)와 11컬럼 엑셀로 산출. URL 만 주면 IG/YT 자동 분기. 트리거 — `instagram.com/p/...` `/reel/...` 또는 `youtube.com/shorts/...` `/watch?v=...` `youtu.be/...` URL 과 함께 "댓글 수집", "댓글 증거자료", "댓글 스크린샷", "댓글 캡처", "엑셀로 정리", "scrape comments" 같은 요청.
---

# Comment Evidence Scraper (IG + YouTube)

URL 분기 + 4단계 파이프라인 + chain of custody. 디지털 비친화 사용자(원장님 등)에게 폴더 통째로 zip 전달 가능.

**캡처는 OS 레벨 풀스크린** — 메뉴바/작업표시줄 시계까지 한 장에 박혀 타임스탬프 위조 난이도 ↑. 브라우저 viewport 가 아님.

Chrome 은 `--start-maximized` 로 launch — 다른 앱들이 Chrome 뒤로 가려지므로 데스크탑 청소 불필요. macOS 메뉴바·시계는 Chrome 위쪽에 그대로 보임.

## Setup (최초 1회)

```bash
pip install playwright playwright_stealth openpyxl mss
playwright install chrome   # 시스템 Chrome 아니면 channel="chrome" 으로 우회
```

**macOS 첫 실행 시** — Screen Recording 권한 요청 팝업 발생. Python(또는 Terminal) 에 허용. 권한 없으면 캡처 결과가 검은 화면.

## Workflow

작업 디렉토리(`cwd`) 로 cd 한 후 1줄 실행:

```bash
python ~/.claude/skills/comment-evidence-scraper/scripts/run.py <URL>
```

`run.py` 가 URL 보고 `ig_*` 또는 `yt_*` 자동 분기 → 4단계 실행 → 검증.

| 단계 | IG | YouTube |
|---|---|---|
| 1. preflight | `ig_preflight.py` (세션 점검) | (스킵 — 공개 댓글) |
| 2. 메타 | `ig_collect_meta.py` (양방향 cursor) | `yt_collect_meta.py` (큐 기반 토큰) |
| 3. 캡처 | `ig_capture_individual.py` (`/p/` UI) | `yt_capture_individual.py` (`/shorts/` UI + 답글 펼치기) |
| 4. 엑셀 | `build_excel.py` (공통 11컬럼) | (동일) |

옵션:
- `--limit N` (IG·YT root N개로 제한 — 빠른 검증. 메타·캡처·엑셀 **모든 단계**에 적용)
- `--no-replies` (답글 스킵 — 빠른 검증)
- `--skip-capture` (메타만)
- `--old-xlsx PATH` (IG 전용 — 이전 xlsx 메타 재사용)
- `--display N` (캡처할 모니터, 1=주, 2,3=보조. 외부 모니터 있으면 `--display 2` → 본인 모니터로 다른 작업 OK)

> ⚠️ **법률 자료는 `--limit` / `--no-replies` / `--skip-capture` 금지.** 이 옵션들은 sanity check 전용. 부분 결과물을 절대 zip 패키징해서 전달하지 말 것. 산출물은 항상 **전수 수집 완료본** 1개만.

> 📌 **`--limit N` 의미** — 메타 수집 단계에서 root N개로 자르고, 그 슬라이스가 캡처·검증·엑셀까지 그대로 흘러감. **기존 `.progress_*.json` 이 있어 메타 단계가 SKIP 되어도** `run.py` 가 in-memory 슬라이스 + capture/excel 에 `--limit` 전달로 동일하게 N개만 처리. 즉, "메타는 전수, 캡처만 10개" 같은 어긋난 상태가 발생하지 않음.
> - IG 캡처의 lazy load 도 슬라이스 기준 (sanity check 속도 우선). 메타 수집 순서와 UI 표시 순서가 일치 보장 없어 일부 username 이 `row not found` 가능 — 의도된 trade-off. 전수 작업은 `--limit` 안 쓰면 자동으로 전체 lazy load.
> - `--limit N` 산출물은 `output/{folder}/스크린샷({n_sliced})/` 에 들어감. 전수 작업이 `output/{folder}/스크린샷({n_full})/` 에 있어도 **별도 디렉토리**라 공존 가능. zip 패키징 전 어느 폴더를 전달하는지 반드시 확인.

플랫폼별 디테일은 `references/instagram-api.md`·`references/youtube-api.md` 필독.

## Output (1 URL → 1 bundle)

```
output/
├── 인스타_260503_DXtpZ69k4hu/        ← 메인 (zip해서 전달)
│   ├── result.xlsx
│   └── 스크린샷(33)/                  ← N = 원댓글 + 대댓글 합산
│       ├── 01_user1/                 ← 원댓글: NN_user
│       ├── 01_01_user2/              ← 답글: NN_RR_user (위계)
│       ├── 01_02_user3/
│       └── 02_user4/
├── 유튜브_260503_GklHqxXDxHw/         ← (다른 작업)
│   └── ...
├── .progress_{POST_ID}.json          ← 내부 (resumable, hidden)
└── _raw_{POST_ID}.json + .sha256     ← chain of custody (hidden)
```

폴더 명명 — `{인스타|유튜브}_{yymmdd}_{POST_ID}` / `스크린샷({N})` / 원댓글 `{NN}_{user}` / 대댓글 `{NN}_{RR}_{user}`. 알파벳 정렬 시 자연 트리.

`POST_ID`: IG `/p|reel|reels/` 다음 segment / YT 11자 video_id (shorts·watch·youtu.be 모두 동일).

엑셀 11컬럼 포맷은 `references/excel-format.md` 참조.

## Critical Rules

1. **Pre-flight 통과 후에만 본 작업** (IG 한정) — `ig_preflight.py` 가 차단/로그인 wall/challenge 감지하면 **본 작업 즉시 중단**. 자동 재시도 금지. 사용자에게 "1-2시간 후 재개" 통보 후 대기. 막힌 상태에서 본 캡처 시도 = 세션 영구 손상 위험.
2. **세션 영구화** — `cwd/chrome_session/` 재사용. 매 작업마다 깨끗한 컨텍스트 launch 금지. 세션 limit 트리거됨.
3. **시스템 Chrome + stealth** — `channel="chrome"` + `playwright_stealth`. Chrome for Testing 은 IG·YT 둘 다 차단 위험.
4. **Chain of custody** — `_raw_{POST_ID}.json` + sha256 자동 저장. 보고서에 명시.
5. **포맷 1mm 도 안 바꿈** — 11컬럼 엑셀, 한국어 라벨(원댓글/대댓글), 위계 명명, 모두 IG·YT 동일.
6. **자동 regroup 금지** — mention/멘션을 답글로 재분류하지 마라. 데이터 손실 위험.
7. **캡처 단계 hands-off** — Step 3 (캡처) 중 Chrome 을 클릭 아닌 곳으로 포커스 이동 / 다른 앱 클릭 금지. Chrome 이 maximized 라 다른 앱이 뒤에 가려있는 건 OK — 클릭으로 포그라운드만 바꾸지 않으면 됨. 외부 모니터 있으면 `--display 2` 로 본인 모니터 자유. Step 2(메타)·Step 4(엑셀)은 백그라운드라 다른 작업 OK.
8. **브라우저 유지** — `run.py` 가 Chrome 1회 launch 후 4단계 끝까지 유지. 단계 사이에서 절대 close 금지. 각 스텝 스크립트는 `--cdp` 로 attach.
9. **Chrome 절대 죽이지 마 (pkill·강제종료 금지)** — `pkill -f run.py` / `pkill -f ig_capture` / Chrome 강제 종료 모두 금지. Chrome 이 dirty shutdown 되면 chrome_session 의 `profile.exit_type=Crashed` 가 남고, 다음 launch 시 **"예기치 못하게 종료되었습니다 / 복원" 배너가 풀스크린 캡처에 박힘** → 법률 자료 무결성 침해. `run.py` 의 `sanitize_chrome_session()` 이 launch 직전 자동 정리하고 `--disable-session-crashed-bubble` 도 걸지만 100% 보장 아님. 옵션 변경/패치는 **작업 시작 *전*에 끝낼 것**. 부득이 중단 필요 시 Ctrl-C (SIGINT) 로만 — asyncio 가 `finally: ctx.close()` 에 도달.
10. **프로필 캡처는 별도 탭** — IG·YT 둘 다 댓글 캡처 page 와 별개의 `prof_page = ctx.new_page()` 에서 navigate. 댓글 페이지가 reload/lazy load 손실 안 입게.

## Pitfalls — 모르면 시간 낭비

**IG**:
- `media/info.comment_count` (UI) ≠ `comments.comment_count` (IG-only) → 차이 = `fb_comment_count` (Facebook 크로스포스트). FB 댓글은 IG API 로 수집 불가능.
- `has_more_comments: false` 가 init 부터 자주 — forward 가 비어있다는 뜻일 뿐, `min_id` 방향(headload) 페이지네이션은 따로 필요.
- "row not found" 캡처 실패 = mention 링크 / 삭제 계정.

**YouTube**:
- Shorts 별도 endpoint 없음. `/watch?v=` 와 `/shorts/` 모두 같은 `youtubei/v1/next`. 메타는 `/watch?v=` 로 수집(ytcfg 안정), 캡처는 `/shorts/` UI 로.
- 답글 단일 토큰 chain → 30%+ 누락. 큐 기반(응답 안 모든 새 토큰을 큐에 push, depth-first follow) 필수.
- `appendContinuationItemsAction` (Action 임! Command 아님) — 답글/추가 페이지 응답.
- `?lc={comment_id}` 딥링크로 댓글 패널 자동 오픈 안 됨. 댓글 버튼 직접 클릭.
- 화면 카운트 vs 응답 합 1-3% 차이 정상 — held for review/spam 댓글 모더레이션.
- `commentRenderer` 아님 — `commentViewModel` + `frameworkUpdates.entityBatchUpdate.mutations[].payload.commentEntityPayload` 사용.

## Stop Signals (즉시 중단)

| 플랫폼 | 신호 | 대응 |
|---|---|---|
| IG | "Please wait a few minutes" / "잠시 후 다시 시도" / `/accounts/login` 또는 `/challenge/` redirect | 자동 재시도 X. 1-2시간 대기 후 재개 |
| YT | INNERTUBE_API_KEY 추출 실패 / 로그인 wall (드물지만 가능) | 동일 |

## Anti-patterns

- 헤드리스 모드 (사용자가 IG 로그인 시각 확인 필요)
- 자격증명 저장 / 자동 로그인
- 엑셀 수동 작성 (반드시 `build_excel.py` 통과)
- 사용자 결과 폴더 외 수정
- IG 차단 메시지 후 즉시 재시도
- YT 답글 단일 토큰 chain
- **`pkill` / 강제 종료로 작업 중단** (Critical Rule 9 참조 — 다음 캡처에 Chrome 복원 배너 박힘)
- **작업 도중 옵션 변경 / 스크립트 패치** (반드시 시작 전에 plan + 패치 완료 → 한 번 launch 로 끝까지)
- IG 프로필 캡처를 댓글 캡처 page 에서 navigate (댓글 페이지 lazy load 손실)

## References

- `references/instagram-api.md` — IG 사설 API 엔드포인트, 양방향 cursor, 응답 필드
- `references/youtube-api.md` — youtubei/v1/next, 큐 기반 토큰, commentEntityPayload, 답글 펼치기
- `references/excel-format.md` — 11컬럼 엑셀 스펙 (`build_excel.py` 와 동기화)
