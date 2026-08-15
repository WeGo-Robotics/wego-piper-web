"""WebSocket 메시지 타입 — 백엔드가 보내는 종류의 단일 정의.

이전에는 `ws.py` 의 `broadcast()` 호출에 문자열이 흩어져 있고, 프론트는 각 페이지에서
같은 문자열을 손으로 비교했다 (refactor/12-ws-message-contract.md).
`WsMessage.type` 이 그냥 `string` 이라 **오타를 내도 빌드가 통과하고 런타임에 조용히
아무 일도 안 일어났다.**

프론트의 `frontend/src/types/ws.ts` 가 짝이다. 여기 없는 타입을 보내거나
저쪽에 없는 타입을 추가하면 `tests/test_ws_contract.py` 가 잡는다.

> 데몬 분리(refactor/daemon-split.md) 후에는 이 목록이 `piper_bus/` 계약으로 옮겨간다.
> 지금 한 곳에 모아두면 그 이행이 파일 하나 이동으로 끝난다.
"""

from typing import Final

# ── 추론 (전역 process_manager) ──
LOG: Final = "log"
STATE: Final = "state"
TELEMETRY: Final = "telemetry"
LOG_SAVED: Final = "log_saved"

# ── 학습 ──
# ⚠ 이 셋은 **`job_id` 를 함께 싣는다** (feature/cloud-training.md 3단계).
# 단일 job 가정으로 두면 클라우드 job 2개가 서로의 상태를 덮어쓴다.
# 로컬 학습도 `job_id="local"` 로 같은 경로를 탄다.
TRAIN_LOG: Final = "train_log"
TRAIN_STATE: Final = "train_state"
TRAIN_METRICS: Final = "train_metrics"
# 실행 중·최근 job 목록. 프론트가 어떤 job 을 볼지 고르는 근거다.
JOB_LIST: Final = "job_list"

# ── 녹화 ──
RECORD_LOG: Final = "record_log"
RECORD_STATE: Final = "record_state"
RECORD_STATUS: Final = "record_status"

# ── 정책 서버 ──
PS_LOG: Final = "ps_log"
PS_STATE: Final = "ps_state"

# ── 에피소드 오케스트레이터 (스텝 전이·회차 완료 — 파이프라인 뷰의 재료) ──
ORCHESTRATOR: Final = "orchestrator"

# ── Hub 업로드 ──
UPLOAD_LOG: Final = "upload_log"
UPLOAD_STATE: Final = "upload_state"

# ── 장치 사라짐 (CAN·카메라) ──
# USB 가 빠지거나 컨트롤러가 죽으면 화면이 마지막 상태에 머물렀다.
# **전이에서만** 온다 — 같은 사실을 반복해 띄우면 아무도 안 읽는다.
DEVICE_ALERT: Final = "device_alert"

# ── 연결 유지 ──
PONG: Final = "pong"


ALL: Final[frozenset[str]] = frozenset({
    LOG, STATE, TELEMETRY, LOG_SAVED,
    TRAIN_LOG, TRAIN_STATE, TRAIN_METRICS, JOB_LIST,
    RECORD_LOG, RECORD_STATE, RECORD_STATUS,
    PS_LOG, PS_STATE,
    ORCHESTRATOR,
    UPLOAD_LOG, UPLOAD_STATE,
    DEVICE_ALERT,
    PONG,
})

# 학습 메시지는 어느 job 것인지 반드시 밝혀야 한다 — 테스트가 이 목록을 강제한다
JOB_SCOPED: Final[frozenset[str]] = frozenset({TRAIN_LOG, TRAIN_STATE, TRAIN_METRICS})

# `*_state` 로 끝나는 것 + `state` — 프론트가 활동 상태 재조회 시점을 이걸로 판단한다
STATE_TYPES: Final[frozenset[str]] = frozenset(
    t for t in ALL if t == STATE or t.endswith("_state")
)
