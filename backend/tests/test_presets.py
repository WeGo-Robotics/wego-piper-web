"""프리셋 스토어 (feature/parameter-presets.md 1~3단계).

원래 문제: 프리셋 성격의 저장소가 여섯 군데에 흩어져 있었고, 로봇만 서버에
이름 있는 프리셋이었다. 나머지는 **브라우저 localStorage 의 이름 없는 프리셋 1개** —
브라우저를 바꾸면 사라지고 팀에서 공유할 수 없다.
"""

import pytest

from app.services import presets
from app.services.presets import Preset, PresetError


@pytest.fixture(autouse=True)
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(presets, "PRESETS_ROOT", tmp_path / "presets")
    return tmp_path


def test_save_list_get_delete():
    presets.save("training", "야간-큐브", {"batch_size": 16}, scope="shared", note="메모")
    listed = presets.list_presets("training")
    assert [p["name"] for p in listed] == ["야간-큐브"]
    assert listed[0]["scope"] == "shared" and listed[0]["note"] == "메모"

    got = presets.get("training", "야간-큐브")
    assert got.values == {"batch_size": 16}
    assert got.updated_at  # 저장 시각이 기록된다

    assert presets.delete("training", "야간-큐브") is True
    assert presets.list_presets("training") == []
    assert presets.delete("training", "야간-큐브") is False


def test_domains_are_isolated():
    """로봇 프리셋과 학습 프리셋이 섞이면 안 된다."""
    presets.save("robot", "구성A", {"robot_type": "piper_follower"})
    presets.save("training", "구성A", {"batch_size": 8})
    assert presets.get("robot", "구성A").values != presets.get("training", "구성A").values


def test_missing_returns_none():
    assert presets.get("training", "없는것") is None


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "", "x" * 65, "nul\x00"])
def test_rejects_path_traversal(bad):
    """이름이 파일명이 되므로 경로 조작을 막아야 한다."""
    with pytest.raises(PresetError):
        presets.save("training", bad, {})


def test_rejects_unknown_scope():
    with pytest.raises(PresetError):
        presets.save("training", "x", {}, scope="global")


def test_korean_and_spaces_allowed():
    presets.save("training", "야간 조명 (실내)", {"a": 1})
    assert presets.get("training", "야간 조명 (실내)") is not None


# ── apply 리포트: 조용히 버리지 않는다 ──

_KNOWN = {"batch_size", "steps", "seed"}


def test_apply_drops_unknown_and_reports():
    """파라미터가 없어졌는데 조용히 무시하면 "저장한 값이 왜 다르지"가 된다."""
    p = Preset(domain="training", name="x", values={"batch_size": 16, "없어진키": 1})
    r = presets.apply(p, _KNOWN)
    assert r.values == {"batch_size": 16}
    assert r.unknown == ["없어진키"]


def test_apply_fills_missing_from_defaults():
    """파라미터가 새로 추가되면 기본값으로 채우고 알린다."""
    p = Preset(domain="training", name="x", values={"batch_size": 16})
    r = presets.apply(p, _KNOWN, defaults={"batch_size": 8, "steps": 1000})
    assert r.values == {"batch_size": 16, "steps": 1000}  # 저장값이 이긴다
    assert r.missing == ["steps"]


def test_apply_reports_policy_mismatch():
    """`act` 프리셋을 `smolvla` 에서 열면 공통 항목만 유효하다."""
    p = Preset(domain="training", name="x", values={"batch_size": 8}, policy_type="act")
    r = presets.apply(p, _KNOWN, current_policy_type="smolvla")
    assert r.policy_mismatch == {"saved": "act", "current": "smolvla"}
    # 정책이 같으면 알리지 않는다
    assert presets.apply(p, _KNOWN, current_policy_type="act").policy_mismatch is None


# ── 도메인 계약 ──

def test_training_preset_excludes_execution_targets():
    """**튜닝만 담고 실행 대상은 담지 않는다** — 담으면 재사용이 안 된다."""
    from app.routers.training import preset_keys

    keys = preset_keys()
    for excluded in ("dataset_repo_id", "output_dir", "pretrained_path", "policy_repo_id"):
        assert excluded not in keys, f"{excluded} 는 실행 대상이라 프리셋에 담으면 안 된다"
    for tuning in ("batch_size", "steps", "optimizer_type", "policy_params", "amp"):
        assert tuning in keys


def test_training_preset_keys_derive_from_request_model():
    """`TrainStartRequest` 에서 파생돼야 사본이 안 생긴다."""
    from app.routers.training import PRESET_EXCLUDED, TrainStartRequest, preset_keys

    assert preset_keys() == set(TrainStartRequest.model_fields) - PRESET_EXCLUDED


def test_robot_preset_roundtrip_keeps_shape():
    """로봇 프리셋 값 구조가 이관 전과 같아야 한다 (프론트가 그대로 읽는다)."""
    values = {
        "robot_type": "piper_follower",
        "config_name": "1 Leader / 1 Follower",
        "arms": [{"slot": "follower_1", "can_name": "can_follower1",
                  "bus_info": "1-1", "role": "follower", "config": {}}],
    }
    presets.save("robot", "작업대A", values, scope="device")
    assert presets.get("robot", "작업대A").values == values
