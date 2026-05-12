# Excel Output Format Spec

샘플(`assets/sample.xlsx`)에서 측정한 값. **`scripts/build_excel.py`의 상수와 항상 일치해야 함**. 양식이 바뀌면 이 파일과 스크립트만 수정.

## Sheet

- 시트 이름: `증거자료`
- 1엑셀 = 1포스트 (모든 스레드를 세로로 누적)

## Columns

| Col | Letter | Width | Header  | 데이터 형식 |
|-----|--------|-------|---------|-------------|
| 1   | A      | 7     | 번호     | 원댓글에만 1,2,3,... / 대댓글은 빈칸 |
| 2   | B      | 10    | 구분     | `"원댓글"` 또는 `"대댓글"` |
| 3   | C      | 22    | 계정     | username (예: `jein_212`) |
| 4   | D      | 18    | 본명     | 표시명 + 비공개 시 ` 🔒` 접미. 표시명 없으면 빈문자 + ` 🔒` |
| 5   | E      | 70    | 댓글 내용 | 원문 그대로 (개행 포함) |
| 6   | F      | 42    | 댓글 URL | `https://www.instagram.com/p/{POST}/c/{COMMENT_ID}` |
| 7   | G      | 32    | 프로필 URL | `https://www.instagram.com/{username}/` |
| 8   | H      | 20    | 작성 시간 | `YYYY-MM-DD HH:MM:SS` 문자열 (Asia/Seoul) |
| 9   | I      | 9     | 좋아요    | int |
| 10  | J      | 70    | 스크린샷  | **셀 자체는 비움**. 스레드 단위로 셀병합 + 이미지 임베드 |

## Header (row 1)

- 행 높이: 28
- 배경: ARGB `FF3F3F46` (다크그레이)
- 글자: 흰색 `FFFFFFFF`, **굵게**, 가운데 정렬 (horizontal=center, vertical=center)
- 모든 컬럼 동일 스타일

## Data rows

- 행 높이: **137.25** (모든 데이터 행 동일)
- 정렬:
  - E (댓글 내용): `wrap_text=True`, `vertical=top`, `horizontal=left`
  - F, G (URL): `vertical=center`, `horizontal=left`, wrap_text=False (잘려도 됨)
  - 그 외(A,B,C,D,H,I,J): `vertical=center`, `horizontal=center`
- 폰트: 기본 (미지정)
- 테두리: 미지정
- 좋아요(I)는 number 타입으로 저장

## Threads

각 스레드(원댓글 1개 + 대댓글 N개)는:

1. **연속된 행 블록**으로 작성. 첫 행 = 원댓글, 그 다음 N행 = 대댓글들 (시간 오름차순 권장)
2. J 컬럼을 그 블록 범위만큼 **셀병합** (예: 스레드1=J2:J11, 스레드2=J12:J18, ...)
3. 병합된 J 셀의 시작셀(예: J2)에 그 스레드의 PNG들을 **OneCellAnchor**로 세로 누적 임베드
   - PNG 너비를 셀 너비에 맞춤 (대략 500px 고정 권장)
   - 여러 장이면 첫 PNG는 셀 상단부터, 다음은 그 아래로 픽셀 오프셋 누적
   - 이미지 총 높이가 병합 영역을 넘어도 그대로 둠 (인스타 캡처가 길면 길수록 셀 영역 밖으로 흘러나오는 게 정상)

## 파일명/위치

- 출력 경로: `output/{POST_ID}/result.xlsx`
- 스크린샷 원본: `output/{POST_ID}/{i}번스레드/01.png`, `02.png`, ... (한국어 폴더명 그대로)
- POST_ID는 URL의 `/p/{ID}/` 또는 `/reel/{ID}/`에서 추출 (쿼리스트링 제거)
