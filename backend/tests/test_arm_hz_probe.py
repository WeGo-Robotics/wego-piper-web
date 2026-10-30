"""tools/arm_hz_probe.py — 떨림이 명령 주기 계단인지, 명령과 무관한 진동인지 가르는가.

판정이 틀리면 처방이 틀린다: (가) 면 robotd 보간, (나) 면 서보·기구, (다) 면 정책·청크.
그래서 세 경우를 합성해서 판정이 맞게 나오는지 본다. 기록(shm 읽기)은 실기에서만 돈다.

⚠ 첫 판정 규칙은 "봉우리가 명령 배음이 아니면 (나)" 였다. 실기(2026-10-28 23시)의 스펙트럼은
평평해서 최고 봉우리가 몫 1% 였는데도 (나) 공진이라고 했다 — 실제로는 팔이 명령을 그대로 따른 (다).
"""

import sys
from pathlib import Path

import numpy as np

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "tools"))
import arm_hz_probe as P  # noqa: E402

FS, CMD_HZ, SECONDS = 100.0, 15.0, 30.0


def _intended(t):
    """느린 의도된 움직임 — 정규화 관절값 (T, 7)."""
    q = np.zeros((len(t), 7))
    q[:, 1] = -60 + 15 * np.sin(2 * np.pi * 0.25 * t)
    q[:, 2] = 80 + 12 * np.sin(2 * np.pi * 0.2 * t + 1)
    q[:, 4] = 30
    return q


def _wobbly(t):
    """정책이 3.2Hz 로 흔들리는 명령을 낸다."""
    q = _intended(t)
    q[:, 1] += 0.4 * np.sin(2 * np.pi * 3.2 * t)
    q[:, 2] += 0.3 * np.sin(2 * np.pi * 3.2 * t + 0.7)
    return q


def _data(actual_fn, with_action=True, command=_intended):
    rng = np.random.default_rng(0)
    st_t = np.arange(0, SECONDS, 1 / FS) + rng.normal(scale=2e-4, size=int(SECONDS * FS))
    st_t.sort()
    ac_t = np.arange(0, SECONDS, 1 / CMD_HZ)
    ac_q = command(ac_t)
    data = {"iface": "test", "state_t": st_t, "state_can_ns": (st_t * 1e9).astype(np.int64),
            "state_q": actual_fn(st_t, ac_t, ac_q),
            "action_t": ac_t if with_action else np.array([]),
            "action_q": ac_q if with_action else np.empty((0, 7))}
    return data


def _staircase(st_t, ac_t, ac_q, tau=0.015):
    """팔이 15Hz 계단을 1차 지연으로 따라간다 — robotd 가 명령이 올 때만 보내는 지금 모양."""
    held = ac_q[np.clip(np.searchsorted(ac_t, st_t, side="right") - 1, 0, len(ac_t) - 1)]
    out = np.empty_like(held)
    out[0] = held[0]
    for k in range(1, len(held)):
        a = 1 - np.exp(-(st_t[k] - st_t[k - 1]) / tau)
        out[k] = out[k - 1] + a * (held[k] - out[k - 1])
    return out


def _real_arm(st_t, ac_t, ac_q, tau=0.04):
    """실기에서 잰 팔 — 15Hz 계단은 ~15배 걸러 내고 몇 Hz 는 통과시킨다 (1차 지연 두 단)."""
    return _staircase(st_t, st_t, _staircase(st_t, ac_t, ac_q, tau), tau)


def _resonance(st_t, ac_t, ac_q):
    """명령은 매끄럽게 따라가는데 8.3Hz 로 따로 떤다 — 서보·기구 공진."""
    q = _intended(st_t)
    q[:, 1] += 0.08 * np.sin(2 * np.pi * 8.3 * st_t)
    q[:, 2] += 0.06 * np.sin(2 * np.pi * 8.3 * st_t + 0.5)
    return q


def test_a_command_rate_staircase_is_called_a_staircase():
    s = P.analyze(_data(_staircase))
    assert s["cmd_hz"] == CMD_HZ and s["state_hz"] == FS
    assert s["actual"]["cmd_share"] >= P.STAIRCASE_SHARE
    assert s["actual"]["peaks"][0]["cmd_harmonic"]
    assert s["verdict"].startswith("(가)")
    # 명령 쪽에도 같은 계단이 보인다 — 영차 유지로 옮겼으니까
    assert s["command"]["cmd_share"] >= P.STAIRCASE_SHARE


def test_a_resonance_away_from_the_command_rate_is_not_blamed_on_it():
    s = P.analyze(_data(_resonance))
    top = s["actual"]["peaks"][0]
    assert abs(top["hz"] - 8.3) < 0.5 and not top["cmd_harmonic"]
    assert s["actual"]["cmd_share"] < P.STAIRCASE_SHARE
    assert s["verdict"].startswith("(나)")
    # 관절 2·3 에 넣은 떨림이니 기여도 거기서 나온다
    c = s["joint_contrib_mm"]
    assert max(c[1], c[2]) > 10 * max(c[0], c[3], c[4], c[5])


def test_an_arm_that_follows_a_wobbly_command_points_at_the_policy():
    """실기 23시 측정과 같은 모양 — 계단은 걸러졌고 떨림은 명령에 이미 있다."""
    s = P.analyze(_data(_real_arm, command=_wobbly))
    assert s["actual"]["cmd_share"] < P.STAIRCASE_SHARE
    two_to_five = s["follow"][0]
    assert two_to_five["band"] == [2.0, 5.0] and two_to_five["actual_mm"] <= two_to_five["command_mm"]
    assert s["verdict"].startswith("(다)")


def test_a_flat_spectrum_is_not_called_a_resonance():
    """봉우리가 몫 몇 % 뿐이면 공진이 아니다 — 첫 규칙이 실기에서 이렇게 틀렸다."""
    s = P.analyze(_data(_real_arm, command=_wobbly))
    assert not s["verdict"].startswith("(나)")


def test_without_commands_it_only_reports_the_noise_floor():
    s = P.analyze(_data(_resonance, with_action=False))
    assert s["cmd_hz"] is None and "command" not in s
    assert "추론 중이 아님" in s["verdict"]
    assert "바닥 잡음" in P.report(s)        # 명령 줄 없이도 보고서가 만들어진다
