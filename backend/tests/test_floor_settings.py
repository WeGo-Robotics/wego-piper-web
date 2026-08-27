"""바닥 필터 설정 — 저장·적용·경계 (refactor/robotd-safety.md).

설정이 robotd 안에 있고 게이트웨이는 RPC 창구일 뿐이다. 여기서 지키는 것은
**설정과 실제 걸린 필터가 어긋나지 않는 것**이다.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("piper_robot")
from piper_robot import safety_store as ST  # noqa: E402
from piper_robot.safety import FloorConfig  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ST, "PATH", tmp_path / "safety.json")
    return ST.PATH


# ── 저장 ─────────────────────────────────────────────────────────────────────

def test_no_file_means_the_measured_default(store):
    """설정이 없으면 **기본값**이다 — 실측으로 정한 안전한 쪽."""
    assert not store.exists()
    assert ST.load() == FloorConfig()


def test_a_broken_file_falls_back_instead_of_crashing(store):
    """⚠ 여기서 예외를 던지면 **robotd 가 기동하지 못한다.** 설정 파일 하나가
    깨졌다고 팔을 통째로 못 쓰게 되면 안 된다."""
    store.write_text("{ 이건 json 이 아니다")
    assert ST.load() == FloorConfig()


def test_a_round_trip_keeps_the_value(store):
    ST.save(replace(FloorConfig(), enabled=False, min_z=-0.075))
    got = ST.load()
    assert got.enabled is False
    assert got.min_z == pytest.approx(-0.075)


def test_only_the_editable_keys_are_written(store):
    """`sweep_steps`·`allow_escape` 는 사람이 만질 것이 아니다 — 후자를 끄면
    **팔이 바닥에 박힌 채 굳어 복구가 불가능해진다.**"""
    ST.save(FloorConfig())
    assert set(json.loads(store.read_text())["floor"]) == set(ST.EDITABLE)


def test_an_unknown_key_is_ignored(store):
    """UI 의 오타가 안전 파라미터를 바꾸면 안 된다."""
    out = ST._apply(FloorConfig(), {"allow_escape": False, "sweep_steps": 1})
    assert out.allow_escape is True
    assert out.sweep_steps == FloorConfig().sweep_steps


def test_a_non_numeric_limit_is_ignored_not_crashed(store):
    out = ST._apply(FloorConfig(), {"min_z": "아래로"})
    assert out.min_z == FloorConfig().min_z


# ── 경계 ─────────────────────────────────────────────────────────────────────

def test_the_limit_is_clamped_to_a_sane_range():
    """⚠ 아주 낮은 값은 **필터를 끈 것과 같다.** 그럴 거면 `enabled` 를 꺼야
    한다 — 화면에는 켜져 있는데 아무것도 안 막는 상태가 제일 위험하다."""
    assert ST.clamp_min_z(-99.0) == ST.MIN_Z_FLOOR
    assert ST.clamp_min_z(99.0) == ST.MIN_Z_CEIL
    assert ST.clamp_min_z(-0.04) == pytest.approx(-0.04)


def test_the_ceiling_still_lets_the_arm_exist():
    """상한이 팔의 구조적 최저점보다 위면 **어떤 자세도 통과 못 한다.**"""
    import numpy as np
    from piper_robot import kinematics as K

    # link1 은 자세와 무관하게 고정이고 그게 팔의 구조적 바닥이다
    floor_of_the_arm = float(K.lowest_z(K.norm_to_rad(np.zeros((1, 6))))[0])
    assert ST.MIN_Z_CEIL <= floor_of_the_arm, (
        f"상한 {ST.MIN_Z_CEIL}m 가 팔의 최저점 {floor_of_the_arm:.3f}m 보다 위다")


# ── 단위 ─────────────────────────────────────────────────────────────────────

def test_the_ui_speaks_centimetres():
    """사람은 미터로 생각하지 않는다. 변환은 **한 곳**에서만 한다 —
    양쪽에서 바꾸면 언젠가 100배 틀린 값이 저장된다."""
    d = ST.as_dict(replace(FloorConfig(), min_z=-0.04))
    assert d["min_z_cm"] == pytest.approx(-4.0)
    assert d["range_cm"] == [ST.MIN_Z_FLOOR * 100, ST.MIN_Z_CEIL * 100]


def test_the_conversion_lives_only_in_the_hub():
    """`min_z_cm` → `min_z` 를 두 곳에서 하면 어긋난다. 허브에만 있어야 한다."""
    hub = (REPO / "robot" / "piper_robot" / "hub.py").read_text()
    assert 'out.pop("min_z_cm")' in hub
    store_src = (REPO / "robot" / "piper_robot" / "safety_store.py").read_text()
    assert "min_z_cm" not in store_src.split("def as_dict")[0], \
        "저장소가 cm 를 또 변환한다"


# ── 적용 ─────────────────────────────────────────────────────────────────────

def test_changing_the_setting_reaches_the_live_bridges(store, monkeypatch):
    """⚠ 저장만 하고 적용을 안 하면 **다음 연결까지 안 바뀐다.** 사용자는
    화면에서 바꿨으니 바뀐 줄 아는데 팔에는 옛 값이 걸려 있다."""
    from piper_robot import publish

    mgr = publish.ArmBridgeManager()

    class _FakeBridge:
        safety = publish.SafetyConfig()
    mgr.bridges["can0"] = _FakeBridge()

    mgr.set_floor({"min_z": -0.09})
    assert mgr.bridges["can0"].safety.floor.min_z == pytest.approx(-0.09)
    assert mgr.floor_config().min_z == pytest.approx(-0.09)


def test_a_new_arm_inherits_the_saved_setting(store):
    """팔을 뽑았다 꽂았다고 **꺼둔 필터가 다시 켜지면** 안 된다."""
    from piper_robot import publish

    ST.save(replace(FloorConfig(), enabled=False, min_z=-0.06))
    mgr = publish.ArmBridgeManager()
    assert mgr.floor_config().enabled is False
    assert mgr.floor_config().min_z == pytest.approx(-0.06)


def test_the_daemon_exposes_both_rpc_methods():
    """화이트리스트에 없으면 게이트웨이가 불러도 '알 수 없는 메서드'로 튕긴다."""
    src = (REPO / "daemons" / "robotd.py").read_text()
    assert '"get_safety"' in src and '"set_safety"' in src
