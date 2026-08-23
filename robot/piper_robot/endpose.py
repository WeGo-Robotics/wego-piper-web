"""말단 자세 조그 — **순수 로직** (feature/teleoperation.md §3-C, §4).

## ⚠ 여기가 유일한 방어선이다

관절 명령은 [`safety.filter_goal`](safety.py) 이 범위와 변화율을 붙잡는다.
말단 명령은 **관절을 우리가 안 정한다** — 팔의 온보드 IK 가 정하므로 그 필터가
한 개도 안 걸린다. MIT 모드가 같은 문제를 갖는다고 manual-control §3 이 적었다.

그래서 이 파일이 대신 막는다:

1. **상대 조그만** — 절대 좌표를 받으면 오타 하나가 큰 이동이 된다
2. **한 걸음의 크기 상한** — 눌린 만큼만 간다
3. **작업 공간 상자** — 목표가 밖이면 **보내지 않는다**

`filter_goal` 과 같은 규율로 쓴다: 하드웨어 없이 부를 수 있고 부작용이 없다.
"""

from __future__ import annotations

from dataclasses import dataclass

# SDK 단위. `EndPoseCtrl` 도 `GetArmEndPoseMsgs` 도 같은 단위를 쓴다.
UM_PER_MM = 1000.0        # 0.001mm
MDEG_PER_DEG = 1000.0     # 0.001도

AXES = ("x", "y", "z", "rx", "ry", "rz")
_LINEAR = ("x", "y", "z")

# 한 걸음의 상한. 버튼을 누르는 것이라 이보다 크게 갈 이유가 없다.
MAX_STEP_MM = 20.0
MAX_STEP_DEG = 10.0


@dataclass(frozen=True)
class WorkspaceBox:
    """말단이 있어도 되는 상자(mm). **좁게 시작한다.**

    ⚠ 팔 컨트롤러가 자체 한계를 갖는다고 **가정하지 않는다.** 실측 전까지는
    좁게 두고, 브링업에서 넓힌다 — 반대로 하면 처음 눌러보는 순간이 실험이 된다.
    """

    x: tuple[float, float] = (100.0, 500.0)
    y: tuple[float, float] = (-300.0, 300.0)
    z: tuple[float, float] = (50.0, 500.0)

    def contains(self, x_mm: float, y_mm: float, z_mm: float) -> tuple[bool, str]:
        for name, value, (lo, hi) in self._axes(x_mm, y_mm, z_mm):
            if not (lo <= value <= hi):
                return False, f"{name} {value:.0f}mm 가 작업 공간({lo:.0f}~{hi:.0f}) 밖입니다"
        return True, "OK"

    def _axes(self, x_mm: float, y_mm: float, z_mm: float):
        return (("X", x_mm, self.x), ("Y", y_mm, self.y), ("Z", z_mm, self.z))

    def excursion(self, x_mm: float, y_mm: float, z_mm: float) -> float:
        """상자 밖으로 얼마나 나가 있나(mm). 안이면 0."""
        out = 0.0
        for _, value, (lo, hi) in self._axes(x_mm, y_mm, z_mm):
            out += max(0.0, lo - value) + max(0.0, value - hi)
        return out

    def to_dict(self) -> dict:
        return {"x": list(self.x), "y": list(self.y), "z": list(self.z)}


def clamp_step(axis: str, delta: float) -> float:
    """한 걸음의 크기를 상한에 맞춘다. 축 이름이 이상하면 예외."""
    if axis not in AXES:
        raise ValueError(f"모르는 축입니다: {axis}")
    limit = MAX_STEP_MM if axis in _LINEAR else MAX_STEP_DEG
    return max(-limit, min(float(delta), limit))


def step_target(current: dict[str, int], axis: str, delta: float,
                box: WorkspaceBox) -> tuple[dict[str, int] | None, str]:
    """지금 자세에서 한 걸음. `(목표, 사유)` — 목표가 None 이면 **보내지 않는다.**

    `current` 와 반환값은 **SDK 단위**(0.001mm / 0.001도)다. 화면과 주고받는
    mm/도 변환은 호출부가 한다 — 단위를 두 번 바꾸는 실수를 한 곳으로 모은다.
    """
    missing = [a for a in AXES if a not in current]
    if missing:
        return None, f"지금 자세를 모릅니다: {missing}"

    step = clamp_step(axis, delta)
    if step == 0:
        return None, "이동량이 0 입니다"

    scale = UM_PER_MM if axis in _LINEAR else MDEG_PER_DEG
    target = dict(current)
    target[axis] = int(round(current[axis] + step * scale))

    def _mm(pose):
        return (pose["x"] / UM_PER_MM, pose["y"] / UM_PER_MM, pose["z"] / UM_PER_MM)

    ok, why = box.contains(*_mm(target))
    if ok:
        return target, "OK"

    # ⚠ **밖에서 시작했으면 돌아올 길을 막지 않는다.**
    #
    # 목표가 밖이라고 무조건 거절하면, 팔이 상자 밖에 있을 때 **상자로 돌아가는
    # 명령까지 거절**돼 영영 못 빠져나온다. 실기에서 걸렸다 — 파킹 자세가
    # X 55mm 였는데 상자는 100~500 이라 어느 방향도 안 됐다.
    #
    # 그래서 "밖으로 더 나가지 않으면" 허용한다. 클램프가 아니라 **방향 판정**이다 —
    # 사용자가 시킨 곳으로 가되, 나빠지는 쪽만 막는다.
    before = box.excursion(*_mm(current))
    after = box.excursion(*_mm(target))
    if before > 0 and after <= before:
        return target, f"작업 공간 밖이지만 돌아오는 방향입니다 ({before:.0f}→{after:.0f}mm)"
    return None, why


# 시킨 거리의 이만큼은 가야 "갔다"고 본다.
MIN_PROGRESS = 0.5


def reached(before: dict[str, int], target: dict[str, int], now: dict[str, int],
            min_progress: float = MIN_PROGRESS) -> bool:
    """명령한 만큼 갔나 — **시킨 거리에 견줘서** 본다.

    ⚠ **회귀. 절대 허용 오차를 쓰면 안 된다.** 처음엔 5mm 오차를 뒀는데 한 걸음도
    5mm 였다 — 팔이 **전혀 안 움직여도** 오차 안이라 "도달"로 읽혔다. 실기에서
    Z +5mm 를 보내고 자세가 그대로인데 성공으로 보고했다.

    시킨 거리의 절반은 가야 갔다고 본다. 마스터/슬레이브 판별이 쓰는 규칙과 같다.

    안 갔으면 **더 보내면 안 된다** — IK 해가 없는 곳을 계속 밀면 팔이 떨거나
    특이점에서 튄다. 못 가는 방향이라고 사람에게 말해야 한다.
    """
    for axis in AXES:
        commanded = target.get(axis, 0) - before.get(axis, 0)
        if commanded == 0:
            continue
        moved = now.get(axis, 0) - before.get(axis, 0)
        # 부호가 다르면 반대로 간 것이다 — 비율이 음수가 되어 걸린다
        if moved / commanded < min_progress:
            return False
    return True
