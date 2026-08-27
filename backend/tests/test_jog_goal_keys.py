"""조그 목표의 관절 이름 규약.

⚠ **실제로 났던 고장이다.** 화면은 LeRobot action-dict 규약(`joint6.pos`)으로
보내는데 조그 세션의 표는 `joint6` 이었다. 병합이 키를 검사하지 않아서

  - 목표 dict 에 아무도 안 읽는 키가 7개 더 붙고
  - **진짜 관절 7개는 시작 자세에 그대로 머물렀다**
  - HTTP 200 이 돌아왔고, robotd 는 시작 자세를 계속 CAN 으로 내보냈다

화면에서는 "조그가 반응이 없다"로 보인다. 아무 에러도 안 난다.
"""

from pathlib import Path

import pytest

from app.core.joints import JOINT_ORDER
from app.services.jog import JogError, canonical_goal

REPO = Path(__file__).resolve().parents[2]


def test_the_lerobot_naming_is_accepted():
    """`config/joints.ts` 의 `actionKey` 가 이 모양이다 — 화면이 이대로 보낸다."""
    got = canonical_goal({f"{j}.pos": 1.0 for j in JOINT_ORDER})
    assert set(got) == set(JOINT_ORDER)


def test_the_plain_naming_is_accepted():
    """`piper_robot`·shm·안전층이 쓰는 이름. 둘 다 정당하다."""
    assert canonical_goal({"joint6": 3.0}) == {"joint6": 3.0}


def test_an_unknown_joint_is_refused_not_merged():
    """⚠ 조용히 병합하면 **200 이 돌아오고 팔은 안 움직인다.** 그게 원래 고장이다."""
    with pytest.raises(JogError, match="joint9"):
        canonical_goal({"joint9.pos": 1.0})


def test_a_typo_cannot_silently_do_nothing():
    with pytest.raises(JogError):
        canonical_goal({"jont1": 0.0})
    with pytest.raises(JogError):
        canonical_goal({"joint1.position": 0.0})


def test_the_goal_never_grows_past_the_joints():
    """키가 7개를 넘으면 규약이 섞인 것이다 — 그때 팔이 멈춘다."""
    got = canonical_goal({**{f"{j}.pos": 0.0 for j in JOINT_ORDER},
                          **{j: 1.0 for j in JOINT_ORDER}})
    assert len(got) == len(JOINT_ORDER)


def test_the_frontend_really_uses_the_dotted_form():
    """별칭을 받아 주는 근거. 화면이 규약을 바꾸면 이 테스트가 알려준다 —
    그때는 별칭이 필요 없어질 수도 있다."""
    src = (REPO / "frontend" / "src" / "config" / "joints.ts").read_text()
    assert "actionKey: 'joint1.pos'" in src


def test_the_panel_is_shared_by_both_paths():
    """화면이 소비자별로 다른 키를 내게 하지 않은 이유 — 같은 패널이
    추론 수동 제어와 조그 양쪽에 쓰인다."""
    users = [p.name for p in (REPO / "frontend" / "src").rglob("*.tsx")
             if "ManualControlPanel" in p.read_text() and p.name != "ManualControlPanel.tsx"]
    assert len(users) >= 2, f"쓰는 곳이 하나뿐이다: {users}"
