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


# ── 추론 파라미터 (게이트웨이 → wrapper) ─────────────────────────────────────
#
# ZMQ PUSH/PULL tcp://127.0.0.1:5555 을 대체한다 (daemon-split.md 3단계).
#
# **pub/sub 이 아니라 리스트인 이유**: pub/sub 은 구독자가 아직 없으면 조용히 버린다.
# wrapper 가 정책을 로드하는 동안(수 초) 보낸 파라미터가 사라지는 레이스가 생긴다.
# PUSH/PULL 은 큐였으므로 리스트(LPUSH/BRPOP)가 같은 시맨틱이다.
PARAMS_QUEUE: Final = _k("params", "queue")

# ⚠ 큐가 **세션을 넘어 살아남는다** — ZMQ 는 소켓을 닫으면 큐도 사라졌지만
# Redis 리스트는 남는다. 시작·종료 때 반드시 비운다. 안 그러면 지난 세션 끝에
# 보낸 파라미터가 다음 추론 시작 직후에 적용된다.


# ── 녹화 제어 (게이트웨이 → wrapper) ─────────────────────────────────────────
#
# ZMQ PUSH/PULL tcp://127.0.0.1:5557 대체. 위와 같은 이유로 리스트다.
# 헤드리스라 LeRobot 키보드 리스너가 꺼져 있어 이 채널이 에피소드 제어의 유일한 경로다.
RECORD_CONTROL_QUEUE: Final = _k("record", "control")

# ⚠ `right` 는 **건너뛰기가 아니다.** LeRobot 은 이 신호로 record 루프를 빠져나온 뒤
# `dataset.save_episode()` 로 떨어진다 — **에피소드는 저장된다.** 지정 시간을 다 채우지
# 않고 지금 마감한다는 뜻이다. (리셋 대기 중이면 "리셋 끝, 다음 시작"이 된다.)
# 에피소드를 버리는 것은 `left`(재녹화)뿐이다. 이름이 "skip" 이던 시절 UI 문구까지
# "건너뛰기"로 새어 사용자가 "이번 걸 버린다"로 읽었다.
CONTROL_NEXT: Final = "right"       # exit_early — 지금 마감하고 저장 → 다음
CONTROL_RERECORD: Final = "left"    # rerecord_episode — 폐기 후 다시
CONTROL_STOP: Final = "escape"      # stop_recording — 저장하고 녹화 종료
CONTROL_COMMANDS: Final = frozenset({CONTROL_NEXT, CONTROL_RERECORD, CONTROL_STOP})


# ── 녹화 task (게이트웨이 → wrapper) ─────────────────────────────────────────
#
# 녹화 중 task 문구를 바꾼다. **큐가 아니라 키다** — 최신 값만 의미가 있고,
# wrapper 는 에피소드를 시작할 때 한 번 읽는다.
#
# ⚠ **에피소드 단위로만 바뀐다.** LeRobot 은 `record_loop` 진입 시점의 task 를
# 그 에피소드의 **모든 프레임**에 찍는다. 도중에 바꾸면 한 에피소드 안에서
# 프레임마다 task 가 달라져 데이터셋의 "에피소드 = 하나의 task" 전제가 깨진다.
# 그래서 바꾼 값은 **다음 에피소드부터** 적용한다.
RECORD_TASK: Final = _k("record", "task")


# ── 녹화 프리뷰 (wrapper → 게이트웨이) ───────────────────────────────────────
#
# ZMQ PUSH/PULL tcp://127.0.0.1:5556 대체.
#
# **여기만 큐가 아니다.** 최신 프레임 하나만 의미가 있어서, 리스트로 두면 UI 폴링이
# 밀릴 때 JPEG 가 무한히 쌓인다. 카메라별 키에 덮어쓰고 TTL 로 만료시킨다 —
# 예전 `_FRESH_SECONDS` 판정이 TTL 로 대체된다.
PREVIEW_PREFIX: Final = _k("preview")
PREVIEW_PATTERN: Final = f"{PREVIEW_PREFIX}:*"
PREVIEW_TTL_MS: Final = 3000


def preview_key(name: str) -> str:
    return f"{PREVIEW_PREFIX}:{name}"


def preview_name(key: str) -> str:
    """`preview_key` 의 역함수. SCAN 결과를 카메라 이름으로 되돌린다."""
    return key[len(PREVIEW_PREFIX) + 1:]


# ── 학습 job 레지스트리 ──────────────────────────────────────────────────────
#
# 해시: {job_id: JobRecord JSON}. 로컬 job 도 여기 들어간다 (`job_id="local"`) —
# 로컬과 원격이 같은 경로를 타야 UI 가 하나로 끝난다
# (feature/cloud-training.md 3단계).
#
# **게이트웨이가 재시작해도 여기서 다시 읽으면 그만이다** — 지금 서버 리로드 시
# 학습 상태가 날아가던 버그 클래스가 구조적으로 사라진다.
TRAIN_JOBS: Final = _k("train", "jobs")

# job 상태 변경 알림
CH_TRAIN_JOBS: Final = _ch("train", "jobs")

# job 별 로그 링버퍼 (리스트). 6시간 학습이면 수만 줄이라 전부 WS 로 밀면 브라우저가 죽는다.
# WS 로는 신규분만 보내고 전체는 REST 페이지네이션으로 읽는다.
def train_log_key(job_id: str) -> str:
    return _k("train", "log", job_id)


TRAIN_LOG_MAX: Final = 5000

# 해시: {job_id: 누적 줄 수}. 링버퍼는 앞을 버리므로 길이만으로는
# "몇 줄 놓쳤나"를 알 수 없다 — 누적 카운터를 따로 둔다.
TRAIN_LOG_LINES: Final = _k("train", "log_lines")

# 로컬 학습에도 job_id 를 준다. 원격과 다른 경로를 만들면 UI 가 두 벌이 된다.
LOCAL_JOB_ID: Final = "local"


# ── 기본값 ────────────────────────────────────────────────────────────────────

DEFAULT_REDIS_URL: Final = "redis://127.0.0.1:6379/0"

# 큐 소비자가 블로킹 대기하는 시간. 0(무한)이 아닌 이유는 스레드가 종료 플래그를
# 주기적으로 확인해야 하기 때문이다.
DEFAULT_POP_TIMEOUT_S: Final = 1

# 이 시간(초) 동안 heartbeat 가 없으면 정지. 게이트웨이 설정과 같아야 하므로 여기 둔다.
DEFAULT_ESTOP_TIMEOUT_S: Final = 2.0
DEFAULT_ESTOP_POLL_S: Final = 0.2
