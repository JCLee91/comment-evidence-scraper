---
name: comment-evidence-scraper
description: 인스타그램 게시물 또는 유튜브 영상/Shorts URL 1개에서 모든 댓글·답글과 작성자 프로필을 캡처하여 법률 증거자료용 폴더(한글명+날짜+위계표기)와 11컬럼 엑셀로 산출. URL 만 주면 IG/YT 자동 분기. 트리거 — `instagram.com/p/...` `/reel/...` 또는 `youtube.com/shorts/...` `/watch?v=...` `youtu.be/...` URL 과 함께 "댓글 수집", "댓글 증거자료", "댓글 스크린샷", "댓글 캡처", "엑셀로 정리", "scrape comments" 같은 요청.
---

# Comment Evidence Scraper (IG + YouTube)

URL 분기 + 4단계 파이프라인 + chain of custody. 디지털 비친화 사용자(원장님 등)에게 폴더 통째로 zip 전달 가능.

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
- `--limit N` (YT root N개 — 빠른 검증)
- `--no-replies` (답글 스킵 — 빠른 검증)
- `--skip-capture` (메타만)
- `--old-xlsx PATH` (IG 전용 — 이전 xlsx 메타 재사용)

> ⚠️ **법률 자료는 `--limit` / `--no-replies` / `--skip-capture` 금지.** 이 옵션들은 sanity check 전용. 부분 결과물을 절대 zip 패키징해서 전달하지 말 것. 산출물은 항상 **전수 수집 완료본** 1개만.

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

## References

- `references/instagram-api.md` — IG 사설 API 엔드포인트, 양방향 cursor, 응답 필드
- `references/youtube-api.md` — youtubei/v1/next, 큐 기반 토큰, commentEntityPayload, 답글 펼치기
- `references/excel-format.md` — 11컬럼 엑셀 스펙 (`build_excel.py` 와 동기화)
