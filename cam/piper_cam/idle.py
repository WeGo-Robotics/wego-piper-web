"""유휴 자동 종료 — 쓰는 곳이 없는 카메라를 일정 시간 뒤 닫는다 (feature/stream-based-cameras.md §5-5).

## 누가 판단하나 — 데몬이다

게이트웨이가 죽어도 동작해야 하고(크래시 격리), 장치를 쥔 쪽이 닫는 게 맞다. 게이트웨이는
카메라별 시간(`set_idle`)과 "시작 요청이 곧 붙는다"(`hold`)를 알려 줄 뿐이다.

## "읽는 곳이 있다"는 어떻게 아나

세그먼트 파일의 공유 락이다 (`piper_shm.has_readers`). 소비자가 열 때 `LOCK_SH` 를 걸고, 여기서
`LOCK_EX|LOCK_NB` 로 떠본다. 커널이 죽은 프로세스의 락을 풀어 주므로 하트비트가 필요 없다.
상시 도는 감시·스냅샷은 `Subscriber(observer=True)` 로 빠진다.

## 안 닫는 쪽으로 틀리게

- 시간은 **하한 30초**다 — 설정 실수로 1초를 줘도 연결 직후에 닫히지 않는다.
- 연결 직후에는 그 시간만큼 유예가 시작된다(`set_idle` 이 불린 시각부터).
- `hold` 는 그 시각까지 닫지 않는다. 시작 요청 → 프로세스 기동 → 정책 로드 → 카메라 연결 사이에는
  수 초~수십 초가 있고, 그동안 읽는 곳이 없어도 닫으면 안 된다.
- 락을 못 걸었거나 모르면 "읽는 곳이 있다"로 본다.
"""

import json
import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

# 설정 실수로 연결 직후에 닫히지 않게 하는 하한. 화면도 같은 값으로 거른다.
MIN_IDLE_S = 30


class IdleReaper:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 has_readers: Callable[[str], bool] | None = None,
                 segment_exists: Callable[[str], bool] | None = None) -> None:
        self._clock = clock
        self._has_readers = has_readers
        self._segment_exists = segment_exists
        # cam_id -> [유휴 허용 초, 마지막으로 읽는 곳이 보인 시각, 이 시각 전엔 안 닫는다]
        self._cams: dict[str, list[float]] = {}
        self._last_tick = 0.0

    # ── 게이트웨이가 정한다 ──

    def set_idle(self, cam_id: str, seconds: int) -> int:
        """0 이면 끈다. 아니면 `MIN_IDLE_S` 이상으로 맞춰 **실제 값**을 돌려준다.

        연결할 때마다 불리므로 유예가 다시 시작된다 — 연결 직후 읽는 곳이 없어도 닫지 않는다.
        """
        try:
            s = int(seconds or 0)
        except (TypeError, ValueError):
            s = 0
        if s <= 0:
            self._cams.pop(cam_id, None)
            return 0
        s = max(MIN_IDLE_S, s)
        now = self._clock()
        self._cams[cam_id] = [float(s), now, 0.0]
        return s

    def hold(self, cam_id: str, seconds: int) -> bool:
        """이 카메라를 `seconds` 동안은 닫지 않는다. 유휴 종료가 꺼져 있으면 할 일이 없다."""
        entry = self._cams.get(cam_id)
        if entry is None:
            return False
        entry[2] = max(entry[2], self._clock() + max(0, float(seconds)))
        return True

    def forget(self, cam_id: str) -> None:
        self._cams.pop(cam_id, None)

    def idle_seconds(self, cam_id: str) -> int:
        entry = self._cams.get(cam_id)
        return int(entry[0]) if entry else 0

    # ── 데몬 루프가 주기적으로 부른다 ──

    def tick(self, stop: Callable[[str], None], segment_of: Callable[[str], str],
             every_s: float = 1.0) -> list[tuple[str, int]]:
        """닫을 것을 닫고 `[(cam_id, 허용 초)]` 를 돌려준다. 호출은 `every_s` 로 솎는다."""
        now = self._clock()
        if now - self._last_tick < every_s:
            return []
        self._last_tick = now

        has_readers = self._has_readers
        exists = self._segment_exists
        if has_readers is None or exists is None:
            from piper_shm import has_readers as _hr, segment_path

            has_readers = has_readers or _hr
            exists = exists or (lambda seg: segment_path(seg).exists())

        stopped: list[tuple[str, int]] = []
        for cam_id, entry in list(self._cams.items()):
            idle_s, last_seen, protected_until = entry
            seg = segment_of(cam_id)
            if not exists(seg):
                continue              # 연결 안 됨 — 닫을 것이 없다 (다음 연결이 유예를 다시 연다)
            try:
                busy = has_readers(seg)
            except Exception:
                busy = True           # 모르면 "있다" — 닫지 않는 쪽이 안전하다
            if busy:
                entry[1] = now
                continue
            if now < protected_until or now - last_seen < idle_s:
                continue
            try:
                stop(cam_id)
            except Exception as exc:
                logger.warning("유휴 종료 실패 (%s): %s", cam_id, exc)
                continue
            logger.info("유휴 종료: %s — %d초 동안 읽는 곳이 없었습니다", cam_id, int(idle_s))
            stopped.append((cam_id, int(idle_s)))
            self._cams.pop(cam_id, None)        # 다음 연결 때 `set_idle` 이 다시 연다
        return stopped


def record_stop(bus, cam_id: str, after_s: int) -> None:
    """"유휴로 종료됨" 을 남긴다. 실패해도 닫는 일을 막지 않는다."""
    from piper_bus import contract as C

    try:
        bus.r.set(C.cam_idle_key(cam_id),
                  json.dumps({"at": time.time(), "after_s": int(after_s)}),
                  ex=C.CAM_IDLE_TTL_S)
    except Exception as exc:
        logger.debug("유휴 종료 기록 실패 (%s): %s", cam_id, exc)


def clear_stop(bus, cam_id: str) -> None:
    from piper_bus import contract as C

    try:
        bus.r.delete(C.cam_idle_key(cam_id))
    except Exception:
        pass


def tick_daemon(bus, stop: Callable[[str], None]) -> None:
    """데몬 루프에서 한 줄로 부른다 — 닫고, 사실을 남긴다."""
    from piper_shm import segment_for_camera

    for cam_id, after_s in reaper.tick(stop, segment_for_camera):
        record_stop(bus, cam_id, after_s)


# 프로세스당 하나 — 데몬마다 자기 카메라만 든다.
reaper = IdleReaper()
