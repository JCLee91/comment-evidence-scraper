"""파이프라인 4단계가 같은 Chrome 인스턴스를 공유하기 위한 헬퍼.

`run.py` 가 Playwright 로 persistent context 를 1회 launch (chrome_session 프로필 +
`--remote-debugging-port`) 한 뒤, 각 스텝 subprocess 는 `--cdp URL` 로
`connect_over_cdp` 어태치만 한다. 스텝 끝나도 Chrome 은 살아있다.

스텝 스크립트는 standalone 으로도 실행 가능해야 하므로 (디버깅·재실행),
`--cdp` 가 없으면 직접 `launch_persistent_context` 하는 fallback 제공.
"""
from __future__ import annotations


async def get_context(p, cdp_url: str | None, user_data_dir: str, **launch_opts):
    """Return (context, owns_context).

    owns_context=True  → caller 가 종료 시 ctx.close() 책임 (직접 launch).
    owns_context=False → 어태치 모드. caller 는 절대 ctx.close() 호출 금지 —
                         shared context 가 깨져 다음 스텝이 실패한다.
                         정리는 `safe_close()` 또는 `await browser.close()` 로.
    """
    if cdp_url:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        if not browser.contexts:
            raise RuntimeError(
                f"CDP 어태치 OK 인데 context 가 없음: {cdp_url}. "
                "run.py 가 persistent context 를 띄웠는지 확인."
            )
        return browser.contexts[0], False
    # Standalone launch — Chrome maximized 로 (메뉴바 시계 보이는 풀스크린 캡처용)
    extra_args = launch_opts.pop("args", [])
    if "--start-maximized" not in extra_args:
        extra_args = ["--start-maximized", *extra_args]
    launch_opts["args"] = extra_args
    ctx = await p.chromium.launch_persistent_context(user_data_dir, **launch_opts)
    return ctx, True


async def safe_close(ctx, owns_context: bool) -> None:
    """owns_context=True 일 때만 닫는다. 어태치 모드면 no-op."""
    if owns_context:
        try:
            await ctx.close()
        except Exception:
            pass
