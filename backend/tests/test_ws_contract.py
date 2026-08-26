"""WS 메시지 계약이 백엔드↔프론트에서 어긋나지 않는지.

두 언어에 걸쳐 있어 자동 동기화가 안 되므로, **목록이 같은지를 테스트로 고정**한다
(refactor/12-ws-message-contract.md 가 (c) 안으로 고른 방식).

이게 없으면: 백엔드가 새 타입을 보내는데 프론트가 모르거나, 프론트가 기다리는 타입을
백엔드가 안 보내도 **에러 없이 화면만 안 갱신된다.**
"""

import re
from pathlib import Path

from app.core import ws_messages as M

_REPO = Path(__file__).resolve().parents[2]
_WS_ROUTER = _REPO / "backend" / "app" / "routers" / "ws.py"
_WS_TYPES = _REPO / "frontend" / "src" / "types" / "ws.ts"


def _types_sent_by_router() -> set[str]:
    """ws.py 가 실제로 보내는 타입 — `"type": M.XXX` 에서 상수 이름을 뽑아 값으로 바꾼다."""
    src = _WS_ROUTER.read_text()
    names = set(re.findall(r'"type":\s*M\.([A-Z_]+)', src))
    assert names, "ws.py 에서 메시지 타입을 못 찾았다 (패턴이 바뀌었나?)"
    return {getattr(M, n) for n in names}


def _types_declared_in_frontend() -> set[str]:
    """types/ws.ts 의 판별 유니언에서 `type: 'xxx'` 리터럴을 뽑는다."""
    src = _WS_TYPES.read_text()
    union = src.split("export type WsMessage =", 1)
    assert len(union) == 2, "types/ws.ts 에서 WsMessage 유니언을 못 찾았다"
    body = union[1].split("export type WsMessageType", 1)[0]
    found = set(re.findall(r"type:\s*'([a-z_]+)'", body))
    assert found, "유니언에서 타입 리터럴을 못 찾았다"
    return found


def test_router_only_sends_declared_types():
    """ws.py 가 `ALL` 에 없는 타입을 보내면 안 된다."""
    unknown = _types_sent_by_router() - M.ALL
    assert not unknown, f"ws_messages.ALL 에 없는 타입을 보내고 있다: {unknown}"


def test_backend_and_frontend_agree():
    """백엔드 목록과 프론트 유니언이 정확히 같아야 한다."""
    backend, frontend = M.ALL, _types_declared_in_frontend()
    assert backend == frontend, (
        f"프론트에만 있음: {frontend - backend} / 백엔드에만 있음: {backend - frontend}"
    )


# 서버가 **보내지 않는** 타입들. 이유가 각자 다르므로 따로 적는다.
_NOT_BROADCAST = {
    M.PONG,        # `broadcast()` 가 아니라 개별 응답
    M.HEARTBEAT,   # ⚠ 방향이 반대다 — 화면이 보내고 서버가 받는다
}


def test_declared_types_are_actually_sent():
    """선언만 해두고 아무도 안 보내는 타입이 있으면 죽은 계약이다."""
    never_sent = M.ALL - _types_sent_by_router() - _NOT_BROADCAST
    assert not never_sent, f"선언됐지만 ws.py 가 보내지 않는 타입: {never_sent}"


def test_the_inbound_type_is_actually_handled():
    """⚠ 보내지 않는다고 빼두면, **받지도 않는데** 아무도 모르는 상태가 된다.

    heartbeat 는 안전 신호다 — 화면은 보내는데 서버가 안 읽으면 E-stop 이
    브라우저가 죽은 줄 알고 돈다.
    """
    from pathlib import Path

    ws_src = (Path(__file__).resolve().parents[1] / "app" / "routers" / "ws.py").read_text()
    assert "M.HEARTBEAT" in ws_src, "WS 가 heartbeat 를 안 받는다"
    assert "estop_bridge.heartbeat(" in ws_src, "받아서 브리지로 안 넘긴다"


def test_state_types_derived_not_hardcoded():
    """`STATE_TYPES` 는 `ALL` 에서 파생돼야 한다 — 프론트 `isStateMessage` 와 같은 규칙."""
    expected = {t for t in M.ALL if t == M.STATE or t.endswith("_state")}
    assert M.STATE_TYPES == expected
    # 각 프로세스 계열이 하나씩 있어야 한다
    for t in (M.STATE, M.TRAIN_STATE, M.RECORD_STATE, M.PS_STATE, M.UPLOAD_STATE):
        assert t in M.STATE_TYPES


def test_frontend_process_state_matches_backend_enum():
    """프론트 `PROCESS_STATES` 가 백엔드 `ProcessState` enum 과 같은 집합인지.

    이전에는 이 유니언이 프론트 4개 페이지에 각각 복붙돼 있었다 (#13).
    """
    from app.services.process_manager import ProcessState

    src = _WS_TYPES.read_text()
    block = re.search(r"PROCESS_STATES = \[(.*?)\]", src, re.S)
    assert block, "types/ws.ts 에서 PROCESS_STATES 를 못 찾았다"
    frontend = set(re.findall(r"'([a-z]+)'", block.group(1)))
    assert frontend == {s.value for s in ProcessState}
