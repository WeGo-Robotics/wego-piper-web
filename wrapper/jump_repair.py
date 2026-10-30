"""계획 안 순간이동 고쳐 잇기 — 청크 안에서 말단이 한 스텝에 너무 멀리 가는 곳을 매끈하게 잇는다.

## 왜

SmolVLA-S 가 정지 자세에서 출발하는 청크는 몇 칸 머물다 **한 스텝에 60~87mm** 뛰었다(15Hz, 2026-10-29
실기). 시연은 최대 29mm 이고 4스텝에 걸쳐 가속한다. 원인은 학습 데이터다 — 시연의 출발 전 대기가
0~33프레임으로 제각각이라 정책이 출발 시점을 못 정하고, "기다리는 부분"과 "이미 출발한 부분"을 서로
다른 출발 시점에서 따와 잇는다. 그 이음새가 순간이동이다. 학습 데이터의 에피소드 시작 정지 장면에서도
39% 가 그랬다(끝 장면은 100% 정지). num_steps 40·노이즈 배율·RTC 로는 거의 안 준다.

병합(chunk_merge)은 청크 **경계**를 잇는다 — 청크 안의 한 스텝 60mm 는 스무딩 창 7 로 못 편다.
ACT 도 가끔 튄다(청크 30개 중 2개, 223957).

## 무엇을 하나

1. 찾기 — 말단(FK)이 한 스텝에 `LIMIT_SPEED / fps` 보다 많이 가는 칸. 15Hz 에서 35mm: 시연 최대 29mm,
   순간이동 최소 40mm 사이다. 관절 공간 판정은 시연과 안 갈렸다 — 앞뒤 스텝 비율로 찾았더니 시연 창의
   38% 를 최대 30mm 고쳤고, 관절 σ 로 나눈 크기는 순간이동 최소가 시연 최대보다 작았다.
2. 고치기 — 튄 칸부터 n 칸을 큐빅 Hermite 로 바꾼다. 양 끝의 위치·속도를 원래 계획에 맞춰 합류한다.
   n 은 이은 구간의 가장 큰 스텝이 한도 안에 들 때까지 늘린다(최대 `MAX_SEC`).
3. 팔 관절만 — 그리퍼는 손대지 않는다.

81초 실기 기록(SmolVLA-S, 090640)을 병합과 함께 재생: 2~5Hz 폭발 구간 최악 3곳 5.4·3.8·3.2 →
2.0·1.6·1.4mm, 한 스텝 최대 42 → 35mm, 바뀐 스텝 5%. 대가로 출발 직후 몇 스텝은 원래 계획과 최대
~100mm 다르다 — 순간이동을 시연처럼 가속하는 출발로 바꾸고 헛출발(올라갔다 도로 내려옴)을 지운 만큼이다.

말단 위치가 필요하므로 PIPER 팔에서만 돈다(`piper_arms`·`piper_fk`). torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import numpy as np

# 15Hz 한 스텝 35mm. 제어 주기가 바뀌면 스텝 한도가 따라간다
LIMIT_SPEED = 525.0   # mm/s
# 이은 구간의 길이 상한 (15Hz 12스텝). 그 안에 못 맞추면 그 길이로 잇는다
MAX_SEC = 0.8
# 첫 n 추정: 이은 구간의 평균 속도가 튄 뒤 속도의 이 배가 되게
CATCHUP = 1.5
MAX_ITER = 4
DEFAULT_ON = True


def _hermite(y: np.ndarray, i: int, j: int, cols: list[int]) -> np.ndarray:
    """y[i] → y[j] 사이 (j-i-1) 칸. 끝점의 위치와 속도(스텝당)를 원래 계획에 맞춘다."""
    n = j - i
    p0, p1 = y[i, cols], y[j, cols]
    v0 = y[i, cols] - y[i - 1, cols] if i > 0 else np.zeros_like(p0)
    v1 = y[j + 1, cols] - y[j, cols] if j + 1 < len(y) else y[j, cols] - y[j - 1, cols]
    t = (np.arange(1, n) / n)[:, None]
    return ((2 * t**3 - 3 * t**2 + 1) * p0 + (t**3 - 2 * t**2 + t) * n * v0
            + (-2 * t**3 + 3 * t**2) * p1 + (t**3 - t**2) * n * v1)


class JumpRepair:
    """청크를 받아 고친 청크를 돌려준다. 팔(FK)을 붙이기 전에는 아무것도 안 한다."""

    def __init__(self, on: bool = DEFAULT_ON) -> None:
        self.on = on
        self.fk = None                    # (T,6) 정규화 관절 → (T,3) mm
        self.arms: list[list[int]] = []   # 팔마다 joint1~6 의 열 번호
        self._set_fps(15)

    def attach(self, fk, arms: list[list[int]], fps: float) -> None:
        self.fk = fk
        self.arms = [list(a) for a in arms]
        self._set_fps(fps)

    def _set_fps(self, fps: float) -> None:
        self.limit = LIMIT_SPEED / fps
        self.max_n = max(2, round(MAX_SEC * fps))

    def update_param(self, key: str, value) -> bool:
        """버스 파라미터 업데이트. 처리했으면 True."""
        if key == "jump_repair":
            self.on = bool(value)
            return True
        return False

    @property
    def enabled(self) -> bool:
        return self.on and self.fk is not None and bool(self.arms)

    def repair(self, chunk) -> tuple[np.ndarray, list[tuple[int, int]]]:
        """(고친 청크 (n, D), [(튄 칸, 이은 길이), ...]). 고칠 것이 없으면 원본과 같은 값이다."""
        y = np.array(chunk, dtype=np.float64)
        fixed: list[tuple[int, int]] = []
        if self.enabled and len(y) >= 3:
            for cols in self.arms:
                fixed += self._repair_arm(y, cols)
        return y, fixed

    def _steps(self, q: np.ndarray) -> np.ndarray:
        return np.linalg.norm(np.diff(self.fk(q), axis=0), axis=1)

    def _repair_arm(self, y: np.ndarray, cols: list[int]) -> list[tuple[int, int]]:
        fixed = []
        last = len(y) - 1
        tried: list[int] = []
        for _ in range(MAX_ITER):
            s = self._steps(y[:, cols])
            # 마지막 칸으로 가는 스텝은 이을 자리가 없다 — 큐 모드는 거기까지 가기 전에 다음 청크가 온다.
            # 한 번 이은 자리는 다시 안 본다 — 끝에 걸려 다 못 편 곳을 같은 식으로 되풀이할 뿐이다
            cand = s.copy()
            cand[[last - 1, *tried]] = 0.0
            i = int(np.argmax(cand))
            if cand[i] <= self.limit:
                break
            tried.append(i)
            after = s[i + 1:i + 4]
            a = float(np.median(after)) if len(after) else 0.0
            n = int(np.ceil((s[i] - a) / ((CATCHUP - 1) * a))) if a > 1e-9 else self.max_n
            n = int(np.clip(n, 2, self.max_n))
            # 이은 구간의 가장 큰 스텝이 한도 안에 들 때까지 늘린다
            for n in range(n, self.max_n + 1):
                j = min(i + n, last)
                seg = _hermite(y, i, j, cols)
                if j == last or self._steps(np.vstack([y[i, cols], seg, y[j, cols]])).max() <= self.limit:
                    break
            y[i + 1:j, cols] = seg
            fixed.append((i, j - i))
        return fixed


def piper_arms(robot_type: str, motor_names: list[str]) -> list[list[int]]:
    """PIPER 팔마다 joint1~6 의 열 번호. 양팔은 `left_`·`right_` 접두사로 갈린다. PIPER 가 아니면 []."""
    if "piper" not in robot_type:
        return []
    arms = []
    for name in motor_names:
        if name.endswith("joint1.pos"):
            prefix = name[: -len("joint1.pos")]
            want = [f"{prefix}joint{k}.pos" for k in range(1, 7)]
            if all(w in motor_names for w in want):
                arms.append([motor_names.index(w) for w in want])
    return arms


def piper_fk():
    """정규화 관절 (T,6) → 말단 (T,3) mm. 기구학을 못 읽으면 None — 고쳐 잇기만 꺼지고 추론은 돈다."""
    try:
        from piper_robot import kinematics as K
    except ImportError:
        return None
    if not K.available():
        return None
    return lambda q: K.endpoint_xyz(K.norm_to_rad(q)) * 1000.0
