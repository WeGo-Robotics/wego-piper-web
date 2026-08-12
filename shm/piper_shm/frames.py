"""seqlock 기반 프레임 발행/구독.

라이터는 락을 잡지 않는다 — **캡처 루프를 절대 멈추지 않는 것**이 요점이다.
리더가 느리면 리더가 재시도하지, 라이터가 기다리지 않는다.

```
writer:  slot[(seq+1) % N] 에 기록  →  write_seq = seq+1
reader:  s1 = write_seq
         slot[s1 % N] 를 자기 버퍼로 복사
         s2 = write_seq
         if s2 - s1 >= N-1:  재시도      # 라이터가 한 바퀴 돌아 덮었을 수 있다
```

⚠ x86-64 는 TSO 라 "기록 → seq 증가" 가 재배열되지 않는다.
**ARM 으로 옮기면 명시적 배리어가 필요하다** (refactor/camera-transport.md).
"""

from __future__ import annotations

import struct
import time

import numpy as np

from piper_shm import segment as S

# 헤더 안에서 자주 갱신되는 두 필드의 오프셋.
# 매 프레임 헤더 전체를 다시 쓰면 레이아웃까지 건드려 리더가 찢어진 헤더를 볼 수 있다.
_SEQ_OFF = struct.calcsize("<IIIIIIIQ")      # write_seq 앞까지
_WALL_OFF = _SEQ_OFF + 8

_DTYPE_NP = {S.DTYPE_UINT8: np.uint8}


class Publisher:
    """카메라 한 대의 프레임을 세그먼트에 흘린다.

    지금은 게이트웨이의 `camera_manager` 가 이걸 쓴다 — camerad 를 분리하기 **전에**
    전송 계층만 검증하기 위해서다 (refactor/camera-transport.md 착수 순서 2단계).
    나중에 이 클래스의 사용처만 camerad 로 옮기면 된다.
    """

    def __init__(self, name: str, width: int, height: int, channels: int = 3,
                 n_slots: int = S.DEFAULT_SLOTS) -> None:
        self.name = name
        self.layout = S.Layout(width=width, height=height, channels=channels,
                               n_slots=n_slots)
        self._fd, self._buf = S.create(name, self.layout)
        self._seq = 0
        self._slots = [
            np.frombuffer(
                self._buf, dtype=_DTYPE_NP[self.layout.dtype],
                count=width * height * channels,
                offset=S.HEADER_SIZE + i * self.layout.slot_bytes,
            ).reshape(height, width, channels)
            for i in range(n_slots)
        ]

    def publish(self, frame: np.ndarray, wall_ns: int | None = None) -> int:
        """프레임 하나 발행. 반환값은 새 `write_seq`.

        모양이 다르면 **조용히 리사이즈하지 않는다** — 정책 입력 크기가 말없이
        바뀌는 것보다 시끄럽게 실패하는 편이 낫다.
        """
        expected = (self.layout.height, self.layout.width, self.layout.channels)
        if frame.shape != expected:
            raise ValueError(f"프레임 모양이 다릅니다: {frame.shape} (기대 {expected})")

        nxt = self._seq + 1
        self._slots[nxt % self.layout.n_slots][:] = frame      # memcpy 1회
        # 슬롯 기록이 먼저, seq 증가가 나중 (x86-64 TSO)
        self._buf[_SEQ_OFF:_SEQ_OFF + 8] = struct.pack("<Q", nxt)
        self._buf[_WALL_OFF:_WALL_OFF + 8] = struct.pack(
            "<Q", wall_ns if wall_ns is not None else time.time_ns()
        )
        self._seq = nxt
        return nxt

    def close(self, unlink: bool = True) -> None:
        """⚠ `unlink=False` 로 두면 `/dev/shm` 에 누수가 남는다."""
        self._slots.clear()
        try:
            self._buf.close()
        finally:
            import os

            os.close(self._fd)
            if unlink:
                S.unlink(self.name)


class Subscriber:
    """세그먼트에서 최신 프레임을 읽는다. LeRobot 플러그인이 쓴다."""

    def __init__(self, name: str, max_retries: int = 8) -> None:
        self.name = name
        self.max_retries = max_retries
        self._fd, self._buf, self.layout = S.open_ro(name)
        self._last_seq = 0
        self.retries = 0        # 진단용 — 비정상적으로 높으면 라이터가 너무 빠르다

    @property
    def shape(self) -> tuple[int, int, int]:
        return (self.layout.height, self.layout.width, self.layout.channels)

    def _seq(self) -> int:
        return struct.unpack("<Q", self._buf[_SEQ_OFF:_SEQ_OFF + 8])[0]

    def wall_ns(self) -> int:
        return struct.unpack("<Q", self._buf[_WALL_OFF:_WALL_OFF + 8])[0]

    def read(self) -> tuple[np.ndarray, int, int] | None:
        """(프레임 복사본, seq, wall_ns). 아직 프레임이 없으면 `None`.

        복사가 필요한 이유: 복사 없이 슬롯을 그대로 넘기면 라이터가 그 위에 덮어써
        **정책이 반쯤 갈린 프레임을 본다.**
        """
        n = self.layout.n_slots
        for _ in range(self.max_retries):
            s1 = self._seq()
            if s1 == 0:
                return None
            off = S.HEADER_SIZE + (s1 % n) * self.layout.slot_bytes
            frame = np.frombuffer(
                self._buf, dtype=_DTYPE_NP[self.layout.dtype],
                count=self.layout.slot_bytes, offset=off,
            ).reshape(self.shape).copy()
            wall = self.wall_ns()
            if self._seq() - s1 < n - 1:        # 라이터가 아직 안 덮었다
                self._last_seq = s1
                return frame, s1, wall
            self.retries += 1
        return None

    def read_new(self, timeout_s: float = 1.0, poll_s: float = 0.001):
        """**새** 프레임을 기다렸다 읽는다.

        알림은 `write_seq` 폴링 + 1ms sleep 이다. 30fps 에 최대 1ms 추가 지연이고
        CPU 는 사실상 0 — eventfd 는 이 지연이 문제가 될 때 생각한다.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._seq() > self._last_seq:
                got = self.read()
                if got is not None:
                    return got
            time.sleep(poll_s)
        return None

    def close(self) -> None:
        try:
            self._buf.close()
        finally:
            import os

            os.close(self._fd)
