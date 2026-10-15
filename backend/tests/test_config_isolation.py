"""테스트가 사용자의 실제 설정을 못 건드린다 (conftest 머리 주석의 사고).

2026-10-14: 테스트가 `camera_session.json` 을 덮어 개발 기계의 카메라 등록이 사라질 뻔했다.
"""

from pathlib import Path


def test_the_config_dir_is_not_the_users_real_one():
    from app.core.config import settings

    real = Path.home() / ".config" / "piper-web"
    assert settings.config_dir != real, "테스트가 사용자의 설정 디렉토리를 쓴다"
    assert "piper-test-config-" in str(settings.config_dir)


def test_every_session_file_lives_under_the_isolated_dir():
    """경로가 클래스 정의 때 굳는 것들 — 한 곳이라도 실제 홈을 가리키면 사고가 난다."""
    from app.core.config import settings
    from app.services.camera_manager import CameraManager
    from app.services.robot_manager import PARKING_DIR, ROBOT_SESSION_PATH

    root = settings.config_dir
    for p in (CameraManager.CAMERA_SESSION_PATH, ROBOT_SESSION_PATH, PARKING_DIR):
        assert root in Path(p).parents or Path(p) == root, f"격리 밖: {p}"


def test_tests_cannot_open_a_real_camera_node():
    """⚠ 2026-10-14: 모드 열거 테스트가 이 기계의 `/dev/video*` 18 개를 열었고, `uvcvideo` 가
    교착된 뒤에는 노드를 닫지 못해 **pytest 본체가 D 상태로 영구 정지**했다(SIGKILL 불가).

    가드가 **실제 `open` 전에** 거절하므로 이 테스트는 장치를 건드리지 않는다.
    """
    import os

    import pytest

    if os.environ.get("PIPER_HW_TESTS") == "1":
        pytest.skip("하드웨어 테스트를 명시적으로 켠 실행")
    with pytest.raises(RuntimeError, match="실제 카메라 장치"):
        os.open("/dev/video0", os.O_RDONLY)
    with pytest.raises(RuntimeError, match="실제 카메라 장치"):
        os.open("/dev/video17", os.O_RDWR | os.O_NONBLOCK)
    # 다른 경로는 건드리지 않는다
    fd = os.open("/dev/null", os.O_RDONLY)
    os.close(fd)
