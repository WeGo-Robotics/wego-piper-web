"""자식 프로세스를 띄우는 인터프리터 경로 (`local_python` / `grpc_python`).

게이트웨이를 systemd 유닛으로 옮긴 뒤 추론이 안 떴다. 유닛 PATH 에는 conda 가
없는데 `local_python` 기본값이 `"python"` 이었다 — 셸에서 띄우는 동안에만 맞던
값이다. 에러는 `[Errno 2] No such file or directory` 였고 **어느 파일인지 안
말해서** 모델 경로를 못 찾는 것으로 읽혔다.
"""

import sys
from pathlib import Path

from app.core.config import Settings

_BACKEND = Path(__file__).resolve().parents[1]


def test_child_interpreters_do_not_depend_on_path():
    """PATH 에 기대면 **띄우는 방식이 바뀔 때** 조용히 깨진다.

    셸(conda 활성) → systemd 유닛(최소 PATH) 이동이 정확히 그거였다. 절대 경로만
    쓴다. `sys.executable` 은 지금 도는 인터프리터라 호스트든 컨테이너든 맞는다.
    """
    s = Settings()
    for name in ("local_python", "grpc_python"):
        value = getattr(s, name)
        assert Path(value).is_absolute(), \
            f"{name}={value!r} — PATH 탐색에 기댄다. 유닛에서 못 찾는다"


def test_the_wrapper_interpreter_is_the_one_we_are_running():
    """wrapper 는 lerobot 을 import 한다 — 아무 python 이나 되는 게 아니다.

    `/usr/bin/python` 으로 떨어지면 PATH 로 찾을 때보다 **더 나쁘다**: 실행은
    되고 import 에서 죽으므로 원인이 한 겹 더 멀어진다.
    """
    assert Settings().local_python == sys.executable


def test_a_missing_executable_says_which_one():
    """`[Errno 2] No such file or directory` 만으로는 뭘 고쳐야 할지 모른다.

    이 실패를 모델 경로 문제로 읽었던 것이 이 파일이 생긴 이유다.
    """
    src = (_BACKEND / "app" / "services" / "process_manager.py").read_text()
    assert "except FileNotFoundError" in src, "실행 파일 없음을 따로 안 다룬다"
    assert "실행 파일이 없습니다: {cmd[0]}" in src, "어느 파일인지 안 밝힌다"


def test_an_unreadable_scan_root_does_not_kill_the_path_list():
    """읽을 수 없는 경로가 목록에 남을 수 있다 (컨테이너가 붙인 `/root/...`).

    `exists()` 가 거기서 `PermissionError` 를 던지면 설정 화면의 경로 목록이
    **통째로 비고**, 그러면 문제의 경로를 지울 방법이 화면에서 사라진다.
    """
    from app.routers.models import _readable

    class Unreadable:
        def exists(self):
            raise PermissionError(13, "Permission denied")

    assert _readable(Unreadable()) is False
