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

# 운영은 0번을 쓴다. 번호만 바꾸면 키 공간이 완전히 갈린다.
TEST_REDIS_DB = 15
TEST_REDIS_URL = f"redis://127.0.0.1:6379/{TEST_REDIS_DB}"


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
