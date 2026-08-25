"""역할(라벨)과 팔 모드(펌웨어)를 맞춘다.

⚠ **둘은 다른 것이다.** `role` 은 우리가 붙인 라벨이라 게이트웨이와 프리셋에
살고, 마스터/슬레이브는 팔 펌웨어의 모드라 **전원을 끄면 풀린다.**

실기에서 이렇게 났다: 팔로워가 바닥에 부딪혀 토크 과부하로 멈춘 뒤 모드가
뒤집혔다(리더=slave, 팔로워=master). 팔로워가 마스터 모드면 외부 명령을 통째로
무시하므로 리더를 끌어도 안 따라왔다. 등록 해제 후 프리셋을 다시 불러도 그대로였다 —
복구 경로가 **라벨만** 되살렸기 때문이다. 화면은 `leader` 라고 말하는데 팔은
슬레이브였고, 에피소드 하나를 그렇게 버렸다.
"""

import inspect

from app.services.robot_manager import ArmInfo, RobotManager


def _arm(role, is_master, connected=True):
    a = ArmInfo("can1")
    a.role, a.is_master, a.connected = role, is_master, connected
    return a


def test_a_follower_in_master_mode_is_flagged():
    """이게 실기에서 난 그 상태다 — 명령이 통째로 무시된다."""
    why = _arm("follower", True).mode_mismatch()
    assert why and "무시" in why


def test_a_leader_in_slave_mode_is_flagged():
    why = _arm("leader", False).mode_mismatch()
    assert why and "리더" in why


def test_matching_modes_say_nothing():
    assert _arm("leader", True).mode_mismatch() is None
    assert _arm("follower", False).mode_mismatch() is None


def test_unknowns_are_not_judged():
    """모르는 것과 어긋난 것은 다르다 — 모르면 경고하지 않는다."""
    assert _arm("unknown", True).mode_mismatch() is None
    assert _arm("follower", None).mode_mismatch() is None
    assert _arm("follower", True, connected=False).mode_mismatch() is None


def test_restoring_a_role_also_sets_the_arm_mode():
    """**회귀** — 프리셋과 세션 복원이 라벨만 되살렸다.

    그러면 화면은 `leader` 인데 팔은 슬레이브로 남는다. 복구의 목적이 "이 배치를
    그대로 되살리는 것"이라면 라벨만 되살리는 건 절반이다.
    """
    for fn in (RobotManager.load_preset, RobotManager.restore_session):
        assert "apply_role_mode" in inspect.getsource(fn), \
            f"{fn.__name__} 이 팔 모드를 안 세운다"


def test_the_mode_is_set_without_trusting_the_read():
    """지금 모드 판정은 CAN RX 유무로 **추정**한다 — 그 값을 믿고 건너뛰면
    틀린 채로 남는다. 그래서 읽어보지 않고 그냥 세운다."""
    src = inspect.getsource(RobotManager.apply_role_mode)
    assert "is_master ==" not in src and "if arm.is_master" not in src, \
        "읽은 값을 믿고 건너뛴다"
    assert "set_master_slave" in src


def test_recording_refuses_to_start_on_a_mismatch():
    """녹화는 정상으로 보이고 **에피소드만 못 쓰게 된다** — 시작 전에 잡아야 한다."""
    from app.routers import recording

    src = inspect.getsource(recording)
    assert "mode_mismatch()" in src, "녹화가 모드를 안 본다"
    start = src.split("async def start_recording", 1)[1].split("\nasync def", 1)[0]
    assert "mode_mismatch" in start


def test_the_screen_shows_the_mismatch():
    """전원이 나가거나 과부하로 멈추면 풀리는데, 화면은 라벨만 보고 멀쩡하다고 했다."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages"
           / "RobotsPage.tsx").read_text()
    assert "arm.mode_mismatch" in src, "화면이 불일치를 안 보여준다"
    # 문구는 백엔드가 만든다 — 화면이 두 값을 비교하면 규칙이 두 곳에 산다
    assert "arm.role === 'leader' && arm.master_slave" not in src
