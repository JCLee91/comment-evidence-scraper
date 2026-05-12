"""크로스플랫폼 전체 화면 캡처 (macOS + Windows + Linux).

법률 증거자료용 — 시스템 메뉴바/작업표시줄의 시계까지 한 장에 포함되어
타임스탬프 위조 난이도가 올라간다.

브라우저 viewport 가 아닌 OS 레벨로 캡처하므로 캡처 순간 Chrome 이
최상단·foreground 여야 한다. 호출 측에서 `bring_to_front()` 호출 후
짧은 대기를 거친다.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


SETTLE_SEC = 0.3  # bring_to_front 후 안정화 대기 (창 전환 애니메이션)


def _import_mss():
    """mss 를 지연 import — --help 같은 메타 작업에서 import 비용·에러 회피."""
    try:
        import mss
        import mss.tools
        return mss
    except ImportError as e:
        raise ImportError(
            "mss 가 설치되지 않았습니다. 설치:\n"
            "  pip install mss\n"
            "또는 venv 활성화 후:\n"
            "  pip install -r requirements.txt"
        ) from e


async def fullscreen_capture(page, dst_png: Path, monitor: int = 1) -> bool:
    """현재 모니터 전체 화면을 PNG 로 저장.

    Args:
        page: Playwright Page 객체 (Chrome 을 foreground 로 가져옴)
        dst_png: 저장 경로
        monitor: 캡처할 모니터 인덱스. mss.monitors 기준:
                 0=전체 가상 화면 합본, 1=주 모니터, 2,3...=보조 모니터.
                 기본 1 (주 모니터).

    Returns:
        True 면 성공.
    """
    mss = _import_mss()
    dst_png.parent.mkdir(parents=True, exist_ok=True)

    try:
        await page.bring_to_front()
    except Exception:
        pass  # 일부 환경에서 no-op; 다음 단계는 진행
    await asyncio.sleep(SETTLE_SEC)

    with mss.mss() as sct:
        monitors = sct.monitors
        if monitor < 0 or monitor >= len(monitors):
            raise ValueError(
                f"모니터 인덱스 {monitor} 가 범위 밖. "
                f"감지된 모니터: 0~{len(monitors)-1} (0=전체합본). "
                f"감지 결과: {monitors}"
            )
        img = sct.grab(monitors[monitor])
        mss.tools.to_png(img.rgb, img.size, output=str(dst_png))

    return dst_png.exists() and dst_png.stat().st_size > 0


def list_monitors() -> list[dict]:
    """디버깅용 — 시스템이 인식한 모니터 목록 반환."""
    mss = _import_mss()
    with mss.mss() as sct:
        return list(sct.monitors)


if __name__ == "__main__":
    # 모니터 목록 확인
    import json

    print(json.dumps(list_monitors(), indent=2))
