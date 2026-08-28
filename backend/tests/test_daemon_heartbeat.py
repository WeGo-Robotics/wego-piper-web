"""데몬 생존 표시는 RPC 루프와 **따로** 나야 한다.

## ⚠ 실측으로 나온 고장 (2026-08-28)

회색 카드 보정을 돌릴 때마다 게이트웨이가 "rsd 응답 없음"을 찍었다. 로그의
상관이 정확하다:

    11:04:05  rsd 응답 없음 (lost)
    11:04:06  회색 카드 보정 335122270699 완료      ← 1초 뒤

데몬은 멀쩡히 일하는 중이었다. 표시가 루프 안에 있어서 처리 시간만큼 늦었을 뿐이다.

## 산술이 답이다

    보정 = 안정화 2.0초 + 자동끄기 0.4초 + 3라운드 × 0.4초 = **최소 3.6초**
    판정 = DAEMON_ALIVE_TTL_MS = 3.0초

**넘길 수밖에 없다.** 우연이 아니라 구조다.

느린 것과 죽은 것은 다르고, 그 둘을 섞으면 **진짜 죽었을 때를 못 알아본다.**
"""

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DAEMONS = {"rsd": REPO / "daemons" / "rsd.py", "robotd": REPO / "daemons" / "robotd.py"}


@pytest.mark.parametrize("name", sorted(DAEMONS))
def test_the_beat_is_not_inside_the_rpc_loop(name):
    """⚠ 루프 안에서 표시하면 **긴 RPC 하나가 데몬을 죽은 것으로 만든다.**"""
    src = DAEMONS[name].read_text()
    serve = src.split("def serve(", 1)[1].split("\ndef ", 1)[0]
    assert "mark_alive" not in serve, f"{name}: 생존 표시가 아직 serve 루프 안이다"


@pytest.mark.parametrize("name", sorted(DAEMONS))
def test_there_is_a_beat_thread(name):
    src = DAEMONS[name].read_text()
    assert "def heartbeat(" in src
    serve = src.split("def serve(", 1)[1].split("\ndef ", 1)[0]
    assert "target=heartbeat" in serve, f"{name}: 심박 스레드를 안 띄운다"


@pytest.mark.parametrize("name", sorted(DAEMONS))
def test_the_beat_stops_when_the_daemon_does(name):
    """종료 경로에서 생존 표시를 계속 내는 것은 거짓말이다."""
    serve = DAEMONS[name].read_text().split("def serve(", 1)[1].split("\ndef ", 1)[0]
    assert "stop.set()" in serve


@pytest.mark.parametrize("name", sorted(DAEMONS))
def test_the_beat_period_leaves_room_under_the_ttl(name):
    """주기가 TTL 에 붙어 있으면 한 번만 늦어도 죽은 것으로 보인다."""
    fn = DAEMONS[name].read_text().split("def heartbeat(", 1)[1].split("\ndef ", 1)[0]
    assert "DAEMON_ALIVE_TTL_MS / 3000" in fn, "TTL 의 3분의 1이 아니다"


def test_the_calibration_really_exceeds_the_ttl():
    """위 주석의 산술이 코드와 맞는지 — 상수가 바뀌면 여기서 걸린다."""
    from piper_bus import contract as C

    hub = (REPO / "rs" / "piper_rs" / "hub.py").read_text()
    tree = ast.parse(hub)
    consts = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            n = node.targets[0].id
            if n in ("_CAL_SETTLE_S", "_CAL_ROUNDS") and isinstance(node.value, ast.Constant):
                consts[n] = node.value.value
    worst = consts["_CAL_SETTLE_S"] + 0.4 + consts["_CAL_ROUNDS"] * 0.4
    assert worst > C.DAEMON_ALIVE_TTL_MS / 1000, (
        f"보정 {worst}초가 TTL {C.DAEMON_ALIVE_TTL_MS / 1000}초를 안 넘는다 — "
        "이 테스트의 전제가 바뀌었다면 주석도 고쳐라")


def test_a_long_rpc_still_blocks_other_rpcs():
    """⚠ 심박을 뗀 것이 **RPC 를 동시에 처리한다는 뜻은 아니다.**

    보정 중에는 다른 요청이 뒤에 줄을 선다 — 그건 그대로다. 고친 것은
    "일하는 중"을 "죽었다"로 읽지 않게 한 것뿐이다. 다음 사람이 이걸 동시성
    수정으로 오해하면 안 된다.
    """
    serve = DAEMONS["rsd"].read_text().split("def serve(", 1)[1].split("\ndef ", 1)[0]
    assert "getattr(hub, method)(*args)" in serve, "여전히 루프에서 순차 처리한다"
