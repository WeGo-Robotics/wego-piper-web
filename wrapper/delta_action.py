"""델타 체크포인트의 출력을 절대 관절 위치로 되돌린다 (feature/act-delta.md).

학습 데이터는 `lerobot_policy_piper.delta` 가 바꾼 것이다 — 관절 채널은 `d[t] = a[t] − a[t−1]`,
그리퍼는 절대. 로봇은 절대 목표만 받으므로 청크마다:

    절대[i] = 앵커 + Σ_{j≤i} clip(d[j], ±상한)        (관절 채널만, 그리퍼는 그대로)

- **앵커 = 그 추론에 넣은 관측의 state.** 큐에 넣는 시점에 다시 읽지 않는다 — 모델이 본 상태와
  복원 기준이 같아야 한다. 청크 경계마다 실측으로 다시 잡으므로 청크 안의 적분 오차가 다음 청크로
  넘어가지 않는다 (대기 중 드리프트는 act-delta.md "대기 구간 드리프트" — 이것으로는 못 막는다).
- **상한은 누적 전에 건다.** 데모에서 실제로 나온 가장 큰 한 스텝이다(변환기가 잰다). 그보다 빠른 예측은
  데모 밖이라 자른다 — 미친 정책이 임의 자세로 점프하지 않는다. robotd 의 기구학 필터·wrapper 의
  속도 제한은 그 바깥 백스톱으로 그대로 있다.
- **복원과 청크 자르기(`_actions_per_chunk`)의 순서는 상관없다.** 앞에서부터 자르고 누적합은 앞쪽 항만
  보므로 결과가 같다. 그래서 wrapper 는 자른 뒤 되돌린다 (뒤에서 자르게 바뀌면 이 성질이 깨진다 — 테스트).

채널은 **이름**으로 찾는다. 학습 데이터의 이름(변환기가 사이드카에 적는다)이 로봇의 액션 이름에 없으면
시작하지 않는다 — 순서로 맞추면 양팔·다른 로봇에서 엉뚱한 관절에 더한다.

torch 를 import 하지 않는다 — 테스트가 그냥 돈다.
"""

from __future__ import annotations

import numpy as np

DELTA_V1 = "delta_v1"
ABSOLUTE = "absolute"
# 체크포인트 폴더의 표식 — backend app/services/action_space.py 가 쓰고 여기서 대조한다
CKPT_MARKER = "piper_action_space.json"


class DeltaRestorer:
    def __init__(self, names: list[str], delta_names: list[str], clamp: dict[str, float | None]) -> None:
        missing = [n for n in delta_names if n not in names]
        if missing:
            raise ValueError(f"델타 채널 {missing} 가 로봇 액션 {names} 에 없다 — 다른 로봇·다른 이름으로 학습한 체크포인트다")
        if not delta_names:
            raise ValueError("델타 채널이 없다")
        self.names = list(delta_names)
        self.idx = np.array([names.index(n) for n in delta_names])
        # 상한이 없으면(데이터가 한 프레임짜리 에피소드뿐이었다) 자르지 않는다
        self.limit = np.array([np.inf if clamp.get(n) is None else float(clamp[n]) for n in delta_names])
        self.clipped_steps = 0           # 상한에 걸린 스텝 수 — 끝날 때 로그에 남긴다

    def anchor(self, raw_obs: dict) -> np.ndarray:
        """추론에 넣은 관측에서 델타 채널의 현재 위치."""
        return np.array([float(raw_obs[n]) for n in self.names], dtype=np.float64)

    def restore(self, chunk: np.ndarray, anchor: np.ndarray) -> np.ndarray:
        """`(k, D)` 델타 청크(역정규화된 값) → 절대. 그리퍼 등 나머지 채널은 그대로."""
        out = np.array(chunk, dtype=np.float64, copy=True)
        d = out[:, self.idx]
        c = np.clip(d, -self.limit, self.limit)
        self.clipped_steps += int((c != d).any(axis=1).sum())
        out[:, self.idx] = anchor + np.cumsum(c, axis=0)
        return out
