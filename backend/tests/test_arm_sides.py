"""좌/우 규칙 — 텔레오퍼레이션은 같은 쪽 리더로만.

## 왜 백엔드에도 있나

화면만 막으면 API 로 우회된다. 그리고 화면은 팔이 넷일 때 어느 리더가 어느
팔로워를 끄는지 보여줄 방법이 없다 — 규칙이 서버에 있어야 그 답이 하나다.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROUTER = REPO / "backend" / "app" / "routers" / "robots.py"
PANEL = REPO / "frontend" / "src" / "components" / "JogPanel.tsx"


def _router() -> str:
    return ROUTER.read_text()


# ── 같은 쪽 ─────────────────────────────────────────────────────────────────

def test_relay_requires_a_leader_on_the_same_side():
    """⚠ 왼팔을 오른쪽 리더로 끌면 **조작자의 손 방향과 팔 방향이 뒤집힌다.**
    사람이 실수하는 자리라 기계가 막는다."""
    body = _router().split('@router.post("/relay/start")', 1)[1].split("@router.", 1)[0]
    assert "_leader_on_side(follower.side)" in body
    assert "같은 쪽 리더만" in body


def test_no_leader_on_that_side_says_what_to_do():
    """"안 됩니다" 만 말하면 사용자가 다음에 뭘 할지 모른다."""
    body = _router().split('@router.post("/relay/start")', 1)[1].split("@router.", 1)[0]
    assert "리더 팔이 없습니다" in body and "마스터" in body


# ── 한 쪽에 리더 하나 ───────────────────────────────────────────────────────

def test_a_side_cannot_get_a_second_leader():
    """⚠ 둘이면 어느 것이 끄는지가 **호출 순서**에 달리는데, 그건 화면에 안 보인다.
    세우는 자리에서 막는다 — 릴레이 시작 때 막으면 이미 둘 다 마스터다."""
    body = _router().split('@router.post("/master-slave")', 1)[1].split("@router.", 1)[0]
    assert "이미 리더가 있습니다" in body
    assert 'a.role == "leader"' in body and "a.side == arm.side" in body


def test_two_leaders_are_refused_at_use_too():
    """설정을 우회해 둘이 됐을 수도 있다(세션 복원, 전원 재투입).
    쓰는 자리에서도 세어 본다."""
    fn = _router().split("def _leader_on_side", 1)[1].split("\ndef ", 1)[0]
    assert "len(found) > 1" in fn


# ── 좌/우 미지정 ────────────────────────────────────────────────────────────

def test_an_unassigned_arm_can_only_be_jogged():
    """⚠ 짝을 정할 수 없는 팔이다. 릴레이를 열면 '아무 리더나' 붙는 셈이고,
    팔이 셋 이상이면 어느 것이 끄는지 화면으로는 알 수 없다.
    수동 조작(조그)은 짝이 필요 없으므로 그대로 쓴다."""
    fn = _router().split("def _require_paired", 1)[1].split("\ndef ", 1)[0]
    assert 'arm.side not in ("left", "right")' in fn
    assert "수동 조작" in fn

    body = _router().split('@router.post("/relay/start")', 1)[1].split("@router.", 1)[0]
    assert "_require_paired(body.follower)" in body, "릴레이가 미지정 팔을 받는다"


def test_jog_does_not_require_a_side():
    """조그까지 막으면 좌/우를 정하기 **전에** 팔을 움직일 방법이 없어진다 —
    [좌/우?] 판별이 팔을 움직여 보는 기능인데."""
    body = _router().split('@router.post("/jog/start")', 1)[1].split("@router.", 1)[0]
    assert "_require_paired" not in body


def test_the_panel_explains_both_cases_separately():
    """"안 된다" 는 같아도 **할 일이 다르다** — 하나는 좌/우 지정, 하나는 마스터 설정."""
    src = PANEL.read_text()
    assert "좌/우가 지정되지 않아" in src
    assert "리더 팔이 없습니다" in src


# ── 원점가기 ────────────────────────────────────────────────────────────────

def test_going_home_does_not_ask_first():
    """자주 누르는 버튼이고 되돌릴 수 있다(다시 조그하면 된다).
    되돌릴 수 없는 조작(하드웨어 영점)과는 다르게 다룬다."""
    from conftest import code_only

    assert "confirm(" not in code_only(PANEL.read_text())


# ── 조그 현재 위치 ──────────────────────────────────────────────────────────

def test_the_slider_reads_the_plain_joint_names():
    """⚠ `/parking/joints` 는 평문 이름(`joint1`)을 준다. `actionKey`(`joint1.pos`)
    로 찾으면 전부 `undefined` → `?? 0` 이라 **슬라이더가 0 으로 굳는다.**
    조그를 켜면 그 0 이 첫 목표가 되어 팔이 엉뚱한 자세로 기어간다."""
    src = PANEL.read_text()
    read = src.split("parking/joints", 1)[1][:400]
    assert "d[j.name]" in read
    assert "d[j.actionKey]" not in read


def test_the_endpoint_really_returns_plain_names():
    """위 테스트의 근거. API 가 바뀌면 여기서 걸린다."""
    body = _router().split('@router.get("/parking/joints/{iface}")', 1)[1].split("@router.", 1)[0]
    assert "read_joints_normalized" in body


# ── 말단 조그 반응 ──────────────────────────────────────────────────────────

def test_the_enable_is_not_paid_on_every_press():
    """⚠ `EnablePiper()` 뒤에는 반영 대기 200ms 가 붙는다. 매번 부르면 버튼 한
    번에 최소 200ms 가 깔리고, 연타하는 조작에서 그건 "답답하다"가 된다."""
    arm = (REPO / "robot" / "piper_robot" / "arm.py").read_text()
    body = arm.split("def move_end_pose", 1)[1].split("\n    def ", 1)[0]
    assert "_ensure_enabled()" in body
    ttl = arm.split("ENABLE_TTL_S = ", 1)[1].split("\n", 1)[0]
    assert float(ttl) > 0
