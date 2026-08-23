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
        for name, value, (lo, hi) in (("X", x_mm, self.x), ("Y", y_mm, self.y),
                                      ("Z", z_mm, self.z)):
            if not (lo <= value <= hi):
                return False, f"{name} {value:.0f}mm 가 작업 공간({lo:.0f}~{hi:.0f}) 밖입니다"
        return True, "OK"

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

    ok, why = box.contains(target["x"] / UM_PER_MM, target["y"] / UM_PER_MM,
                           target["z"] / UM_PER_MM)
    if not ok:
        # ⚠ 클램프해서 보내지 않는다. 상자 모서리로 미끄러져 들어가면 사용자는
        #   자기가 시킨 것과 다른 곳으로 간 이유를 모른다 — 거절하고 말한다.
        return None, why
    return target, "OK"


def reached(target: dict[str, int], now: dict[str, int],
            tol_mm: float = 5.0, tol_deg: float = 3.0) -> bool:
    """명령한 곳에 왔나.

    ⚠ 안 왔으면 **더 보내면 안 된다.** IK 해가 없는 곳을 계속 밀면 팔이 떨거나
    특이점에서 튄다. 못 가는 방향이라는 것을 사람에게 알려야 한다.
    """
    for axis in AXES:
        scale = UM_PER_MM if axis in _LINEAR else MDEG_PER_DEG
        tol = tol_mm if axis in _LINEAR else tol_deg
        if abs(target.get(axis, 0) - now.get(axis, 0)) > tol * scale:
            return False
    return True
