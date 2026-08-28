"""해상도를 바꿔 다시 열 때의 경합.

## ⚠ 실측으로 나온 고장 (2026-08-28, 양팔 녹화)

    WARNING PiperShmCamera(rs_335122270699_color):
            세그먼트가 640x480 인데 설정은 848x480 — 세그먼트 값을 따릅니다
    ValueError: The feature 'observation.images.right_hand' of shape '(480, 640, 3)'
                does not have the expected shape '(480, 848, 3)'

순서가 이랬다:

    1. 카메라가 848x480 으로 발행 중
    2. `prepare_cameras` 가 640x480 을 요청 → rsd 가 파이프라인을 다시 세운다
    3. `_wait_for_frame` 이 **아직 남아 있는 848 프레임**을 읽고 통과한다
    4. `build_cameras_json` 이 848 을 읽어 CLI 에 박는다
    5. 재시작이 끝나고 세그먼트가 640 이 된다
    6. 녹화가 640 을 받는데 스키마는 848 — 첫 프레임에서 죽는다

카메라는 세그먼트를 따라가는데 **데이터셋 스키마는 못 따라간다.** 그쪽은 시작할
때 한 번 정해지고 끝이다.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CFG = REPO / "backend" / "app" / "services" / "camera_config.py"


def _src() -> str:
    return CFG.read_text()


def test_the_wait_can_check_the_size():
    """⚠ 프레임이 **있다**는 것과 **맞다**는 것은 다르다."""
    import inspect

    from app.services.camera_config import _wait_for_frame

    assert "want_wh" in inspect.signature(_wait_for_frame).parameters


def test_prepare_waits_for_the_size_it_opened():
    body = _src().split("def prepare_cameras", 1)[1].split("\ndef ", 1)[0]
    assert "want_wh=want_wh" in body, "크기를 안 보고 기다린다"


def test_it_waits_for_the_running_size_not_the_requested_one():
    """⚠ 장치가 못 내는 조합은 **근사로** 열린다. 요청값을 기다리면 영원히
    안 맞아 타임아웃이 난다 — D405 에 848x480@15 를 요청하면 10fps 로 열린다."""
    body = _src().split("def prepare_cameras", 1)[1].split("\ndef ", 1)[0]
    seg = body.split("want_wh = None", 1)[1].split("_wait_for_frame", 1)[0]
    assert 'got.get("width")' in seg and 'got["width"]' in seg, "실행값이 아니라 요청값을 본다"


def test_no_requested_size_keeps_the_old_behaviour():
    """추론처럼 해상도를 안 따지는 호출은 첫 프레임만 기다리면 된다 —
    거기까지 크기를 요구하면 못 여는 카메라가 생긴다."""
    fn = _src().split("def _wait_for_frame", 1)[1].split("\ndef ", 1)[0]
    assert "if want_wh is None:" in fn
    assert "return True" in fn.split("if want_wh is None:", 1)[1][:80]


def test_the_timeout_message_says_which_size_it_wanted():
    """"프레임을 안 낸다" 만으로는 **크기 때문에** 기다렸다는 걸 모른다."""
    body = _src().split("def prepare_cameras", 1)[1].split("\ndef ", 1)[0]
    assert "로 채워지지 않았습니다" in body


def test_the_shape_comes_from_the_segment_not_the_request():
    """`_shm_dims` 가 세그먼트를 읽는 것 자체는 맞다 — 위 대기가 없으면
    **낡은 세그먼트**를 읽는 것이 문제였다."""
    body = _src().split("def _shm_dims", 1)[1].split("\ndef ", 1)[0]
    assert "sub.shape" in body
