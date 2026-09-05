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

pytest.importorskip("piper_robot")
from piper_robot.arm import Arm  # noqa: E402

# robotd 분리 후 판별이 **두 쪽으로 갈렸다.**
#   - `is_master` = 팔에 물어본 **사실** → 데몬(`piper_robot.arm.Arm`)
#   - leader/follower = 그 사실의 **해석** → 게이트웨이(`ArmInfo`)
# 그래서 아래 헬퍼도 둘로 나뉜다. 경계가 흐려지면 같은 값이 두 프로세스에 생긴다.


def _device_arm(monkeypatch, *, mode_int, rx_changes: bool) -> Arm:
    """데몬 쪽 가짜 팔 — piper SDK 없이 판별 로직만 돌린다.

    ⚠ `rx_changes` 는 이제 **"슬레이브 주기 피드백(0x2Ax)이 보이는가"** 다.
      예전에는 RX 개수 증가였는데, 그 규칙은 조작 중인 마스터가 보내는
      제어지령(0x15x)까지 '트래픽 있음' 으로 세어 리더를 슬레이브로 읽었다
      (test_master_classification.py). 대역을 보는 지금은 둘이 갈린다.
    """
    arm = Arm(iface="can0", bus_info="1-1:1.0")
    arm._piper = object()          # connect 검사 통과용

    monkeypatch.setattr(Arm, "refresh_ctrl_mode", lambda self: mode_int)

    groups = {"slave_fb": 120 if rx_changes else 0, "master_fb": 0,
              "master_ctrl": 0, "driver": 0, "other": 0}
    monkeypatch.setattr("piper_robot.arm.sniff_can_ids",
                        lambda iface, duration: {"groups": groups})
    # ⚠ 조용한 팔은 **물어봐서** 마스터와 죽은 팔을 가른다. 여기 팔은 살아 있다 —
    #   이 파일이 보는 것은 역할 배정이지 생사 판정이 아니다.
    monkeypatch.setattr(Arm, "_probe_alive", lambda self, wait=0.4: True)
    return arm


def _detect(monkeypatch, mode_int, rx_changes: bool) -> ArmInfo:
    """데몬이 판별하고 게이트웨이가 해석하는 **실제 순서** 그대로."""
    device = _device_arm(monkeypatch, mode_int=mode_int, rx_changes=rx_changes)
    device._classify_master(mode_int)

    gw = ArmInfo(iface="can0", bus_info="1-1:1.0")
    gw.absorb(device.to_dict())
    # `ArmInfo.connect()` 가 하는 해석과 같다
    if gw.role == "unknown" and gw.is_master is not None:
        gw.role = "leader" if gw.is_master else "follower"
    return gw


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
    arm = _detect(monkeypatch, mode_int, rx_changes)
    assert arm.is_master is expect_master
    assert arm.role == expect_role
    # 화면에 나가는 두 값이 서로 모순되지 않아야 한다 — 이게 사용자가 본 증상이다
    d = arm.to_dict()
    assert (d["master_slave"] == "master") == (d["role"] == "leader"), (
        f"master_slave={d['master_slave']} 인데 role={d['role']}"
    )


def test_refresh_mode_does_not_clobber_manual_role(monkeypatch):
    """`/robots/current` 폴링이 사용자가 고른 역할을 되돌리면 안 된다.

    데몬이 `is_master` 를 갱신해 보내도, 게이트웨이가 `absorb()` 에서 **역할은
    건드리지 않아야** 한다 — 그게 사실과 해석을 나눈 이유다.
    """
    device = _device_arm(monkeypatch, mode_int=0x00, rx_changes=False)  # RX 없음 → 마스터
    device._classify_master(0x00)

    gw = ArmInfo(iface="can0", bus_info="1-1:1.0")
    gw.role = "follower"                  # 사용자가 직접 지정
    gw.absorb(device.to_dict())
    assert gw.is_master is True           # 하드웨어 판별은 갱신되지만
    assert gw.role == "follower"          # 사용자의 선택은 남는다


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


def test_a_connected_port_cannot_be_attached_twice():
    """**회귀 계보** — 예전 세 목록(1단계/2단계/사용 가능)이 겹쳐 같은 팔을 두 번
    등록한 적이 있다. 2026-09-03 개편(포트/로봇)에서는 포트 카드의 [연결]이
    `port.connected` 로 잠기는 것이 그 방어다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    attach = src.split("handleAttach(port.iface)", 1)[1][:200]
    assert "port.connected" in attach, "연결된 포트의 [연결]이 안 잠긴다 — 이중 연결"
    assert "a.ready || a.connected" in src, "로봇 패널이 연결-미등록 팔을 놓친다"


def test_ready_card_can_reconnect():
    """끊긴 팔을 다시 붙일 길이 사용 가능 카드에도 있어야 한다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    robots_section = src.split("robotArms.map", 1)[1]
    assert "!arm.connected" in robots_section and "handleAttach" in robots_section, (
        "등록된 팔이 끊겼을 때 다시 연결할 방법이 없다"
    )


def test_preset_load_scans_first(monkeypatch, tmp_path):
    """**회귀** — 스캔 안 하고 프리셋을 누르면 `self.arms` 가 비어 전부 건너뛴다.

    "스캔 먼저"를 사용자가 알고 있어야 하는 것은 UI 의 잘못이다.
    `restore_session` 도 스캔부터 한다.
    """
    from app.services import presets as preset_store
    from app.services.robot_manager import RobotManager

    monkeypatch.setattr(preset_store, "PRESETS_ROOT", tmp_path / "presets")
    preset_store.save("robot", "p", {"robot_type": None, "config_name": None, "arms": [
        {"slot": None, "can_name": "can1", "bus_info": "3-3:1.0",
         "role": "leader", "ready": True, "config": {}}]})

    rm = RobotManager()                      # arms 비어 있음 = 스캔 전
    found = ArmInfo(iface="can1", bus_info="3-3:1.0")
    monkeypatch.setattr(RobotManager, "scan",
                        lambda self: self.arms.update({"can1": found}))
    monkeypatch.setattr(ArmInfo, "connect",
                        lambda self: (setattr(self, "connected", True), (True, "OK"))[1])

    out = rm.load_preset("p")
    assert out["applied"] == ["can1"], "스캔을 안 해서 팔을 못 찾았다"
    assert found.ready is True


def test_preset_load_reports_what_it_could_not_apply(monkeypatch, tmp_path):
    """**회귀** — 하나도 적용 못 했는데 "적용됨"이라고 뜨면 안 된다."""
    from app.services import presets as preset_store
    from app.services.robot_manager import RobotManager

    monkeypatch.setattr(preset_store, "PRESETS_ROOT", tmp_path / "presets")
    preset_store.save("robot", "p", {"robot_type": None, "config_name": None, "arms": [
        {"slot": None, "can_name": "can9", "bus_info": "9-9:9.9",
         "role": "leader", "ready": True, "config": {}}]})

    rm = RobotManager()
    monkeypatch.setattr(RobotManager, "scan", lambda self: None)   # 아무것도 못 찾음

    out = rm.load_preset("p")
    assert out["applied"] == []
    assert out["missing"] == ["can9"], "못 찾은 팔을 알려주지 않는다"


def test_frontend_message_uses_the_actual_result():
    """프리셋에 적힌 팔 수를 세면 '적용 안 됐는데 적용됨'이 된다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "frontend" / "src" / "pages" / "RobotsPage.tsx").read_text()
    handler = src.split("const handlePresetLoad", 1)[1].split("const handlePresetDelete", 1)[0]
    assert "d.applied" in handler or "applied" in handler, "실제 적용 결과를 안 본다"
    assert "d.arms?.length" not in handler, "프리셋에 적힌 팔 수를 세고 있다"


# ── 로봇 타입 선택의 수명 ────────────────────────────────────────────────────

def test_select_persists_to_session():
    """**회귀** — 타입을 메모리에만 두면 리로드마다 날아간다.

    팔은 등록돼 있는데 타입만 비어서 추론 시작이 "로봇이 선택되지 않았습니다"로
    막혔고, 원인을 찾기 어려웠다.
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "backend" / "app" / "routers" / "robots.py").read_text()
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "select_type"
    )
    body = ast.unparse(fn)
    assert "save_session" in body, "선택을 세션에 저장하지 않는다"


def test_preset_does_not_wipe_the_robot_type(monkeypatch, tmp_path):
    """**회귀** — 프리셋에 값이 없으면 현재 값을 **지우면 안 된다.**

    저장 당시 타입이 비어 있었으면 null 이 저장되는데, 그걸 그대로 대입하면
    프리셋이 상태를 복원하는 게 아니라 지운다. 실제로 프리셋을 부른 뒤
    추론 시작이 막혔다.
    """
    from app.services import presets as preset_store
    from app.services.robot_manager import RobotManager

    monkeypatch.setattr(preset_store, "PRESETS_ROOT", tmp_path / "presets")
    preset_store.save("robot", "빈타입", {
        "robot_type": None, "config_name": None, "arms": [],
    })

    rm = RobotManager()
    rm.selected_type = "piper_follower"
    rm.config_name = "1 Leader / 1 Follower"
    monkeypatch.setattr(RobotManager, "scan", lambda self: None)

    rm.load_preset("빈타입")
    assert rm.selected_type == "piper_follower", "프리셋이 로봇 타입을 지웠다"
    assert rm.config_name == "1 Leader / 1 Follower"


def test_preset_with_a_type_still_applies_it(monkeypatch, tmp_path):
    """반대 방향 — 값이 있으면 당연히 적용돼야 한다."""
    from app.services import presets as preset_store
    from app.services.robot_manager import RobotManager

    monkeypatch.setattr(preset_store, "PRESETS_ROOT", tmp_path / "presets")
    preset_store.save("robot", "있음", {
        "robot_type": "so_follower", "config_name": "X", "arms": [],
    })

    rm = RobotManager()
    rm.selected_type = "piper_follower"
    monkeypatch.setattr(RobotManager, "scan", lambda self: None)

    rm.load_preset("있음")
    assert rm.selected_type == "so_follower"
    assert rm.config_name == "X"


# ── robotd 가 죽었을 때 무엇을 말하는가 ──────────────────────────────────────

def test_dead_daemon_does_not_leave_the_arm_looking_connected(monkeypatch):
    """**회귀** — robotd 가 죽으면 팔은 연결 해제로 내려가야 한다.

    실제로 겪은 일: robotd 가 죽었는데 로봇 페이지는 초록불이었고, 추론만
    `팔 세그먼트가 없습니다: /dev/shm/piper.arm.can1.state` 로 죽었다.
    화면과 에러가 서로 다른 말을 하면 원인을 찾을 길이 없다.

    `scan()` 이 데몬이 준 것만 순회했기 때문이다 — 빈 목록이 오면 루프가 아예
    안 돌아 `connected` 가 마지막 값에 머물렀다.
    """
    from app.services import robot_manager as rm

    mgr = rm.RobotManager()
    arm = mgr.arms["can1"] = ArmInfo(iface="can1")
    arm.connected = True
    arm.role = "follower"
    arm.ready = True

    monkeypatch.setattr(rm, "_call", lambda *a, **k: k.get("default"))
    mgr.scan()

    assert arm.connected is False        # 장치 사실은 데몬을 따라 내려간다
    assert arm.role == "follower"        # 사람이 정한 것은 남는다
    assert arm.ready is True


def test_refresh_mode_marks_absent_when_the_daemon_says_nothing(monkeypatch):
    """`/robots/current` 폴링 경로도 같아야 한다 — 여기만 남으면 스캔 전까지 거짓말한다."""
    from app.services import robot_manager as rm

    arm = ArmInfo(iface="can1")
    arm.connected = True
    monkeypatch.setattr(rm, "_call", lambda *a, **k: k.get("default"))
    arm.refresh_mode()
    assert arm.connected is False


def test_a_restarted_daemon_alone_does_not_mean_connected(monkeypatch):
    """robotd 가 다시 떠도 팔을 쥔 것은 아니다 — 세그먼트도 아직 없다.

    생존 표시만 보고 연결됐다고 하면 같은 거짓말을 한 번 더 하게 된다.
    """
    from app.services import robot_manager as rm

    mgr = rm.RobotManager()
    arm = mgr.arms["can1"] = ArmInfo(iface="can1")
    arm.connected = True

    # 데몬은 살아나서 iface 는 보이지만 아무것도 연결하지 않았다
    monkeypatch.setattr(rm, "_call", lambda *a, **k: (
        [{"iface": "can1", "connected": False, "state": "UP"}]
        if a and a[0] == "scan" else k.get("default")))
    mgr.scan()
    assert arm.connected is False


def test_attaching_an_arm_fills_the_robot_type_the_ui_cannot_set():
    """⚠ **UI 에는 로봇 타입을 고르는 자리가 없다** — 프리셋이 실어 오거나 세션이
    기억할 뿐이다. 세션이 지워진 뒤 [연결]로 등록만 하면 타입이 null 로 남아
    추론 시작이 "로봇이 선택되지 않았습니다"로 막힌다 (실기에서 그렇게 막혔다).
    등록이 곧 선택이다: 비어 있으면 piper_follower 를 기본으로 채운다.
    이미 있는 타입은 덮지 않는다 — bi 조립은 wrapper 가 robot_ports 로 한다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "robots.py").read_text()
    attach = src.split('@router.post("/attach")', 1)[1].split("@router.post", 1)[0]
    assert 'selected_type = "piper_follower"' in attach, "등록이 타입을 안 채운다"
    assert "if not robot_manager.selected_type" in attach, "있는 타입을 덮어쓴다"
    assert attach.index("selected_type") < attach.index("save_session"), \
        "타입을 채우고 나서 세션을 저장해야 재시작에도 남는다"
