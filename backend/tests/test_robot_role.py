"""팔 역할(leader/follower) 자동 판별.

## 고친 버그

마스터가 되는 길은 **둘**이다:

1. `ctrl_mode == 0x06` (Linkage teaching) 을 보고한다
2. **RX 가 없다** — 마스터(示教输入臂)는 주기 피드백(0x2Ax)을 송신하지 않는다

그런데 `role` 은 1번만 보고 정해져서, 2번으로 잡힌 마스터는
**`master_slave=master` 인데 `role=follower`** 로 떴다.

전원을 껐다 켜면 마스터 설정이 풀려 Standby(0x00)를 보고하므로
([메모리] Piper 마스터/슬레이브는 설정해야 켜지는 모드), 실제로 흔히 걸린다.
"""

import pytest

from app.services.robot_manager import ArmInfo


def _arm(monkeypatch, *, mode_int, rx_changes: bool) -> ArmInfo:
    """가짜 팔 — piper SDK 없이 판별 로직만 돌린다."""
    arm = ArmInfo(iface="can0", bus_info="1-1:1.0")
    arm._piper = object()          # connect 검사 통과용

    monkeypatch.setattr(ArmInfo, "refresh_ctrl_mode", lambda self: mode_int)

    # RX 가 변하면 슬레이브(피드백 송신 중), 안 변하면 마스터
    seq = iter([100, 200] if rx_changes else [100, 100])
    monkeypatch.setattr(
        "app.services.robot_manager._read_can_rx", lambda _iface: next(seq)
    )
    monkeypatch.setattr("app.services.robot_manager.time.sleep", lambda _s: None)
    return arm


def _detect(arm: ArmInfo, mode_int) -> None:
    """`connect()` 안의 판별 부분과 같은 순서."""
    arm._classify_master(mode_int)
    arm.role = "leader" if arm.is_master else "follower"


@pytest.mark.parametrize(
    "mode_int,rx_changes,expect_master,expect_role",
    [
        # 명시적 마스터 — ctrl_mode 0x06
        (0x06, True, True, "leader"),
        # **회귀**: 전원 재투입으로 설정이 풀려 Standby 인데 RX 가 없다 → 마스터
        (0x00, False, True, "leader"),
        # 슬레이브 — 피드백을 계속 송신한다
        (0x00, True, False, "follower"),
        (0x01, True, False, "follower"),
        # 모드를 못 읽어도 RX 규칙은 산다
        (None, False, True, "leader"),
        (None, True, False, "follower"),
    ],
)
def test_role_follows_master_detection(
    monkeypatch, mode_int, rx_changes, expect_master, expect_role
):
    arm = _arm(monkeypatch, mode_int=mode_int, rx_changes=rx_changes)
    _detect(arm, mode_int)
    assert arm.is_master is expect_master
    assert arm.role == expect_role
    # 화면에 나가는 두 값이 서로 모순되지 않아야 한다 — 이게 사용자가 본 증상이다
    d = arm.to_dict()
    assert (d["master_slave"] == "master") == (d["role"] == "leader"), (
        f"master_slave={d['master_slave']} 인데 role={d['role']}"
    )


def test_refresh_mode_does_not_clobber_manual_role(monkeypatch):
    """`/robots/current` 폴링이 사용자가 고른 역할을 되돌리면 안 된다."""
    arm = _arm(monkeypatch, mode_int=0x00, rx_changes=False)   # RX 없음 → 마스터로 잡힘
    arm.role = "follower"                                       # 사용자가 직접 지정
    arm.refresh_mode()
    assert arm.is_master is True          # 하드웨어 판별은 갱신되지만
    assert arm.role == "follower"         # 사용자의 선택은 남는다


# ── 프리셋이 담는 범위 ──────────────────────────────────────────────────────

def test_preset_saves_registered_arms_not_only_slotted(monkeypatch, tmp_path):
    """**회귀** — 등록만 한 팔도 프리셋에 담겨야 한다.

    실제 사용 흐름은 스캔 → 연결 → 등록이고, 이 경로는 `ready` 만 세우고
    `slot` 은 건드리지 않는다(슬롯 배정은 별도 구성 단계). `slot` 만 보면
    등록을 다 끝낸 사용자가 `arms: []` 인 빈 프리셋을 받는다.
    """
    from app.services import presets as preset_store
    from app.services.robot_manager import RobotManager

    monkeypatch.setattr(preset_store, "PRESETS_ROOT", tmp_path / "presets")

    rm = RobotManager()
    arm = ArmInfo(iface="can1", bus_info="3-3:1.0")
    arm.role, arm.ready = "leader", True      # 등록만 함 — slot 은 None
    rm.arms["can1"] = arm

    rm.save_preset("사무실")
    saved = preset_store.get("robot", "사무실")
    assert len(saved.values["arms"]) == 1, "등록된 팔이 프리셋에서 빠졌다"
    assert saved.values["arms"][0]["ready"] is True


def test_preset_refuses_when_nothing_configured(monkeypatch, tmp_path):
    """빈 프리셋을 조용히 저장하지 않는다 — 그게 '저장은 됐는데 안 불러와진다'였다."""
    from app.services import presets as preset_store
    from app.services.robot_manager import RobotManager

    monkeypatch.setattr(preset_store, "PRESETS_ROOT", tmp_path / "presets")

    rm = RobotManager()
    rm.arms["can0"] = ArmInfo(iface="can0", bus_info="3-4:1.0")   # 스캔만 된 상태
    with pytest.raises(ValueError, match="저장할 구성이 없습니다"):
        rm.save_preset("빈것")


def test_page_lists_are_disjoint():
    """**회귀** — 로봇 페이지의 세 목록이 겹치면 같은 팔을 두 번 등록하게 된다.

    프리셋을 불러오면 `ready` 만 서고 연결은 안 되므로 `ready && !connected` 가
    생기는데, 예전 `unconnectedArms` 는 `ready` 를 안 봐서 그 팔이 1단계와
    사용 가능 목록에 동시에 떴다. (카메라 페이지에서도 똑같은 실수를 했었다.)
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    line = next(ln for ln in src.splitlines() if "const unconnectedArms" in ln)
    assert re.search(r"!a\.connected\s*&&\s*!a\.ready", line), (
        f"unconnectedArms 가 ready 를 걸러내지 않는다: {line.strip()}"
    )
    conn = next(ln for ln in src.splitlines() if "const connectedArms" in ln)
    assert "!a.ready" in conn, f"connectedArms 가 ready 를 걸러내지 않는다: {conn.strip()}"
