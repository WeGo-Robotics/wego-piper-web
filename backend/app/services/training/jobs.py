"""학습 job 레코드와 레지스트리 (feature/cloud-training.md 3단계).

## 왜 로컬 학습에도 job_id 를 주는가

지금 `TrainManager` 는 **단일 job 가정**이다 — `train_manager.state` 하나가 곧
"학습 상태"고, WS 메시지에도 누구 것인지가 없다. 원격 job 이 하나라도 붙는 순간
두 job 이 서로의 상태를 덮어쓴다 (feature/cloud-training.md §1-(2)).

그래서 **로컬도 `job_id="local"` 을 달고 같은 경로를 탄다.** 원격용 경로를 따로 만들면
UI 가 두 벌이 되고, 그때부터 둘이 갈라진다.

## 왜 Redis 인가

문서 초안은 `config_dir/cloud_jobs.json` 이었지만, ROADMAP 이 **"3은 Redis 이후"** 로
순서를 잡은 이유가 이것이다 — 파일로 먼저 만들면 곧바로 Redis 로 다시 옮기게 된다.

부수 효과가 하나 더 있다: 상태가 프로세스 밖에 있으므로
**게이트웨이가 재시작해도 다시 읽으면 그만이다.** 지금 서버 리로드 시 학습 상태가
날아가던 문제([메모리] train_manager state resets)가 구조적으로 사라진다.

> ⚠ Redis 스냅샷 사이에 전원이 나가면 마지막 몇 초의 레코드는 잃을 수 있다.
> 원격 job 에서 이건 **과금되는 고아 인스턴스**를 뜻하므로, 최종 방어선은
> 레지스트리가 아니라 프로바이더에 직접 묻는 고아 스캐너다 (같은 문서 7단계).

## 동시 실행 상한

지금은 1이다. 로컬 학습은 `exclusivity` 표가 이미 자기 자신을 막고 있고
(GPU 경합), 원격이 붙기 전까지 2개가 될 이유가 없다. 상한을 **이름 붙은 상수 하나**로
두었으므로 클라우드가 붙을 때 여기만 고치면 된다 — 레지스트리는 이미 N 개를 담는다.
"""

# ⚠ `JobRegistry.list()` 가 클래스 본문에서 내장 `list` 를 가린다 —
# 그 뒤의 `-> list[JobRecord]` 가 메서드를 첨자 접근하려다 죽는다.
# 애노테이션을 지연 평가시켜 막는다 (메서드 이름은 호출부에서 `registry.list()` 가 자연스럽다).
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime

from piper_bus import contract as C
from piper_bus.client import Bus

from app.services.process_manager import ProcessState

logger = logging.getLogger(__name__)

LOCAL_JOB_ID = C.LOCAL_JOB_ID

# 동시에 돌 수 있는 학습 job 수. 클라우드가 붙으면 여기만 올린다.
MAX_CONCURRENT_JOBS = 1

# 아직 끝나지 않은 상태들
ACTIVE_STATES = frozenset({ProcessState.STARTING.value, ProcessState.RUNNING.value})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class JobRecord:
    """실행 방식과 무관한 job 상태. 원격이 붙어도 필드가 그대로다."""

    job_id: str
    runner: str = "local"          # local | ssh | systemd | ...
    state: str = ProcessState.IDLE.value
    output_dir: str = ""
    total_steps: int = 0
    metrics: dict = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    # 원격 전용 — 로컬에서는 비어 있다. 지금 자리를 잡아두면 4단계가 필드 추가로 끝난다.
    provider: str = ""
    instance_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


class JobRegistry:
    """버스 위의 job 목록. **버스가 없어도 학습은 돌아야 한다.**

    레지스트리는 "무엇이 돌고 있나"를 알려주는 부가 기능이지 실행 경로가 아니다.
    Redis 가 죽었다고 학습이 시작조차 안 되면 안 된다 — 모든 실패를 삼키고 로그만 남긴다.
    """

    def __init__(self, bus: Bus | None = None) -> None:
        self._bus = bus
        self._explicit = bus is not None

    def _b(self) -> Bus | None:
        if self._bus is None and not self._explicit:
            try:
                self._bus = Bus()
            except Exception as exc:
                logger.warning("job 레지스트리 버스 연결 실패: %s", exc)
                return None
        return self._bus

    def put(self, record: JobRecord) -> None:
        record.updated_at = _now()
        bus = self._b()
        if bus is None:
            return
        try:
            bus.put_job(record.job_id, record.to_dict())
        except Exception as exc:
            logger.warning("job 저장 실패 (%s): %s", record.job_id, exc)

    def get(self, job_id: str) -> JobRecord | None:
        bus = self._b()
        if bus is None:
            return None
        try:
            raw = bus.get_job(job_id)
        except Exception as exc:
            logger.warning("job 조회 실패 (%s): %s", job_id, exc)
            return None
        return _from_dict(raw) if raw else None

    def list(self) -> list[JobRecord]:
        bus = self._b()
        if bus is None:
            return []
        try:
            raws = bus.list_jobs()
        except Exception as exc:
            logger.warning("job 목록 실패: %s", exc)
            return []
        jobs = [j for j in (_from_dict(r) for r in raws) if j is not None]
        # 최근 것이 위로. 같은 시각이면 job_id 로 안정 정렬한다.
        return sorted(jobs, key=lambda j: (j.created_at, j.job_id), reverse=True)

    def active(self) -> list[JobRecord]:
        return [j for j in self.list() if j.is_active]

    def delete(self, job_id: str) -> None:
        bus = self._b()
        if bus is None:
            return
        try:
            bus.delete_job(job_id)
        except Exception as exc:
            logger.warning("job 삭제 실패 (%s): %s", job_id, exc)

    # ── 로그 링버퍼 ──

    def append_log(self, job_id: str, line: str) -> None:
        bus = self._b()
        if bus is None:
            return
        try:
            bus.append_job_log(job_id, line)
        except Exception:
            pass    # 로그 보관 실패로 학습을 방해하지 않는다

    def logs(self, job_id: str, start: int = 0, limit: int = 500) -> dict:
        bus = self._b()
        if bus is None:
            return {"lines": [], "start": start, "buffered": 0, "total": 0, "dropped": 0}
        try:
            lines = bus.job_logs(job_id, start, limit)
            stats = bus.job_log_stats(job_id)
        except Exception as exc:
            logger.warning("job 로그 조회 실패 (%s): %s", job_id, exc)
            return {"lines": [], "start": start, "buffered": 0, "total": 0, "dropped": 0}
        return {"lines": lines, "start": start, **stats}


def _from_dict(raw: dict) -> JobRecord | None:
    """모르는 필드는 버린다 — 옛 레코드가 남아 있어도 죽지 않게."""
    if not isinstance(raw, dict) or "job_id" not in raw:
        return None
    known = {f for f in JobRecord.__dataclass_fields__}
    return JobRecord(**{k: v for k, v in raw.items() if k in known})


job_registry = JobRegistry()
