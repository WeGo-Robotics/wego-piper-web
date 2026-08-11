"""버스 계약 — 토픽·키 이름과 메시지 스키마의 단일 정의.

프로세스를 쪼개면 **토픽 이름과 메시지 필드가 새 중복원이 된다.**
`_ERR_BITS` 가 프로세스 경계를 넘어 복붙된 것(refactor/04-err-bits.md)이 그 예고편이라,
데몬을 하나라도 떼기 전에 이 패키지를 먼저 세운다
(refactor/daemon-split.md 1단계).

게이트웨이와 데몬이 **같은 패키지를 import** 한다. backend 안에 두면 데몬이
백엔드를 import 하게 되므로 최상위에 둔다 — `pip install -e piper_bus/`.
"""

from typing import Final

# ── 키 이름 규칙 ───────────────────────────────────────────────────────────────
#
#   piper:<도메인>:<이름>          단일 값 / 해시
#   piper:ch:<도메인>:<이름>        pub/sub 채널
#
# 접두사를 고정해 다른 Redis 사용자와 섞이지 않게 한다.

PREFIX: Final = "piper"


def _k(*parts: str) -> str:
    return ":".join((PREFIX, *parts))


def _ch(*parts: str) -> str:
    return ":".join((PREFIX, "ch", *parts))


# ── E-stop ────────────────────────────────────────────────────────────────────

# 마지막 heartbeat 시각 (epoch seconds, float 문자열). 게이트웨이가 쓰고 estopd 가 읽는다.
ESTOP_HEARTBEAT: Final = _k("estop", "heartbeat")

# E-stop 감시 활성 여부 ("1"/"0"). 추론·녹화가 시작될 때 켜진다.
ESTOP_ARMED: Final = _k("estop", "armed")

# 마지막 트리거 기록 (해시: at, reason, stopped)
ESTOP_LAST: Final = _k("estop", "last")

# 트리거 알림 — 게이트웨이가 구독해 UI 에 알린다.
# ⚠ **정지 자체는 이 채널에 의존하지 않는다.** estopd 가 PID 를 직접 kill 한다 —
# 게이트웨이 이벤트 루프가 막혀도 팔은 서야 하기 때문이다.
CH_ESTOP: Final = _ch("estop")


# ── 활동 (무엇이 실행 중인가) ─────────────────────────────────────────────────

# 해시: {activity: pid}. 게이트웨이가 프로세스를 띄우고 지울 때 갱신한다.
# estopd 가 이걸 읽어 **직접** 죽인다.
ACTIVITY_PIDS: Final = _k("activity", "pids")

# 활동 상태 변경 알림
CH_ACTIVITY: Final = _ch("activity")


# ── 메시지 스키마 ─────────────────────────────────────────────────────────────

# CH_ESTOP 페이로드
#   {"at": 1765432100.5, "reason": "heartbeat_timeout" | "manual",
#    "stopped": ["inference", "recording"], "pids": [1234, 5678]}
ESTOP_REASON_TIMEOUT: Final = "heartbeat_timeout"
ESTOP_REASON_MANUAL: Final = "manual"

# CH_ACTIVITY 페이로드
#   {"activity": "inference", "state": "running", "pid": 1234}


# ── 기본값 ────────────────────────────────────────────────────────────────────

DEFAULT_REDIS_URL: Final = "redis://127.0.0.1:6379/0"

# 이 시간(초) 동안 heartbeat 가 없으면 정지. 게이트웨이 설정과 같아야 하므로 여기 둔다.
DEFAULT_ESTOP_TIMEOUT_S: Final = 2.0
DEFAULT_ESTOP_POLL_S: Final = 0.2
