"""계획 안 순간이동 고쳐 잇기 — wrapper/jump_repair.py 와 lerobot_wrapper 배선.

SmolVLA-S 가 정지 자세에서 출발하는 청크는 몇 칸 머물다 한 스텝에 60~87mm 뛰었다(2026-10-29 실기).
여기 테스트는

1. 튄 곳만 고치는가 — 한 스텝 한도 안으로, 원래 계획에 합류, 정상 동작·그리퍼는 그대로
2. PIPER 팔에만 붙는가 — 다른 팔·기구학 없음이면 꺼진 채 추론은 돈다
3. wrapper 가 정말 그걸 쓰는가 — 앙상블·병합 전에, 디버그 기록은 고치기 전 그대로
"""

import re
import sys
from pathlib import Path

import numpy as np
import pytest

from conftest import code_only

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "wrapper"))
import jump_repair as JR  # noqa: E402
from jump_repair import JumpRepair, piper_arms  # noqa: E402

# 앞 3축이 곧 말단 mm 인 가짜 팔 — 판정·잇기만 본다 (PIPER 지오메트리는 맨 아래 한 번)
_FK = lambda q: np.asarray(q, dtype=float)[:, :3]  # noqa: E731
_ARM = [0, 1, 2, 3, 4, 5]


def _repairer(fps=15):
    r = JumpRepair()
    r.attach(_FK, [_ARM], fps)
    return r


def _chunk(xs, dim=7):
    """x 축 위치 열 → (n, dim). 그리퍼(마지막 열)는 칸마다 다른 값."""
    out = np.zeros((len(xs), dim))
    out[:, 0] = xs
    out[:, -1] = np.arange(len(xs)) * 3.0
    return out


def _steps(y):
    return np.linalg.norm(np.diff(_FK(y), axis=0), axis=1)


# 정지 5칸 → 한 스텝 60 → 20/스텝 (실기에서 본 모양)
_DEPART = [0.0] * 5 + [60.0 + 20.0 * k for k in range(20)]


# ── 1. 고치기 ───────────────────────────────────────────────────────────

def test_a_departure_jump_is_bridged_under_the_limit():
    q = _chunk(_DEPART)
    y, fixed = _repairer().repair(q)
    assert fixed and fixed[0][0] == 4, "정지 마지막 칸에서 뛰었다"
    assert _steps(y).max() <= 35.0 + 1e-9
    i, n = fixed[0]
    # 튀기 전과 합류한 뒤는 원래 계획 그대로
    assert np.array_equal(y[:i + 1], q[:i + 1])
    assert np.array_equal(y[i + n:], q[i + n:])
    # 머물다 뛰는 대신 나눠서 가속한다 — 한 스텝 사이 속도 변화가 원래(60)의 1/3 아래
    assert np.abs(np.diff(_steps(y))).max() < np.abs(np.diff(_steps(q))).max() / 3


def test_normal_motion_is_left_alone():
    # 가속해 29/스텝까지 (시연 최대) — 한 칸도 바뀌면 안 된다
    xs = np.cumsum([0, 1, 4, 9, 16, 24, 29, 29, 29, 25, 15, 5, 0, 0])
    q = _chunk(xs)
    y, fixed = _repairer().repair(q)
    assert not fixed and np.array_equal(y, q)


def test_the_gripper_is_never_touched():
    q = _chunk(_DEPART)
    y, fixed = _repairer().repair(q)
    assert fixed
    assert np.array_equal(y[:, -1], q[:, -1])


def test_the_limit_follows_the_control_rate():
    """한도는 속도다 — 30Hz 에서는 한 스텝 17.5mm. 같은 25mm 스텝이 15Hz 에선 정상, 30Hz 에선 튐."""
    xs = [0.0] * 5 + [25.0 + 10.0 * k for k in range(20)]
    assert not _repairer(15).repair(_chunk(xs))[1]
    y, fixed = _repairer(30).repair(_chunk(xs))
    assert fixed and _steps(y).max() <= 17.5 + 1e-9
    assert _repairer(30).max_n == 24


def test_a_jump_into_the_last_step_has_nowhere_to_rejoin():
    """끝에 걸린 곳은 한 번만 해 보고 만다 — 같은 자리를 되풀이해 고친 척하지 않는다."""
    xs = [0.0, 1.0, 2.0, 3.0, 80.0]
    y, fixed = _repairer().repair(_chunk(xs))
    assert np.array_equal(y, _chunk(xs))
    xs = [0.0, 1.0, 2.0, 3.0, 80.0, 100.0]
    _, fixed = _repairer().repair(_chunk(xs))
    assert len(fixed) == len(set(i for i, _ in fixed))


def test_off_or_unattached_is_a_plain_copy():
    q = _chunk(_DEPART)
    r = _repairer()
    assert r.update_param("jump_repair", False) and not r.enabled
    y, fixed = r.repair(q)
    assert not fixed and np.array_equal(y, q)
    assert not JumpRepair().enabled, "팔을 붙이기 전엔 안 돈다"
    assert not r.update_param("chunk_blend_steps", 3)


def test_each_arm_is_judged_on_its_own():
    left, right = list(range(0, 6)), list(range(7, 13))
    q = np.zeros((len(_DEPART), 14))
    q[:, 0] = np.arange(len(_DEPART)) * 10.0    # 왼팔은 등속 10/스텝
    q[:, 7] = _DEPART                          # 오른팔만 튄다
    r = JumpRepair()
    r.attach(_FK, [left, right], 15)
    y, fixed = r.repair(q)
    assert fixed
    assert np.array_equal(y[:, left], q[:, left])
    assert np.linalg.norm(np.diff(y[:, 7:10], axis=0), axis=1).max() <= 35.0 + 1e-9


# ── 2. PIPER 팔만 ───────────────────────────────────────────────────────

_ONE = [f"joint{k}.pos" for k in range(1, 7)] + ["gripper.pos"]


def test_piper_arms_are_found_by_joint_names():
    assert piper_arms("piper_follower_shm", _ONE) == [[0, 1, 2, 3, 4, 5]]
    bi = [f"left_{n}" for n in _ONE] + [f"right_{n}" for n in _ONE]
    assert piper_arms("bi_piper_follower", bi) == [[0, 1, 2, 3, 4, 5], [7, 8, 9, 10, 11, 12]]


def test_other_arms_get_nothing():
    so101 = ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos",
             "wrist_flex.pos", "wrist_roll.pos", "gripper.pos"]
    assert piper_arms("so101_follower", so101) == []
    assert piper_arms("so101_follower", _ONE) == [], "관절 이름이 같아도 PIPER 가 아니면 지오메트리가 다르다"
    assert piper_arms("piper_follower", _ONE[:5]) == []


def test_the_real_fk_speaks_millimetres():
    pytest.importorskip("piper_robot.kinematics")
    fk = JR.piper_fk()
    assert fk is not None
    e = fk(np.zeros((2, 6)))
    assert e.shape == (2, 3)
    assert 50 < np.linalg.norm(e[0]) < 1000, "link6 는 바닥에서 수백 mm — m 로 나오면 한도가 1000배 어긋난다"


# ── 3. 배선 ─────────────────────────────────────────────────────────────

_WRAPPER = (_REPO / "wrapper" / "lerobot_wrapper.py").read_text()


def test_wrapper_repairs_before_the_ensemble_and_the_merge():
    src = code_only(_WRAPPER)
    assert "from jump_repair import JumpRepair" in src
    at = src.index("_jump_repair.repair(")
    # 델타는 절대로 되돌린 뒤에 (말단 위치는 절대 관절에서), 앙상블·병합·RTC 자르기보다 먼저
    assert src.index("_delta.restore(") < at < src.index("with _action_lock:", at)
    assert at < src.index("_ensembler.add(obs_step") < src.index("_chunk_merger.merge(")


def test_the_debug_record_keeps_the_chunk_as_the_policy_made_it():
    src = code_only(_WRAPPER)
    assert re.search(r"recorded_queue = new_queue\s*repaired", src)
    rec = src[src.index("recorder.record_inference("):]
    assert "for (_, a_dict) in recorded_queue]" in rec[:400] and "repaired=repaired" in rec[:400]


def test_both_param_paths_reach_the_repairer_and_the_arm_is_attached():
    src = code_only(_WRAPPER)
    listener = src[src.index("def param_listener"):src.index("def _resolve_policy_path")]
    assert "_jump_repair.update_param(key, value)" in listener
    overrides = src[src.index("for key, value in config_overrides.items():"):src.index("preprocessor = None")]
    assert "_jump_repair.update_param(key, value)" in overrides
    assert re.search(r"_jump_repair\.attach\(_fk, _arms, args\.fps\)", src)
    assert src.index("motor_names = list(robot.action_features") < src.index("piper_arms(args.robot_type, motor_names)")


def test_spec_screen_and_wrapper_agree_on_on():
    """켬이 기본 — 화면 밖에서 띄운 추론(플러그인·외부 API)도 화면과 같게 돈다."""
    from app.core.cli_mapping import OVERRIDE_KEYS
    from app.core.inference_params import PARAM_SPEC
    from app.services.param_bridge import BOOL_PARAMS

    spec = PARAM_SPEC["jump_repair"]
    assert spec["kind"] == "bool" and spec["default"] is True is JR.DEFAULT_ON
    assert not spec.get("scoped"), "정책과 무관하다 — ACT 도 가끔 튄다"
    assert "jump_repair" in BOOL_PARAMS and "jump_repair" in OVERRIDE_KEYS
    page = (_REPO / "frontend" / "src" / "pages" / "InferencePage.tsx").read_text()
    assert re.search(r"jump_repair: true,", page)
    assert "handleParamChange('jump_repair', e.target.checked)" in page
