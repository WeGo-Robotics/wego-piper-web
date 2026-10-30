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

# ⚠ **pub/sub 은 Redis db 를 안 가린다.** db 15 에 publish 한 것을 db 0 의
#   구독자가 받는다 — 테스트가 db 15 로 격리해도 채널은 안 갈라졌고, 그래서
#   테스트를 돌릴 때마다 실기 robotd 가 E-stop 을 받아 **팔 토크를 끊었다.**
#   (`redis-cli -n 15 publish piper:ch:estop ...` 로 재현했다.)
#
#   그래서 격리는 **접두사**로 한다 — 키와 채널이 같은 규칙을 쓰므로 한 번에
#   갈라진다. 자식 프로세스(estopd, wrapper)도 환경변수를 물려받아 같이 갈린다.
#   운영에서는 안 설정하므로 이름이 그대로다.
import os

PREFIX: Final = os.environ.get("PIPER_BUS_PREFIX") or "piper"


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
# 시나리오 계획을 따르는 녹화에서만 뜻이 있다 (feature/scenario-designer.md §8-2) — 계획 없는 녹화의
# wrapper 는 받아도 무시한다. 같은 큐로 오는 것은 ←/→ 와 **순서가 섞이면 안 되기** 때문이다.
CONTROL_FAIL: Final = "scenario_fail"        # 지금 에피소드를 실패로 표시 (저장은 그대로)
CONTROL_UNFAIL: Final = "scenario_unfail"    # 위 표시 취소
CONTROL_DEFER: Final = "scenario_defer"      # 다음 시도를 블록 끝으로 미룬다
CONTROL_COMMANDS: Final = frozenset({CONTROL_NEXT, CONTROL_RERECORD, CONTROL_STOP})
SCENARIO_COMMANDS: Final = frozenset({CONTROL_FAIL, CONTROL_UNFAIL, CONTROL_DEFER})


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


# ── 녹화 시나리오 (feature/scenario-designer.md §9-3) ────────────────────────
#
# 계획(게이트웨이 → wrapper)과 진행(wrapper → 게이트웨이). 둘 다 키다.
#
# ⚠ **어느 시도를 찍는지는 wrapper 가 정한다.** 재녹화 여부는 리셋이 끝나야 정해지므로
#   게이트웨이가 로그를 보고 시도를 넘기면 ←/→ 와 경합한다. wrapper 는 에피소드 진입·저장·
#   버림을 직접 보므로 틀릴 수가 없고, 녹화는 유닛이라 게이트웨이가 재시작돼도 그대로다.
#   게이트웨이는 진행 키를 **읽기만** 한다.
# 계획은 파일이 아니라 키로 넘긴다 — 컨테이너 배포에서 게이트웨이와 녹화 유닛의 파일
# 경로가 다르다. 녹화 시작 때 쓰고 시작·정지 때 지운다 (세션 격리, task 와 같다).
RECORD_SCENARIO_PLAN: Final = _k("record", "scenario", "plan")
RECORD_SCENARIO_STATE: Final = _k("record", "scenario", "state")


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


# ── 시각 검출 (detectord → 오케스트레이터/게이트웨이) ────────────────────────────
#
# 프리뷰와 같은 이유로 큐가 아니다: 최신 장면 하나만 의미가 있다. 카메라별 키에
# JSON 페이로드를 덮어쓰고 TTL 로 만료시킨다. 소비자(판단 스텝)는 payload 의
# `ts` 로 신선도를 한 번 더 판단한다 — TTL 은 "detectord 가 죽었는데 낡은 검출로
# 판단하는" 사고만 막는 안전망이다.
VISION_PREFIX: Final = _k("vision")
VISION_PATTERN: Final = f"{VISION_PREFIX}:*"
VISION_TTL_MS: Final = 3000


def vision_key(name: str) -> str:
    return f"{VISION_PREFIX}:{name}"


def vision_name(key: str) -> str:
    return key[len(VISION_PREFIX) + 1:]


# ── 조명 측정 (게이트웨이 샘플러 → 소비자) ──────────────────────────────────
#
# vision 과 같은 규칙: 최신값 덮어쓰기 + TTL, payload 의 `ts` 로 신선도 재판단.
# payload 는 **원시 특징**만이다 (`piper_cam.lighting.features` 의 모양) —
# EWMA·임계 같은 판정 상태는 알람 소비자의 내부 사정이라 계약에 없다
# (feature/lighting-watch.md §5). 소비자는 이 키에만 의존한다 — 발행자가
# 게이트웨이에서 다른 프로세스로 옮겨 가도 계약은 불변.
LIGHT_PREFIX: Final = _k("light")
LIGHT_PATTERN: Final = f"{LIGHT_PREFIX}:*"
# 샘플 주기가 2초라 vision(3초)보다 길게 — 한 틱 밀렸다고 만료되면 깜빡인다.
LIGHT_TTL_MS: Final = 6000


def light_key(name: str) -> str:
    return f"{LIGHT_PREFIX}:{name}"


def light_name(key: str) -> str:
    return key[len(LIGHT_PREFIX) + 1:]


# ── 카메라 유휴 종료 기록 ────────────────────────────────────────────────────
#
# 데몬이 "읽는 곳이 없어서" 카메라를 닫으면 **그 사실**을 여기 남긴다. 게이트웨이는 장치가
# 안 열려 있다는 것만 알고 *왜*는 모른다 — 선택기가 "유휴로 종료됨"이라고 말하려면 이 기록이
# 필요하다. 사람이 다시 연결하면 데몬이 지운다. TTL 은 안전망(데몬이 죽어 못 지운 경우).
CAM_IDLE_PREFIX: Final = _k("cam", "idle")
CAM_IDLE_TTL_S: Final = 7 * 24 * 3600


def cam_idle_key(cam_id: str) -> str:
    return f"{CAM_IDLE_PREFIX}:{cam_id}"


# 검출 발행자 자기소개 — 어떤 모델·설정으로 돌고 있나. **발행자별** 키다
# (feature/plugins.md §13-3): 검출기가 여럿 동시에 돌 수 있어 프로세스당 하나이던 옛 키
# (`piper:detector:meta`)는 서로 덮어썼다. VISION_PREFIX 아래 두면 `detection_names()`
# 스캔에 잡히므로 접두사를 분리한다. 발행자가 루프마다 갱신하고 TTL 이 죽은 것의 낡은
# 정보를 지운다 — fps 하한(0.1)이면 루프가 10초라 그보다 여유 있게.
VISION_META_PREFIX: Final = _k("vision_meta")
VISION_META_PATTERN: Final = f"{VISION_META_PREFIX}:*"
VISION_META_TTL_MS: Final = 15_000


def vision_meta_key(publisher: str) -> str:
    return f"{VISION_META_PREFIX}:{publisher}"


def vision_meta_name(key: str) -> str:
    return key[len(VISION_META_PREFIX) + 1:]

# ── 플러그인 상태 (plugin → 게이트웨이 화면, feature/plugins.md §7) ──────────
#
# 플러그인이 루프마다 갱신하는 자기 상태 KV(`phase`·fps·…). 최신값 하나만 의미가
# 있으므로 큐가 아니고, TTL 이 죽은 플러그인의 낡은 값을 지운다.
PLUGIN_STATE_PREFIX: Final = _k("plugin", "state")
PLUGIN_STATE_TTL_MS: Final = 15_000


def plugin_state_key(name: str) -> str:
    return f"{PLUGIN_STATE_PREFIX}:{name}"


# 화면·ext API 의 버튼 → 플러그인 `on_command`. 큐다(눌린 순서대로 전부 전달). 플러그인이
# 죽어 있으면 쌓이기만 하므로 TTL 로 지운다 — 다음에 켠 플러그인이 옛 버튼을 받으면 안 된다.
PLUGIN_CMD_PREFIX: Final = _k("plugin", "cmd")
PLUGIN_CMD_TTL_MS: Final = 30_000


def plugin_cmd_key(name: str) -> str:
    return f"{PLUGIN_CMD_PREFIX}:{name}"


# ── 플러그인 토픽 — 플러그인 간 입출력 (feature/plugins.md §7-3, 7단계) ──────────
#
# 검출(`piper:vision:*`)이 아닌 **임의 구조의 결과**(등급·자세·판정 …)를 다음 플러그인에
# 넘기는 길. 최신값 하나 + 발행 카운터(`seq`) + TTL — 검출과 같은 철학이다: 큐가 아니고,
# 죽은 발행자의 낡은 값은 TTL 이 지운다. 소비자는 `seq` 가 바뀌었는지로 "새 것"을 안다.
PLUGIN_TOPIC_PREFIX: Final = _k("plugin", "topic")
PLUGIN_TOPIC_PATTERN: Final = f"{PLUGIN_TOPIC_PREFIX}:*"
PLUGIN_TOPIC_TTL_MS: Final = 15_000


def plugin_topic_key(topic: str) -> str:
    return f"{PLUGIN_TOPIC_PREFIX}:{topic}"


def plugin_topic_name(key: str) -> str:
    return key[len(PLUGIN_TOPIC_PREFIX) + 1:]


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

# 해시: {"데이터셋|정책|amp": 로더 실측 JSON}. job 레코드는 다음 학습이 지우므로 따로 둔다 —
# 학습 폼이 지난 실측으로 workers 권장값을 미리 채운다 (feature/train-loader-workers.md §4).
TRAIN_LOADER_PROFILES: Final = _k("train", "loader_profiles")

# 해시: {"데이터셋|정책|amp|스위치": GPU 메모리 실측 JSON}. 학습 폼이 고른 배치의 메모리를 미리 가늠하고,
# 메모리 부족으로 죽은 학습도 여기 남는다 (backend/app/services/training/gpu_mem.py).
TRAIN_MEM_PROFILES: Final = _k("train", "mem_profiles")


# ── 데몬 RPC (게이트웨이 → 하드웨어 데몬) ───────────────────────────────────
#
# 프레임은 shm 으로 나가지만 **제어**(스캔·연결·컨트롤)는 요청/응답이 필요하다.
# 리스트 기반 요청 + 응답 전용 키로 만든다 — pub/sub 은 구독자가 없으면 버려서
# 데몬이 재시작 중일 때 요청이 조용히 사라진다.
#
#   요청: LPUSH piper:rpc:<daemon>  {"id","method","args"}
#   응답: LPUSH piper:rpc:reply:<id> {"ok","result"|"error"}  (짧은 TTL)
#
# 응답 키에 TTL 을 두는 이유: 요청자가 타임아웃으로 떠나면 응답이 영원히 남는다.


def rpc_queue(daemon: str) -> str:
    return _k("rpc", daemon)


def rpc_reply_key(request_id: str) -> str:
    return _k("rpc", "reply", request_id)


# 데몬이 살아있음을 알리는 키 (TTL 로 갱신). 없으면 게이트웨이가 "데몬 없음"으로 안다.
def daemon_alive_key(daemon: str) -> str:
    return _k("daemon", "alive", daemon)


DAEMON_ALIVE_TTL_MS: Final = 3000

# 응답이 버려지지 않게 요청자가 기다리는 기본 시간.
# `hardware_reset`·`probe` 는 수 초 걸리므로 넉넉해야 한다.
RPC_TIMEOUT_S: Final = 20
RPC_REPLY_TTL_MS: Final = 30_000

RSD: Final = "rsd"          # RealSense 데몬
CAMERAD: Final = "camerad"  # v4l2 데몬
ROBOTD: Final = "robotd"    # CAN 독점 + 안전층
ESTOPD: Final = "estopd"    # E-stop watchdog
SO101D: Final = "so101d"    # SO-101 (Feetech 시리얼) 데몬
SIMD: Final = "simd"        # MuJoCo 시뮬레이션 데몬
UNITD: Final = "unitd"      # 유닛 관리 데몬 (systemctl 을 대신 쥔다 — 컨테이너 게이트웨이용)

# 데몬이 실제로 import 하는 소스 — 자기 보고(`mark_alive` info)의 mtime 스캔 범위.
# 넓게 잡으면 "낡았다" 경고가 늘 켜져 있어 아무도 안 본다.
DAEMON_SOURCES: Final = {
    RSD: ("daemons/rsd.py", "rs", "cam", "shm", "bus"),
    CAMERAD: ("daemons/camerad.py", "cam", "shm", "bus"),
    ROBOTD: ("daemons/robotd.py", "robot", "shm", "bus"),
    ESTOPD: ("daemons/estopd.py", "bus"),
    SO101D: ("daemons/so101d.py", "so101", "shm", "bus"),
    SIMD: ("daemons/simd.py", "sim", "robot", "shm", "bus"),
    UNITD: ("daemons/unitd.py", "bus"),
}

# 데몬이 자기 보고에 싣는 **바깥 소프트웨어** (pip 배포명). 컨테이너 게이트웨이는
# 호스트 venv 를 들여다볼 수 없다 — 데몬이 자기 것을 재서 말하는 길뿐이다
# (feature/version-update.md §2). `piper_*` 는 DAEMON_SOURCES 에서 자동으로 딸려간다.
DAEMON_DISTS: Final = {
    RSD: ("pyrealsense2",),
    CAMERAD: ("opencv-python-headless", "opencv-python"),
    ROBOTD: ("piper-sdk", "python-can"),
    ESTOPD: (),
    SO101D: ("feetech-servo-sdk", "pyserial"),
    SIMD: ("mujoco",),
    UNITD: (),
}

# ── 유닛 카탈로그 — 화면의 [서비스] 가 그리는 것 ─────────────────────────────
#
# 유닛 이름(`piper-` 뒤) → (설명, 종류). 게이트웨이(목록·게이트)와 unitd(허용 목록)가
# **같은 표**를 읽는다 — 둘이면 한쪽만 고쳐져 "화면엔 있는데 못 끈다"가 된다.
#
#   core     — 장치·안전. 설치 스크립트가 켜 두고, 웹에서는 재시작·잠깐 끄기만
#   optional — 있어도 없어도 되는 것. 설치는 되지만 **처음엔 꺼져 있고**, 사람이
#              웹에서 켜고 "부팅 시 시작"을 고른다 (deploy/install-daemons.sh --optional)
#
# ⚠ estopd 는 여기 있지만 **웹에서 끄거나 재시작할 수 없다** — 안전장치에 원격
#   종료 경로를 다는 것은 별개의 결정이다 (UNIT_READONLY).
UNIT_CATALOG: Final[dict[str, tuple[str, str]]] = {
    ESTOPD: ("E-stop 감시 — heartbeat 가 끊기면 활동 프로세스를 죽인다", "core"),
    ROBOTD: ("PIPER 팔 (CAN) — 상태 발행·안전 필터·파킹", "core"),
    CAMERAD: ("USB 카메라 (V4L2)", "core"),
    RSD: ("RealSense 카메라", "core"),
    UNITD: ("서비스 관리 — 이 화면의 켜기/끄기를 실행한다", "core"),
    SO101D: ("SO-101 리더암 (Feetech 시리얼)", "optional"),
    SIMD: ("MuJoCo 시뮬레이션 — 가상 팔·카메라", "optional"),
    "ollama": ("판단 LLM 런타임 (로컬)", "optional"),
}
#: 저널은 볼 수 있지만 **데몬 카탈로그에는 없는** 유닛 — 컨테이너와 일시 유닛이다.
#: 켜고 끄는 대상이 아니라 `UNIT_CATALOG` 에 넣을 수 없지만, 로그는 봐야 한다.
LOG_EXTRA_UNITS: Final[dict[str, str]] = {
    "gateway": "웹 서버 (컨테이너)",
    "frontend": "웹 프론트 (컨테이너)",
    "update": "업데이트 실행 (일시 유닛)",
}


def log_unit_options() -> list[dict[str, str]]:
    """로그 화면이 고를 수 있는 유닛 — **여기가 유일한 목록이다.**

    ⚠ 예전엔 프론트가 배열을 손으로 들고 있었다. 카탈로그에 데몬을 더하면
    (`so101d`·`simd` 를 더했던 것처럼) **백엔드는 따라가고 화면은 안 따라간다** —
    새 데몬 로그를 개별로 못 보는데 에러도 안 나서 아무도 눈치 못 챈다. 실제로
    `frontend` 가 그렇게 빠져 있었다: 백엔드는 받아주는데 고를 수가 없었다.

    라벨은 카탈로그 설명의 **첫 마디**다 — 드롭다운에 한 줄로 들어가야 하고,
    설명 전체는 두 번째 `—` 뒤로 길다.
    """
    out = [{"id": "all", "label": "전체 piper-*"}]
    for name, (desc, _tier) in UNIT_CATALOG.items():
        out.append({"id": name, "label": f"{name} — {desc.split(' — ')[0]}"})
    for name, desc in LOG_EXTRA_UNITS.items():
        out.append({"id": name, "label": f"{name} — {desc}"})
    return out


#: 웹에서 상태만 보고 손대지 않는 유닛
UNIT_READONLY: Final = frozenset({ESTOPD})
#: 자기 자신 — 끄면 이 화면의 켜기/끄기가 같이 죽는다
UNIT_SELF: Final = UNITD


# ── 기본값 ────────────────────────────────────────────────────────────────────

DEFAULT_REDIS_URL: Final = "redis://127.0.0.1:6379/0"

# 큐 소비자가 블로킹 대기하는 시간. 0(무한)이 아닌 이유는 스레드가 종료 플래그를
# 주기적으로 확인해야 하기 때문이다.
DEFAULT_POP_TIMEOUT_S: Final = 1

# 이 시간(초) 동안 heartbeat 가 없으면 정지. 게이트웨이 설정과 같아야 하므로 여기 둔다.
DEFAULT_ESTOP_TIMEOUT_S: Final = 2.0
DEFAULT_ESTOP_POLL_S: Final = 0.2
