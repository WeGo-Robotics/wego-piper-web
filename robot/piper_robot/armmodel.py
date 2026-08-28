"""팔 하나의 기구학 모델 — **리더와 팔로워가 다른 팔이어도 되게.**

## 왜 이 층이 있나

6D 자세 텔레오퍼레이션의 요점은 관절을 복제하지 않는 것이다:

    리더 관절 ──FK──▶ 말단 6D ──IK──▶ 팔로워 관절

가운데 6D 자세만 건너가므로 **양쪽 팔의 관절 구성이 달라도 된다.** SO-101 처럼
축 수도 길이도 다른 팔을 팔로워로 붙이려는 것이 이 설계의 이유다 — 그 팔에
Piper 의 관절값을 직접 대입할 수는 없다.

그래서 FK 도 IK 도 **팔마다 다른 모델**에서 나와야 한다. 이 파일이 그 자리다.

## 새 팔을 붙이려면

1. 그 팔의 URDF 로 지오메트리를 굽는다
   (`tools/build_arm_geometry.py --urdf ... --out .../so101_geometry.npz`)
2. `ArmModel.load("so101")`
3. 끝. FK·IK·야코비안·한계가 전부 그 파일에서 나온다

## 축 수가 달라도 된다

`ik` 는 감쇠 최소자승이라 야코비안이 (6,N) 이든 정사각이 아니어도 푼다.
5축 팔이면 6D 자세를 정확히는 못 맞추지만 **가장 가까운 자세**를 준다 —
그게 5축 팔로 할 수 있는 최선이고, 얼마나 못 맞췄는지(`pos_mm`/`rot_deg`)를
같이 돌려주므로 부르는 쪽이 판단할 수 있다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from piper_robot import kinematics as K

DATA_DIR = Path(__file__).resolve().parent / "data"


@dataclass(frozen=True)
class Solution:
    """IK 결과. **못 푼 것과 코드가 고장난 것을 구분해서** 돌려준다."""

    ok: bool
    q: np.ndarray
    iters: int
    pos_mm: float
    rot_deg: float

    @property
    def reason(self) -> str:
        if self.ok:
            return ""
        return (f"IK 가 목표에 못 닿았습니다 "
                f"(위치 {self.pos_mm:.0f}mm, 자세 {self.rot_deg:.0f}° 남음)")


class ArmModel:
    """한 팔의 FK·IK. 지오메트리 파일 하나에서 전부 나온다."""

    def __init__(self, name: str, geom: K.Geometry, limits: np.ndarray) -> None:
        self.name = name
        self.geom = geom
        self.limits = limits

    # ── 만들기 ──

    @classmethod
    @lru_cache(maxsize=4)
    def load(cls, name: str = "piper") -> "ArmModel":
        """`data/{name}_geometry.npz` 에서. `piper` 는 기존 파일을 그대로 쓴다."""
        if name == "piper":
            return cls(name, K.geometry(), K.joint_limits())
        path = DATA_DIR / f"{name}_geometry.npz"
        if not path.is_file():
            raise FileNotFoundError(
                f"{name} 지오메트리가 없습니다: {path}\n"
                f"  python3 tools/build_arm_geometry.py --urdf <그 팔 URDF> --out {path}")
        with np.load(path, allow_pickle=False) as z:
            geom = K.Geometry(z)
            lim = z["limits"] if "limits" in z else None
        if lim is None:
            raise ValueError(f"{path} 에 관절 한계(limits)가 없습니다 — 다시 구우세요")
        # Piper 와 같은 이유로 여유를 준다 — 실제 팔은 명목 한계 밖에 앉는다.
        m = math.radians(K.IK_LIMIT_MARGIN_DEG)
        return cls(name, geom, np.asarray(lim, dtype=float) + np.array([-m, m]))

    @staticmethod
    def available() -> list[str]:
        """붙일 수 있는 팔 목록. 화면이 고르게 한다."""
        # ⚠ Piper 것은 파일명이 `arm_geometry.npz` 다 — 팔이 하나뿐이던 시절에
        #   붙인 이름이라 여기 규칙(`{팔}_geometry.npz`)에 안 맞는다. 그대로
        #   훑으면 "arm" 이라는 없는 팔이 목록에 뜬다.
        found = {"piper"} if K.available() else set()
        found |= {p.name[: -len("_geometry.npz")]
                  for p in DATA_DIR.glob("*_geometry.npz")
                  if p.name != K.DATA.name}
        return sorted(found)

    # ── 기구학 ──

    #: 허용 오차. 자유도가 모자란 팔은 자세를 다 못 맞추므로 그쪽을 풀어 준다 —
    #: 안 풀면 **닿을 수 있는 자세인데도 늘 실패**라고 답한다.
    @property
    def tol_mm(self) -> float:
        return K.IK_TOL_MM

    @property
    def tol_deg(self) -> float:
        return K.IK_TOL_DEG if self.dof >= 6 else 25.0

    @property
    def dof(self) -> int:
        return int((self.geom.qidx >= 0).sum())

    def fk(self, q_rad: np.ndarray) -> np.ndarray:
        """관절각 → 말단 4x4."""
        return K.link_transforms(np.atleast_2d(q_rad), self.geom)[
            0, self.geom.tip_index]

    def end_pose(self, q_rad: np.ndarray) -> dict[str, int]:
        """관절각 → SDK 단위 6D 자세."""
        return K.end_pose(q_rad, self.geom)

    def lowest_z(self, q_rad: np.ndarray) -> float:
        """이 자세에서 팔의 최저점 (m). 바닥 검사에 쓴다."""
        return float(K.lowest_z(np.atleast_2d(q_rad), self.geom)[0])

    def near_gimbal_lock(self, q_rad: np.ndarray) -> bool:
        return K.near_gimbal_lock(q_rad, self.geom)

    #: 6D 오차의 축별 가중 (위치3 + 자세3). **자유도가 모자란 팔에서 무엇을
    #: 포기할지 정하는 자리다.** 6축이면 다 맞출 수 있으므로 균등이 맞다.
    #: 5축(SO-101)은 임의 6D 를 원리적으로 못 맞춘다 — 위치를 우선한다.
    WEIGHTS_FULL = np.ones(6)
    WEIGHTS_POSITION_FIRST = np.array([1.0, 1.0, 1.0, 0.2, 0.2, 0.2])

    @property
    def weights(self) -> np.ndarray:
        return self.WEIGHTS_FULL if self.dof >= 6 else self.WEIGHTS_POSITION_FIRST

    def ik(self, target: np.ndarray, seed: np.ndarray) -> Solution:
        """말단 4x4 → 관절각. **`seed` 에서 출발한다** — 이어짐이 거기서 나온다."""
        r = K.ik(target, seed, self.limits, geom=self.geom, weights=self.weights,
                 tol_mm=self.tol_mm, tol_deg=self.tol_deg)
        return Solution(bool(r["ok"]), np.asarray(r["q"]), int(r["iters"]),
                        float(r["pos_mm"]), float(r["rot_deg"]))

    def home(self) -> np.ndarray:
        """시드가 없을 때의 출발점. 한계의 가운데다 — 어느 가지에도 안 치우친다."""
        return self.limits.mean(axis=1)
