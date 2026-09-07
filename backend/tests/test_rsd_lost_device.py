"""카메라 한 대가 USB 에서 빠졌을 때 rsd 가 살아남는가.

## ⚠ 실측으로 나온 고장이다 (2026-08-28, 카메라 3대)

    [WARNING] rsd: RealSense 335122271186: USB 노드가 사라졌습니다 — 발행을 중단합니다
    free(): corrupted unsorted chunks
    piper-rsd.service: Main process exited, code=dumped, status=6/ABRT

같은 날 `double free or corruption (!prev)` 로도 한 번 죽었다(재시작 카운터 3).

librealsense 가 **이미 없는 USB 장치**의 전송을 정리하려다 힙을 깨뜨린다.
SIGABRT 라 `except` 로 못 잡고 프로세스가 통째로 죽는다 — 카메라 한 대가
빠졌을 뿐인데 **나머지 두 대까지 같이 멎는다.**
"""

import ast
from pathlib import Path

HUB = Path(__file__).resolve().parents[2] / "rs" / "piper_rs" / "hub.py"


def _src() -> str:
    return HUB.read_text()


def test_a_vanished_device_is_not_torn_down():
    """⚠ `try/except` 로는 못 막는다 — SIGABRT 는 예외가 아니다."""
    src = _src()
    body = src.split("def _stop_pipeline", 1)[1].split("\n    def ", 1)[0]
    assert "if device_gone:" in body
    stop = body.split("if device_gone:", 1)[1]
    # `stop()` 은 **else 쪽에만** 있어야 한다
    assert "else:" in stop and stop.index("else:") < stop.index("self._pipeline.stop()")


def test_the_decision_is_made_from_the_usb_node():
    """"프레임이 안 온다" 는 장치가 아직 있을 수도 있다 — 그때는 정리해야
    자원이 돌아온다. 사유 문자열이 아니라 **노드 존재**로 가른다."""
    body = _src().split("def _declare_lost", 1)[1].split("\n    def ", 1)[0]
    assert "not self._device_present()" in body
    assert "device_gone=gone" in body


def test_presence_does_not_ask_librealsense():
    """⚠ `query_devices()` 를 주기적으로 부르면 D405 를 커널 D-state 로 물리게 한
    그 질의를 늘리는 셈이다 — 프로세스를 나눠 얻은 격리를 스스로 깬다."""
    body = _src().split("def _device_present", 1)[1].split("\n    def ", 1)[0]
    # ⚠ docstring 이 **왜** 그걸 안 쓰는지 설명하느라 그 이름을 적는다.
    #   이 저장소에서 다섯 번째다 — 금지 검사는 늘 주석을 떼고 한다.
    code = body.split('"""', 2)[-1]
    assert "query_devices" not in code
    assert "Path" in code, "파일 존재 확인이 아니다"


def test_losing_one_camera_leaves_the_others_alone():
    """⚠ **이것이 요점이다.** rsd 는 세 대의 파이프라인을 한 프로세스에서 든다.
    한 대의 정리가 프로세스를 죽이면 나머지가 같이 죽는다.

    `_declare_lost` 는 **자기 카메라만** 건드려야 한다 — 다른 카메라나 허브
    전체를 만지면 안 된다.
    """
    tree = ast.parse(_src())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_declare_lost")
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert node.value.id in ("self", "time", "logger"), \
                f"자기 카메라 밖을 만진다: {node.value.id}.{node.attr}"


def test_the_leak_is_written_down():
    """파이프라인 객체를 흘린다는 것은 **의도된 맞바꿈**이다 — 다음 사람이
    "정리 안 하네" 하고 되돌리면 데몬이 다시 죽는다."""
    body = _src().split("def _stop_pipeline", 1)[1].split("\n    def ", 1)[0]
    assert "SIGABRT" in body and "코어덤프" in body


def test_a_returning_device_clears_the_lost_mark():
    """⚠ **실측 (오늘): 카메라는 있는데 화면에서 나왔다 사라졌다 했다.**
    13:49 의 순간 이탈로 `lost_at` 이 찍힌 뒤 장치가 돌아와도 지워지지 않아,
    스캔이 살려낸 카메라를 게이트웨이 감시가 `lost()` 를 보고 2초 안에 도로
    없음 처리했다. 팔의 브리지 복귀와 같은 병이다 (커밋 221800a).

    해제는 두 신호로 한다: 스캔 열거에 다시 보이거나(대부분 안 연 채로 꽂혀만
    있는 카메라), 파이프라인이 실제로 다시 서거나(연결). 열거는 프레임의
    보증이 아니지만 진짜 안 나오면 읽기 루프가 다시 판정한다 — 회복 경로가 있다."""
    src = _src()
    scan_body = src.split("def scan", 1)[1].split("\n    def ", 1)[0]
    assert "dev.lost_at = 0.0" in scan_body, "스캔이 돌아온 장치의 lost 를 안 지운다"
    conn = src.split("def connect_stream", 1)[1].split("\n    def ", 1)[0]
    assert "self.lost_at = 0.0" in conn, "연결 성공이 lost 를 안 지운다"
