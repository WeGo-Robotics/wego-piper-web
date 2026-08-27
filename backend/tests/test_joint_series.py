"""에피소드 뷰어의 **관절별 그래프**.

집계 신호(관절 속도·말단 속도)는 "얼마나 움직였나" 만 말한다. 어느 축이
어떻게 움직였는지, 지령을 못 따라간 구간이 어딘지는 축별로 봐야 보인다.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "phase"))

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from app.routers.phase import _joint_series  # noqa: E402

_FRONT = _ROOT / "frontend" / "src" / "pages" / "EpisodesPage.tsx"


def _frames(n=5, width=7):
    st = np.arange(n * width, dtype=np.float32).reshape(n, width)
    return pd.DataFrame({
        "observation.state": list(st),
        "action": list(st + 0.5),
    })


def test_every_axis_gets_its_own_series():
    j = _joint_series(_frames())
    assert len(j["names"]) == 7
    assert len(j["state"]) == 7 and len(j["action"]) == 7
    assert all(len(s) == 5 for s in j["state"])


def test_the_last_channel_is_named_gripper():
    """⚠ 마지막 채널은 관절이 아니다 — `joint7` 이라고 부르면 없는 축을 만든다."""
    assert _joint_series(_frames())["names"][-1] == "gripper"


def test_commanded_values_come_along():
    """⚠ 실측만 그리면 **추종 오차가 안 보인다.**

    물체에 막혀 못 따라가는 구간이 두 선의 벌어짐으로 드러난다 — 그리퍼 갭
    그래프가 지령과 실측을 같이 보여주는 것과 같은 이유다.
    """
    j = _joint_series(_frames())
    assert j["action"][0][0] == pytest.approx(j["state"][0][0] + 0.5)


def test_a_narrower_arm_does_not_invent_axes():
    """축 수가 다른 팔도 있다 — 7을 박아두면 없는 축을 그리거나 있는 축을 뺀다."""
    j = _joint_series(_frames(width=6))
    assert len(j["names"]) == 6


def test_only_the_needed_columns_are_read():
    """⚠ `labeler.load_frames` 는 데이터셋 **전체**를 읽는다. 에피소드를 고를
    때마다 부르는 자리라 열과 행을 좁힌다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "phase.py").read_text()
    body = src.split("def _episode_frames", 1)[1].split("\ndef _tip_speed_now", 1)[0]
    assert "columns=cols" in body, "열을 안 좁힌다"
    assert "t.episode_index == episode" in body, "행을 안 좁힌다"
    # ⚠ 문자열이 아니라 **호출된 이름**을 본다. 이 함수는 왜 `load_frames` 를
    #   안 쓰는지 docstring 에서 설명한다 — 문자열로 뒤지면 그 설명이 걸린다.
    #   (오늘 같은 함정에 세 번 걸렸다: `window.confirm`, `JOINT_CALIBRATION`.)
    import ast

    tree = ast.parse("def _f():\n" + "\n".join("    " + l for l in body.splitlines()[1:]))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "load_frames" not in called, "전체 읽기를 다시 쓴다"


def test_the_frames_are_read_once_for_both_signals():
    """관절 그래프와 말단 속도가 같은 프레임을 본다 — 두 번 읽을 이유가 없다."""
    src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "phase.py").read_text()
    body = src.split("async def get_signals", 1)[1].split("\ndef _episode_frames", 1)[0]
    assert body.count("_episode_frames(") == 1


def test_the_joint_charts_start_collapsed():
    """축이 7개라 늘 펼쳐두면 신호 그래프가 화면 밖으로 밀린다."""
    src = _FRONT.read_text()
    assert "episodes-show-joints" in src, "선택을 기억하지 않는다"
    assert "showJoints && signals.joints.names.map" in src, "접기가 안 걸려 있다"


def test_both_series_are_plotted_per_joint():
    src = _FRONT.read_text()
    block = src.split("showJoints && signals.joints.names.map", 1)[1][:700]
    assert "실측" in block and "지령" in block, "한쪽만 그린다"
