"""팔 상태 — `/dev/shm/piper.arm.<iface>.state` 구독 (feature/plugins.md §7-5). **읽기만 한다.**

세그먼트는 robotd(실물)·simd(시뮬)가 발행하고 다중 소비자다 — 추론·녹화와 충돌이 없다.
팔을 움직이는 길은 여전히 하나, 추론(`self.robot.*` REST)이다. 이 모듈은 명령 세그먼트를
열지 않는다.

값은 **정규화된 값**이다 — 관절 `-100..100`, 그리퍼 `0..100`(행정 비율). 데이터셋의
`observation.state` 와 같은 눈금이라 정책이 보는 숫자와 플러그인이 보는 숫자가 같다.
캘리브레이션의 소유자는 robotd 하나다(`piper_shm.arm` 머리) — 여기서 도(°)로 되돌리지 않는다.

발행자가 재시작하면 세그먼트가 사라졌다가 다시 생긴다. 옛 mmap 은 계속 읽히고 **마지막 자세가
영원히** 나온다(`StateReader.orphaned`). 여기서 그걸 감지해 다시 연다 — 카메라와 같은 처리다.
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass

# 말단 자세의 SDK 단위 — `piper_robot.endpose` 의 UM_PER_MM·MDEG_PER_DEG 와 같다
_SDK_PER_UNIT = 1000.0


@dataclass(frozen=True)
class ArmState:
    #: `{joint1..joint6, gripper}` — 정규화 값 (관절 -100..100, 그리퍼 0..100)
    joints: dict[str, float]
    err_code: int
    ctrl_mode: int
    seq: int
    #: robotd 가 이 값을 CAN 에서 받은 시각 — 상태의 신선도는 이것으로 잰다
    can_wall_ns: int

    @property
    def gripper(self) -> float:
        """그리퍼 열림 0(닫힘)..100(끝까지 열림)."""
        return self.joints.get("gripper", 0.0)

    @property
    def age_s(self) -> float:
        """이 상태가 몇 초 전 것인가. 팔이 멈춰도 robotd 는 계속 발행하므로 보통 수 ms 다."""
        if not self.can_wall_ns:
            return float("inf")
        return max(0.0, (time.time_ns() - self.can_wall_ns) / 1e9)

    def fresh(self, max_age_s: float = 0.5) -> bool:
        return self.age_s <= max_age_s


class Arm:
    def __init__(self, iface: str, api=None) -> None:
        self.iface = iface
        self._api = api
        self._reader = None
        self._seq = -1

    def _open(self) -> bool:
        from piper_shm.arm import StateReader

        try:
            self._reader = StateReader(self.iface)
            self._seq = -1
            return True
        except Exception:
            self._reader = None
            return False

    def _raw(self) -> dict | None:
        if self._reader is None and not self._open():
            return None
        # getattr — `orphaned` 가 없는 옛 piper_shm 과 짝지어져도 읽기는 돈다(재시작 추종만 빠진다)
        if getattr(self._reader, "orphaned", False):
            self.close()
            if not self._open():
                return None
        try:
            return self._reader.read()
        except Exception:
            self.close()
            return None

    @staticmethod
    def _state(raw: dict) -> ArmState:
        return ArmState(joints=dict(raw["values"]), err_code=int(raw["err_code"]),
                        ctrl_mode=int(raw["ctrl_mode"]), seq=int(raw["seq"]),
                        can_wall_ns=int(raw["can_wall_ns"]))

    def read(self) -> ArmState | None:
        """지금 세그먼트에 있는 상태 — 같은 것이어도 돌려준다. 세그먼트가 없거나 아직 비었으면 None."""
        raw = self._raw()
        return None if raw is None else self._state(raw)

    def read_new(self, timeout_s: float = 1.0, poll_s: float = 0.002) -> ArmState | None:
        """**새** 상태가 올 때까지 기다린다. 타임아웃·세그먼트 없음이면 None."""
        deadline = time.monotonic() + timeout_s
        while True:
            raw = self._raw()
            if raw is not None and raw["seq"] != self._seq:
                self._seq = raw["seq"]
                return self._state(raw)
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_s)

    @property
    def connected(self) -> bool:
        return self.read() is not None

    def end_pose(self) -> dict | None:
        """말단 자세 — `{x, y, z (mm), rx, ry, rz (도), inside, box}`. 범위 `robot.read`.

        팔 컨트롤러가 계산한 값을 게이트웨이(robotd RPC)로 묻는다 — **REST 한 번**이라 회차 경계·
        저속 폴링에 쓴다(제어 루프 속도로 부르지 않는다). `inside` 는 조그가 쓰는 작업 공간 상자
        (`box`, mm) 안인가. 모르면(범위 없음·팔 응답 없음) None.
        """
        if self._api is None:
            return None
        r = self._api.get(f"/api/robots/end-pose/{self.iface}")
        d = r.json() if r.ok else None
        pose = (d or {}).get("pose")
        if not pose:
            return None
        out = {k: float(pose[k]) / _SDK_PER_UNIT for k in ("x", "y", "z", "rx", "ry", "rz")}
        box = d.get("box") or {}
        out["box"] = box
        out["inside"] = all(lo <= out[a] <= hi for a, (lo, hi) in box.items() if a in ("x", "y", "z"))
        return out

    def close(self) -> None:
        if self._reader is not None:
            with contextlib.suppress(Exception):
                self._reader.close()
        self._reader = None
