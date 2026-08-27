"""작업 단계(phase) 라벨러 — 신호 추출 + 인과 FSM.

`observation.state` 에 "지금 로봇이 무슨 단계를 수행 중인지"를 넣기 위한 코어다
(feature/01-phase-annotation.md).

## ⚠ 가장 중요한 제약 — 오프라인 라벨러와 온라인 추정기는 같은 코드여야 한다

학습 데이터의 페이즈 값이 추론 시에도 채워져야 하므로, 라벨러는 **미래를 보지 않고**
(past-only) 계산 가능한 부분과 오프라인 전용 보정을 명확히 분리한다:

| 층 | 내용 | 온라인 |
|---|---|---|
| **인과 코어** (`PhaseFSM`) | 아래 전이 규칙 전부. 과거 N프레임만 참조 | ✅ 그대로 재사용 |
| 오프라인 보정 (`finalize`) | `DONE` 판정(미래 참조), 최소 구간 길이 흡수 | ❌ |

두 벌로 나뉘면 반드시 어긋나므로 **백엔드 라벨러와 wrapper 추정기가 이 파일을 함께 import** 한다.
`bus/` 와 같은 방식의 설치 가능한 패키지다 — `pip install -e phase/`.

## 페이즈

한 에피소드 안에서 집기가 **여러 번 반복된다** (큐브 3개 = 3사이클).
페이즈는 단조 증가가 아니라 **순환**한다 — 이걸 놓치면 첫 사이클 이후 전부 "완료"가 된다.

의존성은 numpy 뿐이다 (wrapper 가 백엔드를 import 하면 안 된다).
"""

from dataclasses import dataclass, field, replace

import numpy as np

# ── 페이즈 코드 ───────────────────────────────────────────────────────────────

IDLE = 0
APPROACH = 1
ALIGN = 2
GRASP = 3
HOLD = 4
RELEASE = 5
DONE = 6
# 마지막 놓기 뒤 **원점으로 돌아가는** 구간. 예전에는 이것도 APPROACH 였다 —
# 다음 물체로 접근하는 것과 구분이 안 됐고, 그 바람에 DONE 이 쓸 꼬리까지 먹었다.
PARKING = 7

# ⚠ **끝에만 붙인다.** 사이드카는 페이즈를 **정수**로 저장한다. 중간에 끼우면
#   이미 라벨링해둔 데이터셋의 뜻이 조용히 바뀐다 — HOLD 였던 4가 다른 것이 된다.
PHASE_NAMES: tuple[str, ...] = (
    "IDLE", "APPROACH", "ALIGN", "GRASP", "HOLD", "RELEASE", "DONE", "PARKING",
)

GRIPPER_IDX = 6  # state/action 의 마지막 채널
JOINT_SLICE = slice(0, 6)


@dataclass(frozen=True)
class Params:
    """임계값. 로봇·태스크마다 다르므로 전부 밖으로 뺀다."""

    fps: float = 15.0
    # ⚠ 단위는 **정규화 단위/초** 다. `deg/s` 라고 적혀 있었는데 도가 아니다 —
    #   `observation.state` 는 ±100 정규화 값이다(실측: 관절별 범위가 ±100 에서
    #   잘린다). 값 자체는 같은 단위로 튜닝돼 있어 동작은 맞지만, 조정하려는
    #   사람이 "20도/초"로 읽으면 어긋난다.
    #   도(°) 나 m/s 로 보고 싶으면 `kinematics.endpoint_speed` 쪽이다.
    still_speed: float = 2.0      # 이하 = 정지
    moving_speed: float = 20.0    # 초과 = 이동 중
    align_speed: float = 12.0     # 이하로 감속하면 미세 접근
    hold_gap: float = -15.0       # 지령-실측 갭이 이보다 작으면 물체가 물려 있다
    hold_cmd_max: float = 20.0    # 그 때 지령은 거의 닫힘이어야 한다
    grip_rate: float = 20.0       # 그리퍼 변화율 |d/dt| 임계 (닫힘/열림 감지)
    gripper_open_min: float = 60.0  # 이 이상이면 "열려 있다"
    # 연속 프레임 요구치 (히스테리시스)
    n_moving: int = 6             # IDLE → APPROACH
    n_align: int = 8              # APPROACH → ALIGN
    n_reapproach: int = 8         # ALIGN → APPROACH
    n_hold: int = 5               # GRASP → HOLD
    # 오프라인 전용
    min_segment: int = 4          # 이보다 짧은 구간은 앞 구간에 흡수
    # 끝 정지구간 최소 길이. 문서 초안은 20이었으나 실측 꼬리가 5~25프레임이라
    # 20이면 절반이 DONE 을 못 받아 라벨이 에피소드마다 들쭉날쭉해진다.
    # "끝까지 아무 일 없음" 이 이미 강한 조건이므로 길이 요구는 낮게 둔다.
    done_still: int = 5
    # 정지 판정 전에 속도에 씌우는 중앙값 창(프레임, 홀수). 1이면 끔.
    # 5는 15fps 에서 3분의 1초 — 지터는 지우고 실제 정지 시점은 거의 안 움직인다.
    still_window: int = 5
    # 끝 정지 구간을 되짚을 때 눈감아 줄 비정지 프레임 수.
    # ⚠ 잔여 지터가 임계(2.0) **바로 위**에 있다 — 실측 2.4~4.2. 연속을 요구하면
    #   멈춰 선 팔도 한두 프레임 때문에 DONE 을 못 받는다. 임계 자체를 올리면
    #   온라인 FSM 의 정지 판정까지 헐거워지므로 여기서만 봐준다.
    done_jitter: int = 3


@dataclass
class Signals:
    """프레임별 신호. 전부 parquet 만으로 계산된다 (손목 변화율 제외)."""

    speed: np.ndarray            # 관절 속도 (정규화 단위/초 — 위 주석 참고)
    gripper_gap: np.ndarray      # action[6] - state[6]
    gripper_cmd: np.ndarray
    gripper_state: np.ndarray
    grip_rate: np.ndarray        # d(state[6])/dt
    hold: np.ndarray             # bool — 물체를 물고 있는가
    wrist_diff: np.ndarray | None = None   # 손목 카메라 변화율 (비디오 필요)
    proximity: np.ndarray | None = None    # wrist_diff / speed

    def __len__(self) -> int:
        return len(self.speed)


def compute_signals(state: np.ndarray, action: np.ndarray, params: Params) -> Signals:
    """(T, 7) state/action 에서 신호를 뽑는다.

    **그리퍼 지령/실측 갭이 가장 강력한 신호다** — 지령은 0(완전 닫힘)인데 실측이
    34에서 멈춰 있으면 물체가 물려 있다는 뜻이다. 비전 없이 "집기 성공"을 판정할 수 있어
    FSM 의 척추로 쓴다.
    """
    state = np.asarray(state, dtype=np.float64)
    action = np.asarray(action, dtype=np.float64)
    if state.ndim != 2 or state.shape[1] <= GRIPPER_IDX:
        raise ValueError(f"state 형태가 (T,7+) 이어야 한다: {state.shape}")

    # 팔 수 판정 — 양팔(bi_piper)은 14축(팔당 7, 그리퍼가 7의 배수-1 위치)이다.
    # 예전에는 (T,14) 를 받아도 조용히 앞 6축만 보고 left_gripper 를 그리퍼로
    # 읽었다 — 소리 없이 왼팔만 분석하는 셈이었다 (feature/bimanual.md 체크리스트의
    # "7 하드코딩 잔재"가 바로 이 자리다).
    width = state.shape[1]
    if width >= 14 and width % 7 == 0:
        grip_idxs = [7 * i + 6 for i in range(width // 7)]
    else:
        grip_idxs = [GRIPPER_IDX]
    joint_cols = [i for i in range(width) if i not in grip_idxs]

    joints = state[:, joint_cols]
    d = np.diff(joints, axis=0, prepend=joints[:1])
    speed = np.linalg.norm(d, axis=1) * params.fps

    # FSM 은 단일 pick 시퀀스 모델이다 — 양팔이면 **더 활동적인 그리퍼**(주 팔)
    # 기준으로 신호를 뽑는다. 양팔 동시 파지의 페이즈 모델은 다음 단계 문제다.
    g_idx = max(grip_idxs, key=lambda i: float(np.std(state[:, i])))
    g_state = state[:, g_idx]
    g_cmd = action[:, g_idx]
    gap = g_cmd - g_state
    grip_rate = np.diff(g_state, prepend=g_state[:1]) * params.fps

    hold = (gap < params.hold_gap) & (g_cmd < params.hold_cmd_max)

    return Signals(
        speed=speed, gripper_gap=gap, gripper_cmd=g_cmd, gripper_state=g_state,
        grip_rate=grip_rate, hold=hold,
    )


def attach_wrist(sig: Signals, wrist_diff: np.ndarray) -> Signals:
    """손목 카메라 변화율을 붙인다 (비디오 디코딩이 필요해 선택적).

    근접도 = 변화율/속도. 물체에 가까울수록 같은 관절 이동이 만드는 시야 변화가 커진다.
    **신호는 살아 있지만 약해서** `ALIGN` 판정의 보조로만 쓴다.
    """
    w = np.asarray(wrist_diff, dtype=np.float64)
    prox = w / np.maximum(sig.speed, 1e-6)
    return replace(sig, wrist_diff=w, proximity=prox)


# ── 인과 코어 ─────────────────────────────────────────────────────────────────


@dataclass
class _Streak:
    """조건이 몇 프레임 연속됐는지."""

    counts: dict[str, int] = field(default_factory=dict)

    def feed(self, key: str, cond: bool) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1 if cond else 0
        return self.counts[key]


class PhaseFSM:
    """프레임을 하나씩 먹여 페이즈를 얻는다. **미래를 보지 않는다.**

    추론 시 wrapper 가 그대로 쓰는 것이 이 클래스다 —
    오프라인 라벨러도 같은 클래스를 쓰므로 학습/추론 분포가 어긋나지 않는다.
    """

    def __init__(self, params: Params | None = None) -> None:
        self.p = params or Params()
        self.phase = IDLE
        self.cycles = 0          # 완료한 집기 사이클 수
        self._s = _Streak()

    def reset(self) -> None:
        self.phase = IDLE
        self.cycles = 0
        self._s = _Streak()

    def update(
        self,
        speed: float,
        gripper_cmd: float,
        gripper_state: float,
        grip_rate: float,
        proximity: float | None = None,
    ) -> int:
        p = self.p
        still = speed < p.still_speed
        moving = speed > p.moving_speed
        slow = speed < p.align_speed
        gap = gripper_cmd - gripper_state
        hold = gap < p.hold_gap and gripper_cmd < p.hold_cmd_max
        closing = grip_rate < -p.grip_rate
        opening = grip_rate > p.grip_rate
        gripper_open = gripper_state > p.gripper_open_min

        n_moving = self._s.feed("moving", moving)
        n_slow = self._s.feed("slow_open", slow and gripper_open)
        n_hold = self._s.feed("hold", hold)
        n_still = self._s.feed("still", still)

        cur = self.phase
        if cur == IDLE:
            if n_moving >= p.n_moving:
                self.phase = APPROACH
        elif cur == APPROACH:
            if closing:
                self.phase = GRASP          # 빠른 집기 — ALIGN 을 건너뛴다
            elif n_slow >= p.n_align:
                self.phase = ALIGN
        elif cur == ALIGN:
            if closing:
                self.phase = GRASP
            elif n_moving >= p.n_reapproach:
                self.phase = APPROACH       # 재접근
        elif cur == GRASP:
            if n_hold >= p.n_hold:
                self.phase = HOLD           # 집기 성공
            elif not hold and gripper_state < 5.0:
                # 끝까지 닫혔는데 갭이 없다 = 빈손 → 재시도
                self.phase = APPROACH
        elif cur == HOLD:
            if opening:
                self.phase = RELEASE
        elif cur == RELEASE:
            if gripper_open and moving:
                self.cycles += 1
                self.phase = APPROACH       # ← 순환 고리
        # DONE 은 오프라인 전용 (미래를 봐야 한다) — 온라인에서는 나오지 않는다

        if self.phase != cur:
            self._s.counts.clear()
        _ = n_still  # 온라인에서는 정지만으로 DONE 을 내지 않는다
        return self.phase


def label_causal(sig: Signals, params: Params | None = None) -> np.ndarray:
    """인과 코어만으로 전 프레임 라벨링. 온라인과 **비트 단위로 같은 결과**여야 한다."""
    p = params or Params()
    fsm = PhaseFSM(p)
    prox = sig.proximity
    out = np.empty(len(sig), dtype=np.int8)
    for i in range(len(sig)):
        out[i] = fsm.update(
            float(sig.speed[i]),
            float(sig.gripper_cmd[i]),
            float(sig.gripper_state[i]),
            float(sig.grip_rate[i]),
            float(prox[i]) if prox is not None else None,
        )
    return out


# ── 오프라인 보정 ─────────────────────────────────────────────────────────────


def _median_filter(x: np.ndarray, window: int) -> np.ndarray:
    """길이를 유지하는 중앙값 필터. 가장자리는 끝값으로 채운다.

    한 프레임짜리 스파이크를 지우는 것이 목적이다. 창을 키우면 정지 시점이 뒤로
    밀리므로 짧게 쓴다.
    """
    if window <= 1 or len(x) < window:
        return x
    half = window // 2
    # ⚠ 가장자리를 **반사**로 채운다. `edge` 로 채우면 마지막 값이 창의 과반이 되어
    #   자기 자신이 중앙값이 된다 — 끝 프레임의 지터가 그대로 살아남는다.
    #   실측: 멈춘 팔의 마지막 한 프레임이 2.4 라 DONE 이 통째로 날아갔다.
    padded = np.pad(x, half, mode="reflect")
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.median(view, axis=-1)


def finalize(phases: np.ndarray, sig: Signals, params: Params | None = None) -> np.ndarray:
    """오프라인 전용 보정. **미래를 참조하므로 온라인에서 쓰면 안 된다.**

    1. `DONE` — "정지 + 에피소드 끝까지 아무 일도 없음". 정지만으로 판정하면 안 된다:
       에피소드 중간에도 `speed=0` 인 순간이 흔하고, 꼬리 정지구간은 5~25프레임으로 짧다.
    2. 최소 구간 길이 — 짧은 구간을 앞 구간에 흡수한다.
    """
    p = params or Params()
    out = phases.copy()
    n = len(out)

    closing = sig.grip_rate < -p.grip_rate
    # ⚠ **지터를 먼저 걷어낸다.** 뒤에서부터 정지 구간을 찾을 때 끊기지 않은 연속을
    #   요구하는데, 멈춰 선 팔도 엔코더 잡음으로 한 프레임씩 임계를 넘는다.
    #   실측(bolt_two1): 마지막 20프레임 중 19개가 정지인데 **맨 끝 한 프레임이
    #   2.4**(임계 2.0)라 스캔이 즉시 멈추고 DONE 이 통째로 날아갔다. 50개 중
    #   16개가 그랬다.
    #
    #   평균이 아니라 **중앙값**이다 — 평균은 경계를 뭉개서 정지 시점을 앞당긴다.
    still = _median_filter(sig.speed, p.still_window) < p.still_speed

    # 1. PARKING — **마지막 놓기 뒤로 다시 집지 않으면** 그 이후는 복귀다.
    #
    # 예전에는 이 구간이 APPROACH 였다. FSM 의 RELEASE→APPROACH 전이가
    # `gripper_open and moving` 이라, 원점으로 돌아가는 움직임과 다음 물체로
    # 접근하는 움직임을 구분할 수 없었기 때문이다. 온라인에서는 정말 구분이
    # 안 된다 — 다음에 집을지 말지는 미래다. 그래서 **오프라인 전용**이고,
    # DONE 이 이미 그런 것과 같은 이유다.
    rel = np.flatnonzero(out == RELEASE)
    parked_from = None
    if len(rel):
        after = int(rel[-1]) + 1
        # 그 뒤로 무는 동작이 한 번이라도 있으면 복귀가 아니라 다음 사이클이다
        if after < n and not (sig.hold[after:].any() or closing[after:].any()):
            out[after:] = PARKING
            parked_from = after

    # 2. DONE — 끝의 정지 구간. 복귀가 있으면 그 안에서, 없으면 예전 방식대로.
    #
    # ⚠ 복귀를 갈라내기 전에는 이 스캔이 **복귀 동작에서 멈췄다.** 팔이 움직이는
    #   중이라 `still` 이 거짓이니 꼬리가 0이 되고, DONE 이 아예 안 붙는
    #   에피소드가 흔했다. 이제 정지한 부분만 남으므로 제대로 잡힌다.
    # ⚠ **끝 프레임은 반드시 정지여야 한다.** 아래 지터 허용이 여기까지 번지면
    #   가속 중에 끝난 에피소드에도 DONE 이 붙는다 — 실측에서 마지막 속도가
    #   33.9 인 에피소드가 DONE 을 받았다. 봐주기는 구간 **안쪽**의 튐만이다.
    floor = parked_from if parked_from is not None else 0
    if n == 0 or not still[n - 1]:
        return _absorb_short_segments(out, p.min_segment)
    tail, bad = n, 0
    while tail > floor and not sig.hold[tail - 1] and not closing[tail - 1]:
        if not still[tail - 1]:
            if bad >= p.done_jitter:
                break
            bad += 1
        tail -= 1
    # 눈감아 준 프레임은 **빼고** 센다 — 봐주기가 길이 요구를 대신하면 안 된다
    if (n - tail) - bad >= p.done_still:
        out[tail:] = DONE

    # 2. 최소 구간 길이 — 앞 구간에 흡수
    return _absorb_short_segments(out, p.min_segment)


def _absorb_short_segments(phases: np.ndarray, min_len: int) -> np.ndarray:
    out = phases.copy()
    for start, end, value in segments(out):
        if end - start + 1 >= min_len or start == 0:
            continue
        out[start:end + 1] = out[start - 1]
        _ = value
    return out


def segments(phases: np.ndarray) -> list[tuple[int, int, int]]:
    """`[start, end(포함), phase]` 구간 리스트.

    31k 개 값 배열보다 작고, UI 에서 경계 드래그가 곧 편집이 된다.
    """
    if len(phases) == 0:
        return []
    out: list[tuple[int, int, int]] = []
    start = 0
    for i in range(1, len(phases)):
        if phases[i] != phases[start]:
            out.append((start, i - 1, int(phases[start])))
            start = i
    out.append((start, len(phases) - 1, int(phases[start])))
    return out


def count_cycles(phases: np.ndarray) -> int:
    """집기 사이클 수 = `hold` 상승 에지 개수 = `HOLD` 구간 개수."""
    return sum(1 for _, _, v in segments(phases) if v == HOLD)


def label_episode(
    state: np.ndarray,
    action: np.ndarray,
    params: Params | None = None,
    wrist_diff: np.ndarray | None = None,
) -> tuple[np.ndarray, Signals]:
    """오프라인 전체 파이프라인 — 신호 → 인과 FSM → 오프라인 보정."""
    p = params or Params()
    sig = compute_signals(state, action, p)
    if wrist_diff is not None:
        sig = attach_wrist(sig, wrist_diff)
    return finalize(label_causal(sig, p), sig, p), sig
