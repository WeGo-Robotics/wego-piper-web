"""프레임 하나짜리 요청 대신 **한 연결로 계속 밀어주는** MJPEG 스트림.

예전에는 프리뷰가 `<img src="...?t=<시각>">` 를 200ms 마다 새로 걸었다. 서버가
느려서 끊긴 게 아니다 — 한 장 내주는 데 1.2ms 였고 기계는 놀고 있었다.
끊겨 보인 이유는 **초당 5장이 설계값이었기 때문**이고, 거기에 요청마다 간격이
출렁이는 지터가 얹혔다.

폴링 주기를 올리는 것으로는 못 고친다. HTTP/1.1 은 오리진당 연결이 6개뿐이라,
프레임 요청을 늘리면 그 경합이 심해진다 — 그 경합이 E-stop heartbeat 를 굶겨
녹화를 죽인 적이 있다. **화면을 부드럽게 하려다 안전 장치를 조이게 된다.**

그래서 연결을 카메라당 하나로 줄인다. `<img>` 가 `multipart/x-mixed-replace` 를
네이티브로 재생하므로 프론트는 `src` 만 바뀐다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from fastapi import Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

_BOUNDARY = "piperframe"
_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={_BOUNDARY}"

# 스트림당 상한. 발행자가 더 빨라도 이 이상으로는 안 보낸다 —
# 브라우저가 그릴 수 있는 것보다 많이 보내면 대역폭만 쓰고 화면은 그대로다.
DEFAULT_FPS = 15.0

# ⚠ **동시 스트림 상한.** 하나가 연결을 계속 쥐고 있으므로, 탭을 여러 개 열어두면
#   그만큼 쌓인다. 무제한이면 잊고 열어둔 탭들이 조용히 자원을 먹는다.
MAX_STREAMS = 12

# 첫 프레임을 기다리는 상한. 그 뒤로는 안 센다 — 위 주석 참고.
FIRST_FRAME_TIMEOUT_S = 15.0
_open = 0


def _part(jpeg: bytes) -> bytes:
    return (f"--{_BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
            f"Content-Length: {len(jpeg)}\r\n\r\n").encode() + jpeg + b"\r\n"


async def _pump(next_frame: Callable[[], bytes | None], fps: float,
                label: str) -> AsyncIterator[bytes]:
    """`next_frame()` 이 **새 프레임일 때만** 바이트를 돌려준다는 약속이다.

    같은 프레임을 다시 보내면 대역폭만 쓰고 화면은 안 바뀐다. 판정은 프레임을
    가져오는 쪽이 한다 — 세그먼트는 `seq`, 버스는 바이트 비교로 서로 다르다.
    """
    global _open
    interval = 1.0 / max(fps, 0.1)
    _open += 1
    waited = 0.0
    seen = False
    try:
        while True:
            try:
                jpeg = await asyncio.to_thread(next_frame)
            except Exception as exc:
                logger.info("스트림 종료 (%s): %s", label, exc)
                return
            if jpeg:
                seen, waited = True, 0.0
                yield _part(jpeg)
            else:
                waited += interval
                # ⚠ 규칙이 **첫 프레임 전후로 다르다.**
                #
                #   아직 한 장도 못 받았다 → 없는 카메라일 수 있다. 오래 붙들면
                #     동시 스트림 자리만 먹는다. 접는다.
                #   한 번이라도 받았다 → 실재하는 카메라가 잠깐 내려간 것이다.
                #     여기서 접으면 화면이 얼어붙고 스스로 안 돌아온다.
                if not seen and waited >= FIRST_FRAME_TIMEOUT_S:
                    logger.info("스트림 종료 (%s): 첫 프레임이 오지 않는다", label)
                    return
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        # 브라우저가 탭을 닫으면 여기로 온다 — 정상 종료다
        raise
    finally:
        _open -= 1


def stream(next_frame: Callable[[], bytes | None], *, label: str,
           fps: float = DEFAULT_FPS) -> Response:
    """MJPEG 응답. 상한을 넘으면 503 — 조용히 늘어나게 두지 않는다."""
    if _open >= MAX_STREAMS:
        return Response(f"동시 스트림 상한({MAX_STREAMS})에 도달했습니다", status_code=503)
    return StreamingResponse(_pump(next_frame, fps, label), media_type=_CONTENT_TYPE)


def open_streams() -> int:
    return _open
