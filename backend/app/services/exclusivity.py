"""활동 배타 규칙 — "무엇이 무엇을 막는가"의 단일 정의.

CLAUDE.md 의 *"GPU 메모리 경합: 학습과 추론 동시 실행 시 OOM 위험 → 모드를 배타적으로 관리"* 를
한 곳의 표로 만든다. 이전에는 같은 규칙이 라우터 8곳 + 프론트 3곳에 손으로 적혀 있었고
**어느 둘도 같지 않았다** (refactor/10-exclusive-mode-guard.md).

호출 형태가 셋이라 함수도 셋이다:

- `require_idle()`   시작을 막는다 (409). 대부분의 `/start` 엔드포인트
- `blocked_reason()` 사유 문자열만 돌려준다. 인코더 프로브의 CPU 폴백, 카메라 스캔 캐시 판정처럼
                     **막지 않고 다르게 동작해야 하는** 경로
- `snapshot()`       GET /api/activity — 프론트가 버튼 비활성화에 쓴다
"""

import enum
import logging
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from app.services import dataset_jobs
from app.services.policy_server_manager import policy_server_manager
from app.services.process_manager import ProcessState, process_manager
from app.services.record_manager import record_manager
from app.services.teleop import teleop_session
from app.services.training import train_manager

logger = logging.getLogger(__name__)


class Activity(str, enum.Enum):
    INFERENCE = "inference"
    RECORDING = "recording"
    TRAINING = "training"
    POLICY_SERVER = "policy_server"
    DATASET_EDIT = "dataset_edit"
    UPLOAD = "upload"
    TELEOP = "teleop"
    # 에피소드 루프 (분리수거 등). 추론 **위에서** 돈다 — 추론과는 배타가 아니다.
    ORCHESTRATOR = "orchestrator"
    # YOLO 커스텀 학습 (feature/yolo-training.md 3단계) — GPU 를 크게 쓴다.
    YOLO_TRAIN = "yolo_train"
    # 아래 둘은 "시작하는 활동"이 아니라 자원 접근이다. 남을 막지 않고 조회만 한다.
    ENCODER_PROBE = "encoder_probe"
    CAMERA_ACCESS = "camera_access"


LABELS: dict[Activity, str] = {
    Activity.INFERENCE: "추론",
    Activity.RECORDING: "녹화",
    Activity.TRAINING: "학습",
    Activity.POLICY_SERVER: "정책 서버",
    Activity.DATASET_EDIT: "데이터셋 편집",
    Activity.UPLOAD: "업로드/캐시 작업",
    Activity.TELEOP: "수동 조작",
    Activity.ORCHESTRATOR: "에피소드 루프",
    Activity.YOLO_TRAIN: "YOLO 학습",
    Activity.ENCODER_PROBE: "인코더 프로브",
    Activity.CAMERA_ACCESS: "카메라 접근",
}

# 이 활동을 시작하려면 아래 활동들이 멈춰 있어야 한다.
#
# 상태를 가진 활동(STATE_PROVIDERS) 사이에서는 **대칭**이어야 한다 —
# A가 B를 막으면 B도 A를 막는다. test_exclusivity.py 가 이걸 강제한다.
# 한쪽만 고치는 사고가 이 표를 만들게 된 원인이었다.
BLOCKED_BY: dict[Activity, list[Activity]] = {
    # 카메라 + CAN + GPU 를 전부 점유한다
    Activity.INFERENCE: [Activity.TRAINING, Activity.RECORDING, Activity.DATASET_EDIT,
                         Activity.YOLO_TRAIN, Activity.TELEOP],
    Activity.RECORDING: [Activity.TRAINING, Activity.INFERENCE, Activity.DATASET_EDIT,
                         Activity.ORCHESTRATOR, Activity.TELEOP],
    # GPU. 정책 서버도 GPU에 정책을 올리므로 학습과 배타다.
    Activity.TRAINING: [Activity.INFERENCE, Activity.RECORDING, Activity.POLICY_SERVER,
                        Activity.ORCHESTRATOR, Activity.YOLO_TRAIN],
    Activity.POLICY_SERVER: [Activity.TRAINING],
    # 데이터셋 파일을 다시 쓴다. 녹화가 같은 디스크에 기록 중이면 피한다.
    Activity.DATASET_EDIT: [Activity.INFERENCE, Activity.RECORDING, Activity.ORCHESTRATOR],
    # 네트워크/디스크만 쓴다. 아무것도 막지 않고 아무것도 막지 못한다.
    Activity.UPLOAD: [],
    # 팔을 직접 민다 — 같은 팔을 둘이 밀면 안 된다. 학습·YOLO 학습은 GPU 만
    # 쓰므로 막지 않는다.
    Activity.TELEOP: [Activity.INFERENCE, Activity.RECORDING, Activity.ORCHESTRATOR],
    # 추론 위에서 도는 자동화 계층 — 추론과 공존이 설계다.
    # 팔을 새 일로 보내는 루프이므로 녹화·학습·편집과는 서로 막는다.
    Activity.ORCHESTRATOR: [Activity.TRAINING, Activity.RECORDING, Activity.DATASET_EDIT,
                            Activity.TELEOP],
    # GPU 만 쓴다 (카메라·CAN·데이터셋 파일과 무관). VLA 학습·추론과 배타,
    # 정책 서버(~1GB 상주)와는 공존 가능 — nano/small 학습은 4~6GB 다.
    Activity.YOLO_TRAIN: [Activity.TRAINING, Activity.INFERENCE],
    # ── 아래는 자원 접근 (막히기만 하고 남을 막지 않는다) ──
    # GPU 경합 시 409가 아니라 CPU 폴백. blocked_reason() 으로만 쓴다.
    Activity.ENCODER_PROBE: [Activity.TRAINING, Activity.INFERENCE],
    # 추론/녹화 subprocess 가 물리 카메라를 소유한다. 이때 백엔드가 같은 RealSense
    # 디바이스를 열거나 UVC 컨트롤을 질의하면 (특히 D405) 커널 uvcvideo 가 D-state 로
    # 물려 librealsense 가 SIGABRT 로 죽고 카메라까지 먹통이 된다.
    Activity.CAMERA_ACCESS: [Activity.INFERENCE, Activity.RECORDING],
}


def _busy(state: ProcessState) -> bool:
    """IDLE/ERROR 가 아니면 실행 중으로 본다.

    `STOPPING` 도 포함한다 — SIGTERM 을 보낸 뒤 종료를 기다리는 동안에도 프로세스는
    살아 있고 카메라·CAN·GPU 를 쥐고 있다. 기존 가드들은 여기서 갈렸다
    (`not in (idle, error)` vs `in (running, starting)`).
    """
    return state not in (ProcessState.IDLE, ProcessState.ERROR)


# 상태를 가진 활동만. 여기 없는 활동은 `running()` 에 나타나지 않는다.
STATE_PROVIDERS: dict[Activity, Callable[[], bool]] = {
    Activity.INFERENCE: lambda: _busy(process_manager.state),
    Activity.RECORDING: lambda: _busy(record_manager.state),
    Activity.TRAINING: lambda: _busy(train_manager.state),
    Activity.POLICY_SERVER: lambda: _busy(policy_server_manager.state),
    Activity.DATASET_EDIT: lambda: _busy(dataset_jobs.edit_pm.state),
    Activity.UPLOAD: lambda: _busy(dataset_jobs.upload_pm.state),
    Activity.TELEOP: lambda: teleop_session.is_running,
    Activity.ORCHESTRATOR: lambda: _orchestrator().is_running,
    Activity.YOLO_TRAIN: lambda: _busy(_yolo_train_pm().state),
}


def _yolo_train_pm():
    # 지연 import — yolo_train_manager 가 make_process(→settings)를 물고 있어
    # 모듈 로드 순서에 얽히지 않게 한다 (orchestrator 와 같은 이유)
    from app.services.yolo_train_manager import yolo_train_pm
    return yolo_train_pm


def _orchestrator():
    # 지연 import — orchestrator 가 process_manager 등을 물고 있어 기동 순서 순환 방지
    from app.services.orchestrator import orchestrator
    return orchestrator

# E-stop 이 정지시키는 활동 — 로봇을 물리적으로 움직이는 것 전부.
# 녹화도 텔레오퍼레이션으로 팔을 움직이므로 포함한다 (이전에는 추론만 죽였다).
ESTOP_TARGETS: list[Activity] = [Activity.INFERENCE, Activity.RECORDING,
                                 Activity.TELEOP]

# E-stop 은 graceful stop 이 아니라 즉시 kill 이다.
STOPPERS: dict[Activity, Callable[[], Awaitable[None]]] = {
    Activity.INFERENCE: lambda: process_manager.kill(),
    Activity.RECORDING: lambda: record_manager.pm.kill(),
    # ⚠ 여기서 토크를 끊지 않는다. 게이트웨이가 멈춰 있으면 못 하기 때문이다 —
    #   팔을 쥔 robotd 가 E-stop 알림을 **직접 듣고** 끊는다.
    # 조그든 릴레이든 세션 하나다 — 무엇이 돌든 그걸 닫는다.
    Activity.TELEOP: lambda: _stop_teleop(),
}


# ── 조회 ──────────────────────────────────────────────────────────────────────


async def _stop_teleop() -> None:
    """텔레오퍼레이션 정지. **토크는 robotd 가 끊는다** — 여기서는 세션만 닫는다.

    조그와 릴레이가 각각 자기 자원(shm 라이터·리더 리더)을 들고 있으므로
    `teleop_session` 만 닫으면 그것들이 남는다.
    """
    from app.services.jog import jog_session
    from app.services.relay import relay_session

    for session in (jog_session, relay_session):
        try:
            session.stop()
        except Exception as exc:
            logger.error("텔레오퍼레이션 정지 실패 (%s): %s", type(session).__name__, exc)
    await teleop_session.kill()


def is_running(activity: Activity) -> bool:
    """활동이 실행 중인가. 상태 제공자가 없으면 항상 False."""
    provider = STATE_PROVIDERS.get(activity)
    return bool(provider and provider())


def running() -> list[Activity]:
    """지금 실행 중인 활동 전부."""
    return [a for a in STATE_PROVIDERS if is_running(a)]


def _contends(target: Activity, blocker: Activity) -> bool:
    """둘이 **실제로** 자원을 다투는가. 표는 "다툴 수 있다"까지만 말한다.

    학습이 낀 관계는 전부 GPU 다 (표의 넷을 보면 그렇다). 그래서 학습이 **원격에서**
    돌면 그 관계들이 통째로 사라진다 — 다른 기계의 GPU 는 이 기계의 추론을 안 막는다.

    이게 원격으로 보내는 **이유**다. 여기서 안 풀면 원격 학습을 켜도 여전히
    추론이 409 로 막혀서, 기능을 켠 값이 하나도 안 난다
    (feature/cloud-training.md 첫 표: "학습은 원격, 로컬 GPU 는 수집/추론 전용").

    ⚠ 학습 **자기 자신**은 여전히 자기를 막는다 — 원격이든 아니든 두 개를 동시에
    시작하면 같은 출력 디렉토리를 밟는다. 그건 GPU 문제가 아니라서 여기 안 걸린다.
    """
    if Activity.TRAINING not in (target, blocker):
        return True
    return getattr(train_manager, "uses_local_gpu", True)


def blocking(target: Activity) -> list[Activity]:
    """`target` 을 지금 막고 있는 활동들. 자기 자신도 포함한다(이미 실행 중이면)."""
    blockers = [a for a in BLOCKED_BY.get(target, [])
                if is_running(a) and _contends(target, a)]
    if is_running(target):
        blockers.insert(0, target)
    return blockers


def _josa(word: str, with_batchim: str, without: str) -> str:
    """받침 유무에 따라 조사를 고른다 — "정책 서버을(를)" 같은 문구를 피한다."""
    if not word:
        return without
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return without
    return with_batchim if (ord(last) - 0xAC00) % 28 else without


def blocked_reason(target: Activity) -> str | None:
    """막는 사유를 한글 라벨로. 없으면 None.

    409 를 던지지 않아야 하는 호출부용 — 인코더 프로브(CPU 폴백),
    카메라 스캔(캐시 반환) 등.
    """
    blockers = blocking(target)
    if not blockers:
        return None
    return " · ".join(LABELS[a] for a in blockers)


def require_idle(target: Activity) -> None:
    """막는 활동이 있으면 409. 메시지 형식이 여기 한 곳에만 있다."""
    blockers = blocking(target)
    if not blockers:
        return
    label = LABELS[target]
    if blockers[0] == target:
        raise HTTPException(
            409, f"{label}{_josa(label, '이', '가')} 이미 실행 중입니다."
        )
    names = " · ".join(LABELS[a] for a in blockers)
    raise HTTPException(
        409,
        f"{names} 실행 중입니다. 먼저 중지한 뒤 {label}{_josa(label, '을', '를')} 시작하세요.",
    )


def snapshot() -> dict:
    """GET /api/activity 응답. in-memory boolean 만 읽으므로 비용이 없다."""
    active = running()
    return {
        "running": [a.value for a in active],
        "blocked": {
            target.value: [a.value for a in blocking(target)]
            for target in STATE_PROVIDERS
        },
        "labels": {a.value: LABELS[a] for a in Activity},
    }


# ── E-stop ────────────────────────────────────────────────────────────────────


async def estop_all() -> list[Activity]:
    """로봇을 움직이는 활동을 전부 즉시 kill. 정지시킨 목록을 돌려준다.

    하나가 실패해도 나머지는 계속 시도한다 — E-stop 은 부분 성공이라도 해야 한다.
    """
    stopped: list[Activity] = []
    for activity in ESTOP_TARGETS:
        if not is_running(activity):
            continue
        stopper = STOPPERS.get(activity)
        if stopper is None:
            logger.error("E-stop: %s 정지 방법이 정의되지 않았습니다", activity.value)
            continue
        try:
            await stopper()
            stopped.append(activity)
            logger.warning("E-stop: %s 정지됨", LABELS[activity])
        except Exception as e:
            logger.error("E-stop: %s 정지 실패 — %s", LABELS[activity], e)
    return stopped
