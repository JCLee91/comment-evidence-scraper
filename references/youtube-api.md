# YouTube Internal API — Comments

## Endpoints

| Purpose | Method | Path | Body |
|---|---|---|---|
| 댓글 목록 + 답글 목록 | POST | `/youtubei/v1/next?key={api_key}&prettyPrint=false` | `{context, continuation}` |

**Base**: `https://www.youtube.com`

`/shorts/{id}` 와 `/watch?v={id}` 둘 다 동일한 endpoint 사용. 별도 shorts endpoint **없음**.

## ytcfg + 초기 토큰 추출

`/watch?v={id}` 페이지 navigate 후 (shorts 직접 navigate 시 ytInitialData 구조 다름):

```javascript
// in-page eval
() => ({
    api_key: ytcfg.data_.INNERTUBE_API_KEY,
    client_name: ytcfg.data_.INNERTUBE_CLIENT_NAME,    // "WEB"
    client_version: ytcfg.data_.INNERTUBE_CLIENT_VERSION,
    context: ytcfg.data_.INNERTUBE_CONTEXT,
})
```

댓글 로딩 트리거 위해 8회 정도 mouse wheel scroll 필요 (lazy load). 그 후 ytInitialData walk:

```javascript
// engagementPanels 내부에서 첫 continuationItemRenderer.continuationEndpoint.continuationCommand.token 추출
```

## 응답 구조 (key paths)

```jsonc
{
  "onResponseReceivedEndpoints": [
    {
      "reloadContinuationItemsCommand": {        // 첫 호출
        "continuationItems": [...]
      }
    },
    {
      "appendContinuationItemsAction": {          // ★ 이후 호출 / 답글 (Action 임! Command 아님)
        "continuationItems": [...]
      }
    }
  ],
  "frameworkUpdates": {
    "entityBatchUpdate": {
      "mutations": [
        {"payload": {"commentEntityPayload": {...}}}
      ]
    }
  }
}
```

`continuationItems[]` = `commentThreadRenderer` × N + 끝에 `continuationItemRenderer` (다음 토큰).

`commentEntityPayload` = 실제 댓글 데이터 (commentId, content, author, toolbar 등).

## 토큰 위치 두 가지 패턴

```javascript
// 패턴 1: root 페이지네이션
continuationItemRenderer.continuationEndpoint.continuationCommand.token

// 패턴 2: "답글 더보기" 버튼
continuationItemRenderer.button.buttonRenderer.command.continuationCommand.token
```

답글 첫 토큰 위치: `commentThreadRenderer.replies.commentRepliesRenderer.subThreads[0].continuationItemRenderer.continuationEndpoint.continuationCommand.token`. `subThreads` 또는 `contents` 배열 안에 있음 (양쪽 모두 검사 필요).

## 답글 수집 — 큐 기반 (핵심)

**단일 토큰 chain 으로는 누락**. 응답 어디서든 발견되는 모든 새 토큰을 큐에 넣고 모두 follow:

```python
queue = [initial_reply_token]
seen = set(queue)
while queue:
    tok = queue.pop(0)
    res = post_next(api_key, context, tok)
    # mutations 의 모든 cep 를 reply 로 저장
    for cid, cep in index_entity_payloads(res).items():
        if cid != parent_cid:
            replies[cid] = build_record(cep, video_id, parent_cid)
    # 응답 트리 어디서든 새 continuation 토큰 찾기
    found = []
    collect_all_continuation_tokens(res, found)
    for t in found:
        if t not in seen:
            seen.add(t); queue.append(t)
```

`collect_all_continuation_tokens` 는 응답 dict 재귀 탐색:
- `continuationEndpoint.continuationCommand.token`
- `buttonRenderer.command.continuationCommand.token`

## commentEntityPayload 필드 (관련만)

```jsonc
{
  "key": "...",
  "properties": {
    "commentId": "Ugyw8YLHWETfQ2i6G394AaABAg",  // root: 단일, reply: "{root_cid}.{reply_cid}"
    "content": {"content": "댓글 내용"},
    "publishedTime": "3일 전",                   // ★ 상대 표기만 — 절대 timestamp 노출 안 됨
    "replyLevel": 0                              // 0=root, 1=reply, 2+=nested reply
  },
  "author": {
    "channelId": "UC...",
    "displayName": "@handle-xxx",
    "canonicalBaseUrl": "/@handle-xxx",
    "avatarThumbnailUrl": "..."
  },
  "toolbar": {
    "likeCountNotliked": "5.5천",                // ★ display string ("5.5천", "1.2만")
    "replyCount": "22"
  }
}
```

URL 생성:
- `https://www.youtube.com/shorts/{video_id}?lc={comment_id}` ← 댓글 deep link (캡처용)
- `https://www.youtube.com{canonicalBaseUrl}` ← 채널 페이지 (프로필 캡처용)

`?lc=` 파라미터는 댓글 패널을 자동으로 열어주지 **않음** — 캡처 시 댓글 버튼 직접 클릭 필요.

## 캡처 (DOM 매칭)

`/shorts/{id}` navigate → `button[aria-label*="댓글"]` 클릭 → 패널 오픈.

각 댓글 매칭:
```javascript
document.querySelector(`ytd-comment-thread-renderer a[href*="lc=${cid}"]`)
```

찾으면 `closest('ytd-comment-thread-renderer').scrollIntoView({block: 'start'})` → `page.screenshot()`.

답글 캡처 전에 root 의 "답글 N개 보기" + "답글 더보기" 버튼 모두 클릭 (최대 15회) — 그래야 답글 cid 가 DOM 에 들어옴.

## Counts 의미 (debugging 핵심)

| 표시 | 의미 |
|---|---|
| 댓글 패널 헤더 "댓글 5,014개" | UI 표시 카운트 (held for review / spam 포함) |
| 응답 mutations 의 cep 합 | 실제 수집 가능량 (모더레이션된 댓글은 응답에 안 옴) |

차이 원인:
- "Held for review" / "Likely spam" → 카운트엔 들어가지만 응답엔 없음
- 채널 차단 댓글 → 비공개 응답 사용자 입장에서 안 보임
- 우리 수집 = (응답 cep 합) ≈ UI count - (모더레이션 약 1-3%)

## 시도해봤는데 무용한 것들

- `?lc={comment_id}` 딥링크로 패널 자동 오픈 — 안 됨
- `/shorts/{id}` 직접 navigate 후 ytcfg 추출 — INNERTUBE_API_KEY 안 들어있을 수 있음. `/watch?v={id}` 가 안정적
- 단일 토큰 chain 답글 페이지네이션 — 30%+ 누락. 큐 기반 필수
- `commentRenderer` 만 파싱 — 새 YouTube 는 `commentViewModel` + `commentEntityPayload` 사용. 둘 다 매핑

## 함정

- **응답에 `commentRenderer` 없음** — 새 포맷은 `commentViewModel` (껍데기) + mutations 의 `commentEntityPayload` (실제 데이터). `commentViewModel.commentKey` 로 mutations 매핑.
- **답글 응답은 `appendContinuationItemsAction`** (Action 임! Command 아님). 이름 다름.
- **publishedTime 은 상대 표기만** ("3일 전"). 수집 시각(`collected_at`) 함께 progress.json 에 박아서 환산 가능하게.
- **YouTube 모더레이션 카운트**: 화면 카운트 vs 우리 수집 1-3% 차이 정상.

## 참고

- yt-dlp 의 `_extract_comment_replies` (commit d22436e) — 답글 토큰 처리 검증 구현
- youtube-comment-downloader (egbertbouman) — 큐 기반 단순 패턴
