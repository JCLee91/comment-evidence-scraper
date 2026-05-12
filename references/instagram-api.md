# Instagram Private API — Comments

## Endpoints

| Purpose | Method | Path |
|---|---|---|
| 댓글 카운트 (UI 일치) | GET | `/api/v1/media/{media_id}/info/` |
| Root 댓글 페이지 | GET | `/api/v1/media/{media_id}/comments/?can_support_threading=true` |
| 답글 페이지 | GET | `/api/v1/media/{media_id}/comments/{comment_id}/child_comments/` |

**Base**: `https://www.instagram.com`

## 필수 헤더

```
X-IG-App-ID: 936619743392459
X-CSRFToken: <csrftoken cookie>
X-Requested-With: XMLHttpRequest
```

`credentials: 'include'` (in-page fetch) 또는 sessionid + csrftoken 쿠키 동봉. App ID 는 인스타 웹 공식 값 (변경 거의 없음).

## shortcode → media_id

```python
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
def shortcode_to_media_id(s):
    m = 0
    for c in s:
        m = m * 64 + ALPHABET.index(c)
    return str(m)
```

`/p/DXtpZ69k4hu/` → `3885944154694584430`

## Root comments — 양방향 cursor (핵심)

응답 구조:
```json
{
  "comment_count": 335,
  "comments": [...],            // 최대 15개씩
  "has_more_comments": true,    // forward (older) 더 있음
  "has_more_headload_comments": true,  // backward (newer) 더 있음
  "next_min_id": "...",         // headload cursor
  "next_max_id": "...",         // forward cursor
  "is_ranked": true,
  "comment_filter_param": "no_filter"
}
```

**필수: 양방향 모두 소진**

```python
# Forward (older, max_id):
while res["has_more_comments"]:
    res = fetch(f"?can_support_threading=true&max_id={res['next_max_id']}")
    collect(res["comments"])

# Backward (newer, headload, min_id):
while res["has_more_headload_comments"]:
    res = fetch(f"?can_support_threading=true&min_id={res['next_min_id']}")
    collect(res["comments"])
```

한 방향만 처리하면 절반 누락. init 응답에서 `has_more_comments: false` 가 자주 나오는데 이건 정상 — forward 가 비어있다는 뜻이지 끝났다는 게 아님. backward 만 페이지네이션 필요.

**3-4초 랜덤 딜레이** 사이에 삽입 (rate limit).

## Child comments — 양방향 cursor

응답 구조:
```json
{
  "child_comments": [...],
  "has_more_tail_child_comments": true,
  "has_more_head_child_comments": true,
  "next_max_child_cursor": "...",
  "next_min_child_cursor": "..."
}
```

```python
# tail (max):
while res.get("has_more_tail_child_comments"):
    res = fetch(f"/{cid}/child_comments/?max_id={res['next_max_child_cursor']}")

# head (min):
while res.get("has_more_head_child_comments"):
    qs = f"?min_id={cur}" if cur else "?min_id="  # 빈 min_id 첫 호출
    res = fetch(f"/{cid}/child_comments/{qs}")
```

`child_comment_count > 0` 인 root 만 호출 대상.

## Comment 객체 필드 (관련만)

```json
{
  "pk": "17963688203917605",        // comment id (string)
  "text": "댓글 내용",
  "user": {
    "username": "tudorolex_777",
    "full_name": "표시 이름",
    "is_private": false,
    "is_verified": false,
    "profile_pic_url": "..."
  },
  "created_at_utc": 1777654173,     // unix epoch UTC
  "comment_like_count": 2,
  "child_comment_count": 0,
  "content_type": "comment"
}
```

URL 생성: `https://www.instagram.com/p/{shortcode}/c/{pk}` / `https://www.instagram.com/{username}/`

## Counts 의미 (debugging 핵심)

| 값 | 출처 | 의미 |
|---|---|---|
| `media/info` `comment_count` | info endpoint | UI 표시 카운트 (IG + FB 합산) |
| `media/info` `fb_comment_count` | info endpoint | FB 크로스포스트 댓글 수 (인스타 API 로 접근 불가) |
| `comments/` `comment_count` | comments endpoint | IG-only 트리 총량 (root + replies) |

차이 식별:
- `info.comment_count > comments.comment_count` → 차이 = `fb_comment_count`
- `comments.comment_count` 가 우리 수집 상한 (스팸 필터 1-2% 제외)

## 시도해봤는데 무용한 것들 (시간 낭비 방지)

- `?sort_order=recent|oldest|popular` — 동일 카운트, 동일 응답
- `?comment_filter_param=show_all|show_hidden|include_hidden` — 변화 없음
- `?include_fb_comments=true&surface=instagram` — `fb_comments` 항상 0
- GraphQL `PolarisPostCommentsContainerQuery` — execution error (서명 토큰 필요)
- `https://graph.facebook.com/{fbid}/comments` — App ID 필수 (403)
- public `https://www.facebook.com/{fbid}` — 로그인 wall

## In-page fetch pattern (CSP 우회 X, same-origin)

```javascript
async ([url]) => {
    const csrf = document.cookie.split('; ').find(r => r.startsWith('csrftoken='))?.split('=')[1] || '';
    const r = await fetch(url, {
        headers: {
            'X-IG-App-ID': '936619743392459',
            'X-CSRFToken': csrf,
            'X-Requested-With': 'XMLHttpRequest',
        },
        credentials: 'include',
    });
    return await r.json();
}
```

Playwright `page.evaluate` 로 호출. `chrome_session/` 의 sessionid 쿠키 자동 사용.

## block route (안전 장치)

자동화 중 실수로 follow/unfollow 트리거 방지:
```python
async def block_follow(route, request):
    if "/friendships/create/" in request.url or "/friendships/destroy/" in request.url:
        await route.abort()
        return
    await route.continue_()
await ctx.route("**/api/v1/friendships/**", block_follow)
```
