"""세션에 저장하는 값을 바꾸는 라우트는 **저장까지 해야 한다.**

프리셋으로 팔을 되살린 뒤 게이트웨이를 재시작했더니 팔이 통째로 사라졌다.
`load_preset` 은 역할·슬롯·등록 상태를 다 복원하고 팔의 마스터/슬레이브 모드까지
세우는데 `save_session()` 을 안 불렀다. 화면에는 멀쩡히 보였으므로 **재시작하기
전까지 아무도 모른다.**

한 곳을 빠뜨리는 실수라, 목록으로 잠근다.
"""

import re
from pathlib import Path

import pytest

_ROUTER = Path(__file__).resolve().parents[1] / "app" / "routers" / "robots.py"

# 세션 파일에 실제로 실리는 값을 바꾸는 라우트
#   (`save_session` 이 쓰는 필드: role, slot, side, ready, config, robot_type)
_MUST_PERSIST = [
    ("/role", "역할은 사람이 정한 값이라 재시작 뒤에도 남아야 한다"),
    ("/assign", "슬롯이 세션에 실린다"),
    ("/side", "좌/우는 데이터셋 해석이 걸린 값이다"),
    ("/register", "등록 여부가 세션의 핵심이다"),
    ("/unregister", "해제도 남아야 한다 — 안 그러면 되살아난다"),
    ("/presets/load", "프리셋 로드가 곧 세션이 기억해야 할 상태다"),
    ("/master-slave", "이 호출은 역할도 같이 바꾼다"),
    ("/arm-config", "팔 설정값(`config`)이 세션에 그대로 실린다"),
    ("/can/rename", "세션은 iface 이름으로 팔을 찾는다"),
    ("/save", "`config_name` 이 세션에 실린다"),
    ("/select", "로봇 타입이 세션에 실린다"),
]


def _handler(path: str) -> str:
    src = _ROUTER.read_text()
    i = src.index(f'@router.post("{path}")')
    j = src.find("\n@router.", i + 1)
    return src[i: j if j != -1 else len(src)]


@pytest.mark.parametrize("path,why", _MUST_PERSIST)
def test_the_route_saves_the_session(path, why):
    assert "save_session()" in _handler(path), f"{path}: {why}"


def test_a_read_only_route_does_not_save():
    """저장이 공짜는 아니다(파일 쓰기). 읽기만 하는 곳에 붙이면 폴링마다 쓴다 —
    `/current` 는 화면이 1초마다 부른다."""
    src = _ROUTER.read_text()
    i = src.index('@router.get("/current")')
    j = src.find("\n@router.", i + 1)
    assert "save_session()" not in src[i:j]


def test_the_list_covers_every_mutating_post():
    """⚠ 이 목록이 낡으면 검사가 조용히 헐거워진다.

    새로 생긴 POST 가 세션 값을 건드리는지는 사람이 판단해야 하지만, **목록에
    없는 POST 가 생겼다는 것**은 기계가 알려줄 수 있다.
    """
    posts = set(re.findall(r'@router\.post\("([^"]+)"\)', _ROUTER.read_text()))
    known = {p for p, _ in _MUST_PERSIST} | {
        # 세션에 안 실리는 것들 — 장치 조작·일회성 동작
        "/connect", "/disconnect", "/config", "/scan",
        "/identify", "/find-by-motion", "/find-by-motion/stop",
        "/presets", "/presets/delete", "/parking/torque", "/parking/joints",
        "/parking/go", "/parking/save", "/jog/start", "/jog/stop",
        "/jog/goal", "/jog/home", "/presets/save", "/can/up", "/usb/recover",
        "/end-pose/jog", "/end-pose/home", "/relay/start", "/relay/stop",
        "/errors/clear", "/save-config", "/load-config",
        # 바닥 필터 설정 — 세션(팔 슬롯·역할·config)이 아니라 **robotd 가**
        # 자기 `safety.json` 에 따로 저장한다. 게이트웨이 세션에 실으면
        # 저장이 두 곳이 되고, 둘이 어긋나면 화면과 실제 필터가 달라진다.
        "/safety",
        # 0x150 리셋 — 세션에 실을 것이 없다. 장치의 보고 프레임을
        # 재동기화하는 일회성 조작이고, 슬립 간극은 응답·저널로만 남는다.
        "/reset",
        # 하드웨어 영점 — 세션이 아니라 **모터 플래시**에 쓴다. 게이트웨이가
        # 기억할 것이 없다(팔이 기억한다). 오히려 세션에 실으면 재연결 때
        # 다시 굽을 수 있다는 뜻이 되는데, 되돌릴 수 없는 조작이라 위험하다.
        "/zero",
    }
    new = posts - known
    assert not new, f"세션에 실리는 값을 건드리는지 판단이 필요한 새 라우트: {sorted(new)}"
