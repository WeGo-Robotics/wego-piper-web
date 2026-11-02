"""목표 위치 입력 — 학습 때 state 뒤에 붙인 목표 칸을 추론에서 똑같이 만든다 (feature/target-conditioning.md).

학습 데이터는 `lerobot_policy_piper.target` 이 바꾼 것이다 — `observation.state` 뒤에 집을 물체의 위 카메라 좌표를
푸리에 특징으로 편 칸이 붙었다. 추론에서는 로봇 state 뒤에 **지금 목표**(버스의 `target`, 플러그인이 지시문과
함께 보낸다)를 같은 식으로 펴서 붙인다:

    [로봇 state…, valid, u, v, cos 2π b_k·p, sin 2π b_k·p]      p = (u, v·h/w), 목표 없음 = 0

- **식은 여기 다시 적는다** (변환기 `encode` 와 같은 식 — 테스트가 대조). 주파수·종횡비는 데이터 — 사양을
  그대로 받아 쓰므로 다시 뽑지 않는다.
- **로봇 state 이름을 대조한다.** 학습 때의 이름(사양의 `state_names`)이 로봇의 state 앞부분과 다르면 시작하지
  않는다 — 순서로 붙이면 다른 로봇·다른 팔 구성에서 엉뚱한 자리에 목표가 붙는다.
- **목표 없음을 배우지 않은 체크포인트는 목표가 올 때까지 서 있는다** (`needs_target`). 학습 데이터에 목표 없는
  에피소드가 없으면 변환기가 `valid` 칸을 빼고, 0 을 넣을 방법이 없다 — 아무 값이나 넣으면 학습에 없던 자리로
  팔을 뻗을 수 있다. 사과 분류(`empty_target` 15개)는 `valid` 가 있어 목표 없음 = "가만히" 를 배웠다.

torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import math

import numpy as np

TARGET_V1 = "rff_v1"
# 체크포인트 폴더의 표식 — backend app/services/target_input.py 가 쓰고 여기서 대조한다
CKPT_MARKER = "piper_target_input.json"
VALID = "target.valid"
PREFIX = "target."


def encode(target: tuple[float, float] | None, spec: dict) -> np.ndarray:
    """목표 `(u, v)` → 사양의 이름 순서 벡터 (float32). `None` = 목표 없음 = 전부 0."""
    names = spec["names"]
    out = np.zeros(len(names), dtype=np.float32)
    if target is None:
        if names[0] != VALID:
            raise ValueError("이 체크포인트는 목표 없음을 모른다 — 목표가 올 때까지 서 있어야 한다")
        return out
    u, v = float(target[0]), float(target[1])
    b = np.asarray(spec["freqs"], dtype=np.float64).reshape(-1, 2)
    ph = 2.0 * math.pi * (b[:, 0] * u + b[:, 1] * v * float(spec["aspect"]))
    vals = np.concatenate([[u, v], np.cos(ph), np.sin(ph)])
    if names[0] == VALID:
        vals = np.concatenate([[1.0], vals])
    out[:] = vals
    return out


def parse_target(value) -> tuple[float, float] | None:
    """버스 값 → `(u, v)`(0~1 로 자른다) 또는 `None`(지움). 모양이 틀리면 ValueError — 호출부가 지운다."""
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, dict):
        value = [value.get("u"), value.get("v")]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"목표는 [u, v] 두 값이어야 한다: {value!r}")
    u, v = float(value[0]), float(value[1])
    if not (math.isfinite(u) and math.isfinite(v)):
        raise ValueError(f"목표에 유한하지 않은 값: {value!r}")
    return min(1.0, max(0.0, u)), min(1.0, max(0.0, v))


def same_spec(a: dict, b: dict) -> bool:
    """두 사양이 같은 벡터를 만드나 — 이름·주파수·종횡비."""
    return (list(a.get("names") or []) == list(b.get("names") or [])
            and np.allclose(np.asarray(a.get("freqs") or [], dtype=float).reshape(-1, 2),
                            np.asarray(b.get("freqs") or [], dtype=float).reshape(-1, 2))
            and math.isclose(float(a.get("aspect", 0)), float(b.get("aspect", 0))))


class TargetInput:
    """로봇 state + 지금 목표 → 체크포인트가 기대하는 긴 state."""

    def __init__(self, spec: dict, robot_state_names: list[str], expected_len: int) -> None:
        if spec.get("encoding") != TARGET_V1:
            raise ValueError(f"모르는 목표 인코딩: {spec.get('encoding')!r}")
        self.spec = spec
        self.names = list(spec["names"])
        self.k = len(self.names)
        trained = list(spec.get("state_names") or [])
        self.n = expected_len - self.k
        if self.n <= 0 or self.n != len(trained):
            raise ValueError(f"체크포인트 state {expected_len} 가 학습 사양(로봇 {len(trained)} + 목표 {self.k})과 "
                             f"안 맞는다 — 다른 데이터셋으로 학습한 체크포인트다")
        if list(robot_state_names[:self.n]) != trained:
            raise ValueError(f"로봇 state 이름이 학습 때와 다르다 — 학습 {trained[:3]}…, 로봇 {list(robot_state_names[:3])}… "
                             f"(다른 로봇·팔 구성이다)")
        self.needs_target = self.names[0] != VALID

    def extend(self, state, target: tuple[float, float] | None) -> np.ndarray:
        """로봇 state(앞 `n` 칸) 뒤에 목표 칸을 붙인다."""
        s = np.asarray(state, dtype=np.float32).reshape(-1)
        if len(s) < self.n:
            raise ValueError(f"로봇 state 가 {len(s)} 칸이다 (학습 {self.n})")
        return np.concatenate([s[:self.n], encode(target, self.spec)])

    def summary(self) -> dict:
        """실행 기록에 남길 것."""
        return {"encoding": self.spec.get("encoding"), "dims": self.k, "robot_dims": self.n,
                "length_mm": self.spec.get("length_mm"), "camera": self.spec.get("camera"),
                "needs_target": self.needs_target}
