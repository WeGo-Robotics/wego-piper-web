"""카메라 — `/dev/shm` 세그먼트 구독 (feature/plugins.md §0). 카메라를 **열지 않는다.**

세그먼트는 camerad·rsd·simd 가 발행하고 다중 소비자다 — 수집·추론과 충돌이 없다.
프레임은 **BGR** 이다.

발행자가 재시작하면 세그먼트가 잠깐 사라지고 다시 생긴다. 그때 옛 mmap 은 계속 읽히고
같은 프레임만 나온다(`Subscriber.orphaned`). 여기서 그걸 감지해 다시 연다 — 게이트웨이의
`segment_reader` 와 같은 처리다. 플러그인은 그 사정을 몰라도 된다.
"""

from __future__ import annotations

import contextlib
import time


class Camera:
    def __init__(self, stream: str) -> None:
        self.stream = stream
        self._sub = None
        self._seq = -1

    def _open(self) -> bool:
        from piper_shm import Subscriber, segment_for_camera

        try:
            self._sub = Subscriber(segment_for_camera(self.stream))
            self._seq = -1
            return True
        except Exception:
            self._sub = None
            return False

    def _read(self):
        if self._sub is None and not self._open():
            return None
        if self._sub.orphaned:
            self.close()
            return None
        try:
            return self._sub.read()
        except Exception:
            self.close()
            return None

    def latest(self):
        """지금 세그먼트에 있는 프레임(BGR) — 같은 프레임이어도 돌려준다. 없으면 None."""
        got = self._read()
        return None if got is None else got[0]

    def read_new(self, timeout_s: float = 1.0, poll_s: float = 0.005):
        """**새** 프레임이 올 때까지 기다린다. `(frame, seq, wall_ns)` 또는 None(타임아웃·세그먼트 없음)."""
        deadline = time.monotonic() + timeout_s
        while True:
            got = self._read()
            if got is not None and got[1] != self._seq:
                self._seq = got[1]
                return got
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_s)

    @property
    def connected(self) -> bool:
        return self._read() is not None

    def close(self) -> None:
        if self._sub is not None:
            with contextlib.suppress(Exception):
                self._sub.close()
        self._sub = None
