"""v4l2 포맷 선택 — 비압축으로 요청 fps 가 안 나올 때만 MJPG (2026-10-10).

GPU 없는 노트북에서 CPU 20% 인데 카메라가 5Hz 로 고정되고, 녹화의 `async_read`
(200ms = 5Hz) 가 TimeoutError 로 죽었다. 포맷을 안 정하면 OpenCV 가 비압축 YUYV 를
골라 USB 대역폭에 걸린다.

여기서 지키는 것: 비압축이 요청을 내면 건드리지 않는다(깨끗한 원본을 굳이 압축본으로
바꾸지 않는다) · 비압축은 못 내고 MJPG 는 내면 MJPG · 둘 다 못 내거나 크기가 없으면
손대지 않는다 · 요청이 없으면(드라이버 기본값) 손대지 않는다.
"""


def _m(w, h, fps, fourcc):
    return {"width": w, "height": h, "fps": fps, "fourcc": fourcc}


def test_raw_that_already_delivers_is_left_alone():
    from piper_cam.v4l2 import choose_fourcc

    modes = [_m(640, 480, 30, "YUYV"), _m(640, 480, 30, "MJPG")]
    assert choose_fourcc(modes, 640, 480, 30) is None


def test_mjpg_is_chosen_when_raw_cannot_reach_the_requested_fps():
    from piper_cam.v4l2 import choose_fourcc

    modes = [_m(1920, 1080, 5, "YUYV"), _m(1920, 1080, 30, "MJPG")]
    assert choose_fourcc(modes, 1920, 1080, 30) == "MJPG"


def test_the_size_must_match_exactly():
    from piper_cam.v4l2 import choose_fourcc

    modes = [_m(1920, 1080, 5, "YUYV"), _m(1920, 1080, 30, "MJPG")]
    assert choose_fourcc(modes, 1280, 720, 30) is None


def test_nothing_changes_when_neither_format_reaches_the_fps():
    from piper_cam.v4l2 import choose_fourcc

    modes = [_m(1920, 1080, 5, "YUYV"), _m(1920, 1080, 15, "MJPG")]
    assert choose_fourcc(modes, 1920, 1080, 30) is None


def test_no_request_means_the_driver_default_stays():
    from piper_cam.v4l2 import choose_fourcc

    modes = [_m(640, 480, 5, "YUYV"), _m(640, 480, 30, "MJPG")]
    assert choose_fourcc(modes, 0, 0, 0) is None
    assert choose_fourcc([], 640, 480, 30) is None
