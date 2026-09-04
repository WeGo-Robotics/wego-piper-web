"""관절 캘리브레이션 (refactor/05-joint-calibration.md).

원래 문제: 같은 `cal` dict 와 **서로 역함수인 변환식**이 `robot_manager.py` 안에
두 번 인라인으로 적혀 있었다. 한쪽 범위만 고치면 정규화/역정규화가 어긋나
**팔이 엉뚱한 위치로 간다** — 파킹 동작이라 바닥을 긁거나 관절 한계를 칠 수 있다.
"""

import math
import re
from pathlib import Path

import pytest

from app.core.joints import (
    JOINT_CALIBRATION,
    JOINT_ORDER,
    denormalize_joint,
    normalize_joint,
)

_REPO = Path(__file__).resolve().parents[2]
_PIPER_FOLLOWER = (
    _REPO / "vendor" / "lerobot_robot_piper" / "lerobot_robot_piper" / "piper_follower.py"
)


def _vendor_calibration() -> dict[str, tuple[int, int]]:
    """vendor 드라이버의 `MotorCalibration(id, ?, ?, range_min, range_max)` 를 읽는다."""
    src = _PIPER_FOLLOWER.read_text()
    found = re.findall(
        r'"(\w+)":\s*MotorCalibration\(\s*\d+,\s*\d+,\s*\d+,\s*(-?\d+),\s*(-?\d+)\s*\)', src
    )
    assert found, "piper_follower.py 에서 MotorCalibration 을 못 찾았다 (형식이 바뀌었나?)"
    return {name: (int(lo), int(hi)) for name, lo, hi in found}


def test_matches_vendor_driver():
    """⚠ 진짜 정본은 vendor 드라이버다 — 실제 추론·녹화가 그 값을 쓴다.

    vendor 는 외부 repo 스냅샷이라 여기서 고칠 수 없으므로 복제를 유지하되,
    스냅샷을 다시 뜬 뒤 값이 달라지면 여기서 잡는다.
    어긋난 채로 두면 백엔드가 보여주는 관절 값과 팔의 실제 위치가 달라진다.
    """
    assert JOINT_CALIBRATION == _vendor_calibration(), (
        "백엔드 JOINT_CALIBRATION 이 vendor/lerobot_robot_piper 와 다르다. "
        "vendor 스냅샷이 갱신됐다면 app/core/joints.py 를 맞춰야 한다."
    )


def test_joint_order_covers_calibration():
    """순서를 바꾸면 프론트 수동 제어가 엉뚱한 관절을 움직인다 (인덱스 매칭)."""
    assert set(JOINT_ORDER) == set(JOINT_CALIBRATION)
    assert JOINT_ORDER[-1] == "gripper"


@pytest.mark.parametrize("name", sorted(JOINT_CALIBRATION))
def test_roundtrip_is_stable(name: str):
    """정규화 → 역정규화가 raw 를 되돌려야 한다 (반올림 오차 범위 안).

    두 식이 따로 적혀 있던 것이 원래 위험이었다.
    """
    mn, mx = JOINT_CALIBRATION[name]
    span = mx - mn
    for i in range(0, 101):
        raw = mn + span * i // 100
        back = denormalize_joint(name, normalize_joint(name, raw))
        # 정규화가 소수 2자리로 반올림되므로 그만큼의 오차는 정상
        assert abs(back - raw) <= span / 100_00 + 1, f"{name}: {raw} → {back}"


def test_normalized_ranges():
    """관절은 -100..100, 그리퍼만 0..100."""
    for name, (mn, mx) in JOINT_CALIBRATION.items():
        lo, hi = normalize_joint(name, mn), normalize_joint(name, mx)
        expected = (0.0, 100.0) if name == "gripper" else (-100.0, 100.0)
        assert (lo, hi) == expected, f"{name} 스케일이 다르다: {(lo, hi)}"


def test_parking_targets_unchanged():
    """파킹 목표 raw 값 회귀 — 1 LSB 라도 달라지면 팔이 다른 곳으로 간다.

    값은 리팩터 전 코드로 실측한 것이다.

    ⚠ joint6 은 **일부러** 15000 → 0 으로 옮겼다. 옛 캘리브레이션이 비대칭이라
    (`-100000, 130000`) 정규화 0 이 물리 +15° 였고, 파킹이 그만큼 틀어진 채
    섰다. ±120 으로 바로잡으면서 파킹도 진짜 0° 에 선다.
    """
    from lerobot_robot_piper.motors.tables import INITIALIZE_POSITION

    expected = {
        "joint1": 0, "joint2": 0, "joint3": 0, "joint4": 0,
        "joint5": 22750, "joint6": 0, "gripper": 0,
    }
    actual = {n: denormalize_joint(n, v) for n, v in INITIALIZE_POSITION.items()}
    assert actual == expected


def test_frontend_joint_order_matches_backend():
    """프론트 `config/joints.ts` 순서가 백엔드와 같아야 한다.

    `ManualControlPanel` 이 `currentJoints[i]` 로 인덱스 매칭하므로,
    순서가 어긋나면 **슬라이더가 엉뚱한 관절을 움직인다.**
    """
    ts = (_REPO / "frontend" / "src" / "config" / "joints.ts").read_text()
    block = re.search(r"export const JOINTS: Joint\[\] = \[(.*?)\n\]", ts, re.S)
    assert block, "config/joints.ts 에서 JOINTS 를 못 찾았다"
    names = re.findall(r"name:\s*'([a-z0-9]+)'", block.group(1))
    assert tuple(names) == JOINT_ORDER, f"프론트 {names} vs 백엔드 {list(JOINT_ORDER)}"


def test_frontend_action_keys_have_pos_suffix():
    """백엔드 `/params/manual-action` 이 `joint1.pos` 형식을 기대한다."""
    ts = (_REPO / "frontend" / "src" / "config" / "joints.ts").read_text()
    pairs = re.findall(r"name:\s*'([a-z0-9]+)',\s*actionKey:\s*'([a-z0-9.]+)'", ts)
    assert len(pairs) == len(JOINT_ORDER)
    for name, action_key in pairs:
        assert action_key == f"{name}.pos", f"{name} → {action_key}"


def test_calibration_stays_inside_the_official_urdf():
    """⚠ **공식 한계 밖으로 나가면 안 된다.** 한때 joint6 이 `+130000` 이었는데,
    그 값은 piper_sdk 표에도 AgileX URDF 에도 없다 (둘 다 ±120). 정규화 +100 이
    130° 를 가리키니 **기구 한계 밖까지 명령할 수 있었다** — 펌웨어 소프트 한계는
    ±170/±180 이라 막아주지 않는다. 이 표가 j6 을 막는 유일한 것이었다.

    좁히는 것은 허용한다 (joint5 가 ±70 → ±65 로 일부러 좁혀져 있다).
    """
    urdf = Path(__file__).resolve().parents[2] / "vendor/agx_arm_urdf/piper/urdf/piper_description.urdf"
    if not urdf.exists():
        pytest.skip("URDF 스냅샷이 없다")
    text = urdf.read_text()
    for name, (mn, mx) in JOINT_CALIBRATION.items():
        if name == "gripper":
            continue
        m = re.search(rf'<joint name="{name}".*?lower="([-\d.eE]+)"\s+upper="([-\d.eE]+)"',
                      text, re.S)
        assert m, f"{name} 을 URDF 에서 못 찾았다"
        lo, hi = (math.degrees(float(m.group(i))) * 1000 for i in (1, 2))
        assert mn >= lo - 1 and mx <= hi + 1, (
            f"{name} 이 공식 한계({lo/1000:.0f}~{hi/1000:.0f}°) 밖이다: "
            f"{mn/1000:.0f}~{mx/1000:.0f}°")


def test_symmetric_joints_put_normalized_zero_at_physical_zero():
    """⚠ **비대칭이면 정규화 0 이 물리 0 이 아니다.** joint6 이 `-100~+130` 이라
    조그로 0 을 보내도 팔은 +15° 에 섰고, 파킹도 그만큼 틀어졌다. 관절의 물리
    범위가 원점을 사이에 두고 대칭이면 우리 표도 대칭이어야 한다.

    joint2(0~180)·joint3(-170~0)은 물리 범위 자체가 한쪽이라 제외한다.
    """
    for name, (mn, mx) in JOINT_CALIBRATION.items():
        if name in ("gripper", "joint2", "joint3"):
            continue
        assert mn == -mx, f"{name} 이 비대칭이다: {mn}~{mx}"
        assert denormalize_joint(name, 0) == 0, \
            f"{name} 의 정규화 0 이 물리 0 이 아니다: {denormalize_joint(name, 0)}"
