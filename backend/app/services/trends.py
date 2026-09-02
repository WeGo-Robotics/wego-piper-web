"""자원 추이 — 서버가 쌓아 두는 최근 15분.

## 왜 서버가 쌓나

브라우저가 쌓으면 **페이지를 연 순간부터만** 보인다 — 그런데 그래프를 보러
오는 이유는 대개 "아까 무슨 일이 있었나"다. 게이트웨이가 4초마다 견본을 뜨고,
대시보드는 로딩 때 지난 15분을 통째로 받아 처음부터 찬 그래프를 그린다.

메모리에만 든다(게이트웨이 재시작 = 초기화). 225점짜리 링버퍼라 디스크에
남길 물건이 아니고, 재시작 직후는 "지금 괜찮은가"를 새로 재는 게 맞다.

## fps 는 견본을 뜨지 않는다 — 적어 둔 것을 줍는다

추론 텔레메트리는 WS 콜백 스레드로 **밀려 들어온다**(초당 수십 번일 수 있다).
그걸 다 쌓으면 버퍼가 fps 로 도배되니, 텔레메트리가 지나갈 때 마지막 값만
적어 두고(`note_fps`) 샘플러가 4초마다 줍는다. 한동안 새 값이 없으면
추론이 멈춘 것이다 — None 으로 남겨 그래프가 **끊기게** 둔다.
"""

import asyncio
import logging
import time
from collections import deque

from app.services import resources

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 4.0
WINDOW_S = 15 * 60

_samples: deque[dict] = deque(maxlen=int(WINDOW_S / SAMPLE_INTERVAL_S) + 5)
_last_fps: tuple[float, float] | None = None   # (monotonic 시각, fps)
# 마지막 견본의 GPU 전체 행 — /system/resources 가 재사용한다.
# nvidia-smi 는 드라이버가 걸리면 멈추는 물건이라 **호출 횟수 자체가 위험**이다.
# 샘플러가 이미 4초마다 부르는데 대시보드 폴링이 또 부르면 두 배로 찌른다.
_last_gpus: list[dict] = []


def note_fps(fps: float) -> None:
    """추론 텔레메트리가 지나갈 때 부른다(콜백 스레드 — 대입뿐이라 안전)."""
    global _last_fps
    _last_fps = (time.monotonic(), float(fps))


def latest_gpus() -> list[dict]:
    """샘플러가 마지막으로 본 GPU 행. 아직 한 번도 못 떴으면 빈 목록."""
    return _last_gpus


def latest_cpu() -> float | None:
    return _samples[-1]["cpu_pct"] if _samples else None


def samples(since: float | None = None) -> list[dict]:
    """`since`(epoch 초) 이후 견본만. 없으면 창 전체 — 페이지 로딩이 이 경우다."""
    if since is None:
        return list(_samples)
    return [s for s in _samples if s["t"] > since]


def _take_sample() -> dict:
    """스레드에서 돈다 — nvidia-smi 가 멈춰도 이벤트 루프는 산다."""
    global _last_gpus
    gpus = resources.gpus()
    _last_gpus = gpus
    cpu = resources.cpu_pct()
    fps = None
    # 두 주기 넘게 새 텔레메트리가 없으면 멈춘 값이다 — 이어 그리면 거짓말
    if _last_fps and time.monotonic() - _last_fps[0] < SAMPLE_INTERVAL_S * 2:
        fps = round(_last_fps[1], 1)
    return {
        "t": round(time.time(), 1),
        "cpu_pct": cpu,
        "fps": fps,
        "gpus": [{
            "name": g["name"],
            "util_pct": g["util_pct"],
            "mem_pct": round(g["mem_used_mb"] / g["mem_total_mb"] * 100, 1)
                       if g["mem_used_mb"] is not None and g["mem_total_mb"] else None,
        } for g in gpus],
    }


async def run_sampler() -> None:
    """lifespan 이 태스크로 띄운다. 한 번 실패해도 다음 주기는 돈다."""
    while True:
        try:
            _samples.append(await asyncio.to_thread(_take_sample))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("자원 견본 실패 (다음 주기에 재시도): %s", exc)
        await asyncio.sleep(SAMPLE_INTERVAL_S)
