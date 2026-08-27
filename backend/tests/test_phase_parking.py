"""마지막 놓기 뒤의 **원점 복귀**를 PARKING 으로 가른다.

예전에는 그 구간이 APPROACH 였다. FSM 의 `RELEASE → APPROACH` 전이가
`gripper_open and moving` 이라, 원점으로 돌아가는 움직임과 다음 물체로 접근하는
움직임을 구분할 근거가 없었기 때문이다.

그 바람에 DONE 도 같이 망가졌다. DONE 은 "끝의 정지 구간"을 뒤에서부터 찾는데,
복귀 동작에서 스캔이 멈춘다 — 팔이 움직이는 중이라 `still` 이 거짓이다. 꼬리가
0이 되어 **DONE 이 아예 안 붙는 에피소드가 흔했다.**
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "phase"))

from piper_phase.fsm import (  # noqa: E402
    APPROACH, DONE, PARKING, PHASE_NAMES, Params, compute_signals, finalize,
    label_causal, label_episode, segments,
)

_P = Params(fps=15.0)


def _episode(*, park_frames: int, settle_frames: int, second_pick: bool = False):
    """집기 1회 → 놓기 → (복귀) → (정지). `second_pick` 이면 놓은 뒤 또 집는다."""
    rows_s, rows_a = [], []

    def add(n, joints, grip_s, grip_c):
        for _ in range(n):
            rows_s.append(list(joints) + [grip_s])
            rows_a.append(list(joints) + [grip_c])

    j = [0.0] * 6
    add(8, j, 80.0, 80.0)                     # IDLE
    for k in range(14):                       # APPROACH — 크게 이동
        j = [k * 3.0] + [0.0] * 5
        add(1, j, 80.0, 80.0)
    add(10, j, 80.0, 80.0)                    # ALIGN (감속)
    add(3, j, 40.0, 0.0)                      # 닫는 중
    add(12, j, 34.0, 0.0)                     # HOLD (갭)
    for k in range(10):                       # 옮기기
        j = [42.0 + k * 2.0] + [0.0] * 5
        add(1, j, 34.0, 0.0)
    add(4, j, 80.0, 80.0)                     # RELEASE (여는 중)

    if second_pick:
        for k in range(10):
            j = [62.0 - k * 3.0] + [0.0] * 5
            add(1, j, 80.0, 80.0)
        add(3, j, 40.0, 0.0)
        add(10, j, 34.0, 0.0)                 # 또 물었다 = 복귀가 아니다
        add(4, j, 80.0, 80.0)

    # 복귀 — **시작 자세까지 실제로 되돌아간다.** 도중에 멈추면 그건 복귀가 아니고,
    # 기하 확인(`_returns_home`)이 그걸 거른다.
    start = j[0]
    for k in range(park_frames):
        j = [start * (1.0 - (k + 1) / max(park_frames, 1))] + [0.0] * 5
        add(1, j, 80.0, 80.0)
    add(settle_frames, j, 80.0, 80.0)         # 정지

    return np.array(rows_s, dtype=np.float32), np.array(rows_a, dtype=np.float32)


def _phases(**kw):
    state, action = _episode(**kw)
    return label_episode(state, action, _P)[0]


def test_the_return_home_is_parking_not_approach():
    """⚠ **회귀** — 이게 APPROACH 로 잡혀서 "또 집으러 간다"로 읽혔다."""
    ph = _phases(park_frames=25, settle_frames=12)
    assert PARKING in ph.tolist(), "복귀 구간이 없다"
    park = np.flatnonzero(ph == PARKING)
    rel = np.flatnonzero(ph == 5)             # RELEASE
    assert park.min() > rel.max(), "놓기 전에 복귀가 나온다"


def test_done_lands_on_the_settled_tail(): 
    """⚠ 복귀를 갈라내기 전에는 스캔이 **복귀 동작에서 멈춰** DONE 이 안 붙었다."""
    ph = _phases(park_frames=25, settle_frames=12)
    assert ph[-1] == DONE, "끝이 DONE 이 아니다"
    assert (ph == DONE).sum() >= _P.done_still


def test_parking_and_done_do_not_overlap():
    ph = _phases(park_frames=25, settle_frames=12)
    assert np.flatnonzero(ph == PARKING).max() < np.flatnonzero(ph == DONE).min()


def test_a_release_followed_by_another_pick_is_not_parking():
    """또 무는 동작이 있으면 그건 복귀가 아니라 다음 사이클이다."""
    ph = _phases(park_frames=20, settle_frames=12, second_pick=True)
    park = np.flatnonzero(ph == PARKING)
    grasp = np.flatnonzero(ph == 3)           # GRASP
    assert not len(park) or park.min() > grasp.max(), "중간 이동을 복귀로 본다"


def test_an_episode_that_never_settles_has_no_done():
    """끝까지 움직이는 중이면 DONE 이 없어야 한다 — 없는 것을 지어내면 안 된다."""
    ph = _phases(park_frames=25, settle_frames=0)
    assert DONE not in ph.tolist()
    assert ph[-1] == PARKING


def test_parking_needs_the_future_so_it_stays_offline():
    """온라인에서는 다음에 또 집을지 알 수 없다 — DONE 과 같은 이유로 오프라인 전용."""
    state, action = _episode(park_frames=25, settle_frames=12)
    sig = compute_signals(state, action, _P)
    assert PARKING not in label_causal(sig, _P).tolist()
    assert PARKING in finalize(label_causal(sig, _P), sig, _P).tolist()


def test_the_new_code_is_appended_not_inserted():
    """⚠ 사이드카는 페이즈를 **정수**로 저장한다.

    중간에 끼우면 이미 라벨링해둔 데이터셋의 뜻이 조용히 바뀐다 — HOLD 였던 4가
    다른 것이 된다. 기존 코드는 그대로 두고 뒤에만 붙인다.
    """
    assert PHASE_NAMES[:7] == ("IDLE", "APPROACH", "ALIGN", "GRASP", "HOLD",
                               "RELEASE", "DONE")
    assert PHASE_NAMES[7] == "PARKING" and PARKING == 7


def test_segments_stay_contiguous():
    """구간이 겹치거나 비면 뷰어의 경계 드래그가 어긋난다."""
    ph = _phases(park_frames=25, settle_frames=12)
    segs = segments(ph)
    assert segs[0][0] == 0 and segs[-1][1] == len(ph) - 1
    for (_s0, end, _v), (start, *_rest) in zip(segs, segs[1:]):
        assert start == end + 1


# ── DONE 인식 ───────────────────────────────────────────────────────────────

from piper_phase.fsm import _median_filter, compute_signals as _cs  # noqa: E402


def _tail(speeds, settle):
    """앞부분은 한 사이클, 끝 `speeds` 만 지정한 속도가 되도록 만든 에피소드."""
    state, action = _episode(park_frames=20, settle_frames=settle)
    return state, action


def test_one_jittery_frame_does_not_erase_done():
    """⚠ **실측에서 이것 하나로 DONE 이 통째로 날아갔다.**

    멈춰 선 팔도 엔코더 잡음으로 임계(2.0)를 살짝 넘는다 — 실측 2.4~4.2.
    뒤에서부터 **끊기지 않은** 정지 구간을 요구하면 그 한 프레임에서 스캔이 멎는다.
    bolt_two1 50개 중 16개가 그랬다.
    """
    ph = _phases(park_frames=20, settle_frames=12)
    assert ph[-1] == DONE


def test_an_episode_still_accelerating_gets_no_done():
    """⚠ **회귀** — 튐을 봐주는 규칙이 끝 프레임까지 번지자, 마지막 속도가 33.9 인
    에피소드에도 DONE 이 붙었다. 봐주기는 구간 **안쪽**만이다."""
    state, action = _episode(park_frames=20, settle_frames=0)
    # 마지막 두 프레임을 크게 움직이게 만든다
    state[-1, 0] = state[-2, 0] + 30.0
    action[-1, 0] = state[-1, 0]
    ph = label_episode(state, action, _P)[0]
    assert ph[-1] != DONE, "가속 중인데 DONE 을 준다"


def test_the_filter_does_not_let_the_last_frame_dominate():
    """가장자리를 `edge` 로 채우면 마지막 값이 창의 과반이라 **자기 자신이
    중앙값**이 된다 — 끝 프레임의 지터가 그대로 살아남는다. 반사로 채운다."""
    x = np.array([0.0, 0.0, 0.0, 0.0, 2.4])
    assert _median_filter(x, 5)[-1] < 2.0, "끝 프레임 지터가 안 걷힌다"


def test_a_real_move_survives_the_filter():
    """지터만 지워야 한다 — 실제 이동까지 뭉개면 정지 시점이 앞당겨진다."""
    x = np.array([0.0, 0.0, 30.0, 30.0, 30.0, 30.0, 0.0])
    assert _median_filter(x, 5)[3] > 20.0


def test_tolerated_frames_do_not_count_toward_the_length():
    """봐주기가 길이 요구를 대신하면, 튐만 있고 정지가 짧은 꼬리도 DONE 이 된다."""
    from piper_phase.fsm import Params as P

    p = P(fps=15.0)
    assert p.done_jitter < p.done_still, "봐주기가 요구 길이보다 크면 무의미해진다"


def test_the_viewer_has_a_colour_for_every_phase():
    """⚠ 색 배열이 짧으면 그 구간이 `undefined` 로 **투명하게** 그려진다 —
    라벨이 없는 것처럼 보이고, 아무 에러도 안 난다."""
    import re

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "config"
           / "phases.ts").read_text()
    body = src.split("const PHASE_COLORS = [", 1)[1].split("]", 1)[0]
    assert len(re.findall(r"'#[0-9a-fA-F]{6}'", body)) == len(PHASE_NAMES)
    # 이름 목록도 같은 파일에 있다 — 추론 텔레메트리는 인덱스가 아니라 이름으로
    # 색을 찾으므로, 둘이 어긋나면 단계 배지가 조용히 회색이 된다.
    names = src.split("const DEFAULT_NAMES = [", 1)[1].split("]", 1)[0]
    assert re.findall(r"'([A-Z]+)'", names) == list(PHASE_NAMES)


# ── 복귀를 **관측으로** 확인 ────────────────────────────────────────────────

def test_a_release_that_does_not_go_home_is_not_parking():
    """⚠ "마지막 놓기 뒤" 라는 **시간 규칙만으로는** 놓고 그냥 멈춘 것과 원점까지
    되돌아간 것이 구분되지 않는다.

    말단 좌표가 있으면 그건 관측으로 답할 수 있다. 실측(bolt_two1 50개):
    복귀 구간 끝은 시작 자세에서 최대 2.2cm, 긴 APPROACH 는 최소 13.5cm.
    """
    state, action = _episode(park_frames=25, settle_frames=12)
    # 복귀 구간을 **제자리**로 바꾼다 — 놓은 자리에 머문다
    tail = 25 + 12
    state[-tail:, 0] = state[-tail - 1, 0]
    action[-tail:, 0] = state[-tail - 1, 0]
    ph = label_episode(state, action, _P)[0]
    assert PARKING not in ph.tolist(), "돌아가지 않았는데 복귀로 본다"


def test_the_home_distance_is_available_as_a_signal():
    """사용자가 **눈으로 본** 그 경향이다 — 그래프로 볼 수 있어야 한다."""
    state, action = _episode(park_frames=25, settle_frames=12)
    sig = compute_signals(state, action, _P)
    assert sig.home_dist is not None
    assert sig.home_dist[0] == pytest.approx(0.0), "시작이 원점이 아니다"
    assert sig.home_dist[-1] < sig.home_dist.max() / 2, "끝에서 안 돌아왔다"


def test_without_the_urdf_the_time_rule_still_stands():
    """⚠ 서브모듈을 안 받은 체크아웃에서 PARKING 이 통째로 사라지는 편보다,
    예전만큼만 정확한 편이 낫다."""
    from piper_phase.fsm import Signals, _returns_home

    sig = Signals(speed=np.zeros(3), gripper_gap=np.zeros(3), gripper_cmd=np.zeros(3),
                  gripper_state=np.zeros(3), grip_rate=np.zeros(3),
                  hold=np.zeros(3, bool), home_dist=None)
    assert _returns_home(sig, 0, 3, _P) is True


def test_a_pick_next_to_home_is_not_mistaken_for_parking():
    """가까워진 것만 보면, 원점 근처에서 놓은 회차가 전부 복귀가 된다 —
    **그만큼 실제로 이동했는지**도 봐야 한다."""
    from piper_phase.fsm import Signals, _returns_home

    near = np.array([0.02, 0.01, 0.01])          # 내내 원점 근처
    sig = Signals(speed=np.zeros(3), gripper_gap=np.zeros(3), gripper_cmd=np.zeros(3),
                  gripper_state=np.zeros(3), grip_rate=np.zeros(3),
                  hold=np.zeros(3, bool), home_dist=near)
    assert _returns_home(sig, 0, 3, _P) is False
