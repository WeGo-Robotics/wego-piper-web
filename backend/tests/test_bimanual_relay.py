"""양팔 리더 조종 — 녹화 없이 쌍 둘을 함께 (feature/bimanual.md §8).

쌍 하나의 일(읽기·낡음·전송)은 `test_relay.py` 가 본다. 여기는 **쌍 둘이 함께 돌 때만
생기는 것**을 잠근다: 서로의 잠금을 풀지 않는가, 한쪽만 따라오는 상태가 남지 않는가,
E-stop 이 둘 다 닫는가, 쌍은 화면이 아니라 등록이 정하는가.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("piper_shm")
from app.main import app  # noqa: E402
from app.services import relay as relay_mod  # noqa: E402
from app.services.relay import RelayError, RelayPool  # noqa: E402
from app.services.robot_manager import ArmInfo  # noqa: E402
from app.services.teleop import TeleopSession, teleop_session  # noqa: E402

FULL = {f"joint{i}": 0.0 for i in range(1, 7)} | {"gripper": 0.0}


class _Writer:
    def __init__(self, iface, deadman_ms=0):
        self.iface, self.published, self.closed = iface, [], False
    def publish(self, values):
        self.published.append(dict(values)); return len(self.published)
    def close(self):
        self.closed = True


class _Reader:
    def __init__(self, iface):
        self.iface, self.closed = iface, False
    def read(self):
        return {"values": dict(FULL, joint2=-10.0 if self.iface == "can1" else 20.0),
                "can_wall_ns": time.time_ns()}
    def close(self):
        self.closed = True


@pytest.fixture
def pool(monkeypatch):
    from piper_shm import arm as shm_arm

    from app.services import teleop

    monkeypatch.setattr(shm_arm, "ActionWriter", _Writer)
    monkeypatch.setattr(shm_arm, "StateReader", _Reader)
    monkeypatch.setattr(shm_arm, "list_segments", lambda: [])
    monkeypatch.setattr(shm_arm, "unlink", lambda name: True)
    monkeypatch.setattr(teleop, "enable_torque", lambda iface: None)
    teleop_session.stop()
    p = RelayPool()
    monkeypatch.setattr(relay_mod, "relays", p)
    yield p
    p.stop()
    teleop_session.stop()


# ── 쌍 둘 ───────────────────────────────────────────────────────────────────


def test_two_pairs_run_side_by_side_and_each_follows_its_own_leader(pool):
    pool.start("can1", "can2")
    pool.start("can0", "can3")
    time.sleep(0.2)
    assert {s.follower for s in pool.sessions()} == {"can2", "can3"}
    assert pool.get("can2")._writer.published[-1]["joint2"] == -10.0
    assert pool.get("can3")._writer.published[-1]["joint2"] == 20.0
    assert set(teleop_session.held) == {"can2", "can3"}


def test_stopping_one_side_leaves_the_other_running(pool):
    """⚠ 예전에는 정지가 teleop 잠금을 **통째로** 풀었다 — 쌍이 하나일 때는 같은 말이었다.
    둘이면 한쪽을 닫는 순간 다른 쪽 팔로워의 잠금이 풀려 조그가 그 위에 열린다."""
    pool.start("can1", "can2")
    pool.start("can0", "can3")
    assert pool.stop("can2") == ["can2"]
    assert pool.get("can2") is None and pool.get("can3") is not None
    assert set(teleop_session.held) == {"can3"}, "다른 쪽 잠금까지 풀렸다"


def test_a_follower_cannot_take_two_leaders_nor_a_leader_two_followers(pool):
    pool.start("can1", "can2")
    with pytest.raises(RelayError, match="이미"):
        pool.start("can0", "can2")
    with pytest.raises(RelayError, match="리더 하나로"):
        pool.start("can1", "can3")
    assert [s.follower for s in pool.sessions()] == ["can2"]


def test_starting_both_rolls_back_when_one_side_fails(pool, monkeypatch):
    """⚠ 한 팔만 따라 움직이는 상태를 남기지 않는다 — 두 리더를 함께 쥔 사람은
    한쪽 팔로워가 안 따라오는 것을 놓치기 쉽다."""
    from piper_shm import arm as shm_arm

    class _Silent(_Reader):
        def read(self):
            return None if self.iface == "can0" else super().read()

    monkeypatch.setattr(shm_arm, "StateReader", _Silent)
    with pytest.raises(RelayError, match="발행"):
        pool.start_pairs([("can1", "can2"), ("can0", "can3")])
    assert not pool.is_running, "왼쪽만 남았다"
    assert not teleop_session.is_running


def test_the_estop_closes_both_pairs(pool):
    from app.services import exclusivity as ex

    pool.start_pairs([("can1", "can2"), ("can0", "can3")])
    asyncio.run(ex.STOPPERS[ex.Activity.TELEOP]())
    assert not pool.is_running and not teleop_session.is_running


def test_a_second_pair_is_not_blocked_by_the_first_but_recording_still_is(pool):
    """릴레이끼리는 팔마다 가린다. 녹화·추론과는 여전히 배타다 — 같은 팔을 둘이 민다."""
    from fastapi import HTTPException

    from app.services import exclusivity as ex

    pool.start("can1", "can2")
    ex.require_idle(ex.Activity.TELEOP, allow_self=True)
    with pytest.raises(HTTPException):
        ex.require_idle(ex.Activity.TELEOP)
    with pytest.raises(HTTPException):
        ex.require_idle(ex.Activity.RECORDING)


def test_the_busy_message_names_every_arm(pool):
    from fastapi import HTTPException

    from app.services import exclusivity as ex

    pool.start_pairs([("can1", "can2"), ("can0", "can3")])
    with pytest.raises(HTTPException) as e:
        ex.require_idle(ex.Activity.TELEOP)
    assert "can2" in e.value.detail and "can3" in e.value.detail


def test_status_keeps_the_single_pair_shape_and_lists_pairs(pool):
    """단팔 화면들은 위층의 `leader`·`follower` 를 바로 읽는다 — 한 쌍일 때 그대로여야 한다."""
    pool.start("can1", "can2")
    st = pool.status()
    assert st["leader"] == "can1" and st["follower"] == "can2" and len(st["pairs"]) == 1
    pool.start("can0", "can3")
    st = pool.status()
    assert st["running"] and {p["follower"] for p in st["pairs"]} == {"can2", "can3"}
    assert pool.status("can3")["leader"] == "can0"
    assert pool.status("can9")["running"] is False


# ── 팔마다 잠금 ─────────────────────────────────────────────────────────────


def test_only_relays_stack_on_different_arms():
    """조그·말단 조그는 화면 하나가 팔 하나를 미는 것이라 그대로 혼자다."""
    s = TeleopSession()
    assert s.start("can2", "leader")[0] and s.start("can3", "leader")[0]
    assert not s.start("can3", "leader")[0], "같은 팔을 둘이 민다"
    assert not s.start("can4", "joint")[0], "릴레이가 도는데 조그가 열린다"
    s.stop()
    assert s.start("can2", "joint")[0]
    assert not s.start("can3", "leader")[0], "조그가 도는데 릴레이가 열린다"


def test_releasing_only_frees_what_the_caller_holds():
    """⚠ [해제]로 이미 놓은 릴레이가 나중에 정지하면서 놓으면, 그 사이 같은 팔을
    잡은 조그의 잠금을 대신 풀어 버린다."""
    s = TeleopSession()
    s.start("can2", "joint")
    s.release("can2", "leader")
    assert s.held.get("can2", ("",))[0] == "joint"
    s.release("can2", "joint")
    assert not s.is_running


# ── 쌍은 등록이 정한다 (라우터) ──────────────────────────────────────────────


def _arms(*specs):
    out = {}
    for iface, role, side, *rest in specs:
        a = ArmInfo(iface=iface)
        a.role, a.side, a.connected = role, side, (rest[0] if rest else True)
        out[iface] = a
    return out


FOUR = (("can1", "leader", "left"), ("can2", "follower", "left"),
        ("can0", "leader", "right"), ("can3", "follower", "right"))


@pytest.fixture
def client():
    return TestClient(app)


def test_pairs_come_from_the_side_registration(client, monkeypatch):
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm.robot_manager, "arms", _arms(*FOUR))
    pairs = client.get("/api/robots/relay/pairs").json()["pairs"]
    assert [(p["side"], p["leader"], p["follower"], p["problem"]) for p in pairs] == [
        ("left", "can1", "can2", ""), ("right", "can0", "can3", "")]


def test_a_side_that_cannot_pair_says_what_to_do(client, monkeypatch):
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm.robot_manager, "arms", _arms(
        ("can1", "leader", "left"), ("can2", "follower", "left", False),
        ("can3", "follower", "right")))
    left, right = client.get("/api/robots/relay/pairs").json()["pairs"]
    assert "can2" in left["problem"] and "[연결]" in left["problem"]
    assert "리더 팔이 없습니다" in right["problem"] and "[마스터]" in right["problem"]


def test_a_follower_driven_by_another_leader_is_not_counted_as_running(pool, client, monkeypatch):
    """⚠ 조종 창(웹 리더)이 그 팔로워를 끌고 있으면 그 쪽은 "도는 쌍"이 아니다 — 그렇게
    세면 [양팔 시작]이 건너뛰어, 이 쪽 리더를 움직여도 팔로워가 안 따라온다."""
    from app.services import robot_manager as rm

    monkeypatch.setattr(rm.robot_manager, "arms", _arms(*FOUR))
    pool.start("web_leader1", "can2")
    left, right = client.get("/api/robots/relay/pairs").json()["pairs"]
    assert left["relay"] is None and "web_leader1" in left["problem"]
    assert client.post("/api/robots/relay/pairs/start", json={}).status_code == 409
    assert [s.follower for s in pool.sessions()] == ["can2"]


def test_starting_both_is_refused_until_both_sides_pair(client, monkeypatch):
    from app.services import robot_manager as rm

    started = []
    monkeypatch.setattr(relay_mod, "relays", type("P", (), {
        "get": lambda self, f: None, "sessions": lambda self: [],
        "is_running": False,
        "start_pairs": lambda self, pairs, mode: started.append((pairs, mode)),
    })())
    monkeypatch.setattr(rm.robot_manager, "arms", _arms(*FOUR[:3]))
    r = client.post("/api/robots/relay/pairs/start", json={})
    assert r.status_code == 409 and "오른쪽" in r.json()["detail"] and not started

    monkeypatch.setattr(rm.robot_manager, "arms", _arms(*FOUR))
    r = client.post("/api/robots/relay/pairs/start", json={})
    assert r.status_code == 200, r.text
    assert started == [([("can1", "can2"), ("can0", "can3")], "joint")]


def test_stop_without_a_follower_closes_everything_and_with_one_only_that(pool, client):
    pool.start_pairs([("can1", "can2"), ("can0", "can3")])
    assert client.post("/api/robots/relay/stop", json={"follower": "can3"}).json()["stopped"] == ["can3"]
    assert pool.get("can2") is not None
    assert client.post("/api/robots/relay/stop").json()["stopped"] == ["can2"]
    assert not pool.is_running
