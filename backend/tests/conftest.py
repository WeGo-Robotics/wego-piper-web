"""테스트 전역 설정.

## 버스를 쓰는 테스트는 **전부** 별도 Redis DB 로 격리한다

운영과 같은 DB(0)를 쓰면 양방향으로 사고가 난다:

- 테스트 fixture 가 `piper:train:jobs` 를 지운다 → **돌고 있는 학습 기록이 사라진다.**
  실제로 일어났다: 학습을 두 번 시작했는데 `pytest` 를 돌린 뒤 레지스트리가 비어 있었다
- 테스트가 띄운 estopd 가 `piper:activity:pids` 를 읽고 **진짜 추론·녹화를 SIGKILL** 한다

예전에는 `test_estop_daemon.py` 에만 격리를 넣었다. 나머지 파일이 같은 DB 를 계속
쓰고 있었으므로 **같은 실수를 파일 단위로 반복하는 구조**였다.
여기 conftest 에 두면 새 테스트 파일이 생겨도 자동으로 적용된다.

`autouse` 인 이유: 버스를 쓰는지 파일마다 판단하게 두면 결국 하나를 빠뜨린다.
"""

import os

import pytest

# ⚠ **러너도 고정한다.** 개발자 `.env` 에 `PIPER_PROCESS_RUNNER=systemd` 가 있으면
# 학습·업로드 소유자가 `SystemdProcess` 로 만들어져, 내부(`runner.pm`)를 만지는
# 테스트가 통째로 깨진다 — 실제로 8개가 그렇게 터졌다.
# 소유자는 **모듈 로드 때** 정해지므로 fixture 로는 늦다. import 전에 박는다.
os.environ["PIPER_PROCESS_RUNNER"] = "local"

# ⚠ **테스트는 사용자의 설정 디렉토리(`~/.config/piper-web`)를 절대 건드리면 안 된다.**
#
#   실제로 일어났다(2026-10-14): 카메라 이름 테스트가 `CameraManager` 를 만들어 저장하자
#   **개발 기계의 `camera_session.json` 이 테스트 데이터로 덮였다** — RealSense 3 대의 등록이
#   `/dev/video0` 한 줄로 바뀌었고, 게이트웨이를 재시작했다면 등록이 통째로 사라질 뻔했다.
#   `CAMERA_SESSION_PATH` 는 클래스 정의 때 `settings.config_dir` 로 **굳으므로** 테스트마다
#   경로를 패치하는 규율에 기대면 한 번 빠뜨리는 순간 사고가 난다. 로봇 세션·파킹 위치도 같은
#   디렉토리다(robotd 도 같은 환경변수를 읽는다).
#
#   그래서 앱을 import 하기 **전에** 디렉토리 자체를 임시 위치로 돌린다 — 위
#   `PIPER_PROCESS_RUNNER` 와 같은 이유로 fixture 로는 늦다. 자식 프로세스도 물려받는다.
import atexit
import shutil
import tempfile

_TEST_CONFIG_DIR = tempfile.mkdtemp(prefix="piper-test-config-")
os.environ["PIPER_CONFIG_DIR"] = _TEST_CONFIG_DIR
atexit.register(shutil.rmtree, _TEST_CONFIG_DIR, ignore_errors=True)

# 운영은 0번을 쓴다. 번호만 바꾸면 키 공간이 완전히 갈린다.
TEST_REDIS_DB = 15
TEST_REDIS_URL = f"redis://127.0.0.1:6379/{TEST_REDIS_DB}"

# ⚠ **db 번호만으로는 안 갈린다.** Redis pub/sub 은 db 를 무시하므로, db 15 에
#   publish 한 E-stop 을 db 0 의 robotd 가 받아 **실기 팔의 토크를 끊었다.**
#   테스트를 한 번 돌릴 때마다 로봇이 서 버린 것이다.
#
#   `PREFIX` 는 **모듈 로드 때** 정해지므로 fixture 로는 늦다 — 위
#   `PIPER_PROCESS_RUNNER` 와 같은 이유로 import 전에 박는다. 자식 프로세스도
#   환경변수를 물려받아 같은 접두사를 쓴다.
os.environ["PIPER_BUS_PREFIX"] = "pipertest"


@pytest.fixture(autouse=True)
def healthy_can(monkeypatch):
    """**이 기계의 CAN 상태를 아무 테스트도 읽지 않는다.**

    ⚠ 실기에서 `can1` 이 ERROR-PASSIVE 로 넘어가자 단위 테스트 9개가 우수수
    깨졌다. 로직 테스트가 그 기계의 상태에 매이면 그때부터 아무것도 못 믿는다 —
    실패가 코드 탓인지 케이블 탓인지 가릴 수 없고, CI 에서는 아예 못 돈다.

    ⚠ **막는 자리가 `require_healthy_bus` 면 안 된다.** `jog`·`relay` 가
    `from app.services.teleop import require_healthy_bus` 로 이름을 미리 묶으므로
    `teleop` 쪽을 패치해도 안 먹는다. 실제로 두 파일이 그렇게 패치하고 있었고,
    **문자열만 보는 메타 테스트까지 있어서 아무도 안 먹는 줄 몰랐다.**

    그래서 기계를 읽는 **가장 아래**를 막는다. `can_unhealthy_reason` 은 순수
    로직이라 그대로 두고(그것도 테스트 대상이다), `can_state` 만 고정한다.
    특정 버스 상태를 보고 싶은 테스트는 스스로 다시 패치하면 된다.
    """
    try:
        from piper_robot import can
    except Exception:
        return          # piper_robot 미설치 — 관련 테스트는 어차피 skip 된다
    monkeypatch.setattr(can, "can_state", lambda iface: can.CAN_HEALTHY)


@pytest.fixture(autouse=True)
def isolated_bus(monkeypatch):
    """모든 테스트(와 그 자식 프로세스)를 테스트 DB 로 돌린다.

    두 지점을 **모두** 덮어야 한다:

    - `PIPER_REDIS_URL` — 자식 프로세스(estopd, wrapper)가 이걸 읽는다
    - `piper_bus.client.url()` — 이미 import 된 모듈이 env 를 다시 안 읽는다

    하나만 바꾸면 한쪽이 운영 DB 로 샌다.
    """
    monkeypatch.setenv("PIPER_REDIS_URL", TEST_REDIS_URL)
    try:
        from piper_bus import client as bus_client
    except Exception:
        return          # redis 미설치 환경 — 버스 테스트는 어차피 skip 된다
    monkeypatch.setattr(bus_client, "url", lambda: TEST_REDIS_URL)


def code_only(src: str) -> str:
    """주석을 걷어낸 소스.

    ⚠ 소스를 문자열로 뒤지는 테스트에서 **주석이 자꾸 걸린다.** "`window.confirm` 을
    쓰지 않는다"고 적어둔 설명이 "`window.confirm` 이 있으면 실패" 검사에 걸리는 식
    이다 — 이 저장소에서 두 번 났다(`shadow-[0_0_0_9999px_…]`, `window.confirm`).

    금지 검사에는 이걸 통과시킨 소스를 쓴다. **왜 안 쓰는지 적어둔 주석이
    그 규칙을 깨뜨리면 안 된다.**
    """
    import re

    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)      # /* … */
    src = re.sub(r"^\s*#.*$", "", src, flags=re.M)       # 파이썬 주석
    # `//` 는 URL(`https://`) 안에도 나온다 — 앞이 `:` 가 아닐 때만 주석으로 본다
    src = re.sub(r"(?<!:)//.*$", "", src, flags=re.M)
    return src


def python_code_only(src: str) -> str:
    """주석 **과 docstring** 을 걷어낸 파이썬 소스.

    ⚠ `code_only` 는 `#` 만 지운다. 파이썬에서는 "왜 이렇게 안 했는지"를
    docstring 에 적는 일이 흔한데, 그걸 코드로 세면 **설명문 때문에 검사가
    실패한다** — 실제로 "ultralytics 를 안 쓴다"는 검사가 "ultralytics 는
    AGPL 이라 걷어냈다"는 docstring 에 걸렸다.
    """
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


@pytest.fixture(autouse=True)
def no_leftover_segment_names():
    """**스트림 이름 지정은 프로세스 전역이다** — 한 테스트가 남기면 다음 테스트의
    `segment_for_camera` 가 엉뚱한 이름을 돌려준다.

    게이트웨이는 데몬이 확인해 준 이름을 `piper_shm` 에 든다(feature/stream-based-cameras.md).
    그 상태는 테스트 사이를 새어 나가면 안 된다 — 앞뒤로 비운다.
    """
    try:
        from piper_shm import clear_segment_names
    except Exception:
        yield
        return
    clear_segment_names()
    yield
    clear_segment_names()


@pytest.fixture(autouse=True)
def tests_never_open_real_cameras(monkeypatch):
    """**단위 테스트는 실제 `/dev/video*` 를 열지 않는다.** 열려면 `PIPER_HW_TESTS=1` 을 준다.

    ⚠ 실제로 일어났다(2026-10-14): `test_v4l2_modes_come_from_the_driver` 가 이 기계의 모든
    `/dev/video*`(RealSense 18 개 노드)를 열어 모드를 열거했다. 정상일 때는 통과했지만
    `uvcvideo` 가 커널에서 교착된 뒤에는 **노드를 닫지 못하고 pytest 본체가 D 상태로 영구
    정지**했다 — SIGKILL 로도 안 죽는다. 게다가 장치를 쥔 rsd 가 스트리밍 중인 UVC 노드에
    열거 ioctl 을 던지는 일 자체가, D405 에서 알려진 hang 의 입구와 같은 종류다.

    Python 이 여는 경로(`os.open`)만 막는다. OpenCV 등 C 라이브러리는 못 막으므로, 장치를
    쓰는 테스트는 애초에 가짜를 써야 한다.
    """
    import os as _os

    if _os.environ.get("PIPER_HW_TESTS") == "1":
        yield
        return
    real_open = _os.open

    def guarded(path, *args, **kwargs):
        if isinstance(path, (str, bytes, _os.PathLike)) and str(
                _os.fsdecode(path)).startswith("/dev/video"):
            raise RuntimeError(
                f"테스트가 실제 카메라 장치를 열려 했다: {path} — 가짜를 쓰거나 "
                "PIPER_HW_TESTS=1 로 명시적으로 켜라 (conftest 의 이 가드 주석 참고)")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(_os, "open", guarded)
    yield
