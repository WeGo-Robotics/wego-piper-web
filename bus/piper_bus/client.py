"""Redis 연결 — 게이트웨이와 데몬이 같은 방식으로 붙는다.

ZMQ 소켓 3개(5555/5556/5557)를 대체하는 자리다. 상태가 Redis 에 남으므로
**게이트웨이가 재시작해도 다시 읽으면 그만인 무상태가 된다** —
지금 서버 리로드 시 학습 상태가 날아가던 버그 클래스가 구조적으로 사라진다.
"""

import json
import os
import time
from typing import Any

import redis

from piper_bus import contract as C


def url() -> str:
    return os.environ.get("PIPER_REDIS_URL", C.DEFAULT_REDIS_URL)


def connect(decode: bool = True, timeout: float = 1.0) -> redis.Redis:
    """동기 클라이언트. 데몬은 이걸 쓴다.

    ⚠ **타임아웃이 있어야 한다.** 게이트웨이가 추론 중에 파라미터를 보내는데
    Redis 가 응답하지 않으면 이벤트 루프가 통째로 멈추고, 그러면 heartbeat 이 끊겨
    2초 뒤 E-stop 이 추론을 죽인다. 느린 버스가 팔을 세우게 두지 않는다.
    블로킹 pop 은 자기 타임아웃(`BRPOP`)을 쓰므로 `socket_timeout` 을 따로 넘긴다.
    """
    return redis.Redis.from_url(
        url(),
        decode_responses=decode,
        socket_timeout=timeout,
        socket_connect_timeout=timeout,
    )


class Bus:
    """계약을 아는 얇은 래퍼. 키 이름을 호출부에 흘리지 않는다."""

    def __init__(
        self,
        client: redis.Redis | None = None,
        binary_client: redis.Redis | None = None,
    ) -> None:
        self.r = client or connect()
        self._rb = binary_client

    @property
    def rb(self) -> redis.Redis:
        """바이너리 클라이언트 — JPEG 처럼 디코드하면 안 되는 값에 쓴다.

        `self.r` 은 `decode_responses=True` 라 바이트를 문자열로 바꾸려다 깨진다.
        프리뷰에서만 필요하므로 지연 생성한다.
        """
        if self._rb is None:
            self._rb = connect(decode=False)
        return self._rb

    # ── E-stop ──

    def beat(self, at: float | None = None) -> None:
        self.r.set(C.ESTOP_HEARTBEAT, repr(at if at is not None else time.time()))

    def last_beat(self) -> float | None:
        v = self.r.get(C.ESTOP_HEARTBEAT)
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def set_armed(self, armed: bool) -> None:
        self.r.set(C.ESTOP_ARMED, "1" if armed else "0")

    def is_armed(self) -> bool:
        return self.r.get(C.ESTOP_ARMED) == "1"

    def record_estop(self, reason: str, stopped: list[str], pids: list[int]) -> dict:
        payload = {
            "at": time.time(),
            "reason": reason,
            "stopped": stopped,
            "pids": pids,
        }
        self.r.hset(C.ESTOP_LAST, mapping={k: json.dumps(v) for k, v in payload.items()})
        self.r.publish(C.CH_ESTOP, json.dumps(payload))
        return payload

    def last_estop(self) -> dict | None:
        raw = self.r.hgetall(C.ESTOP_LAST)
        if not raw:
            return None
        out: dict[str, Any] = {}
        for k, v in raw.items():
            try:
                out[k] = json.loads(v)
            except (TypeError, ValueError):
                out[k] = v
        return out

    # ── 활동 PID ──

    def set_activity_pid(self, activity: str, pid: int | None) -> None:
        """실행 중인 활동의 PID 를 알린다. `None` 이면 지운다.

        estopd 가 이 PID 를 **직접** kill 한다 — 게이트웨이가 응답하지 않아도
        팔이 서야 하기 때문이다.
        """
        if pid is None:
            self.r.hdel(C.ACTIVITY_PIDS, activity)
        else:
            self.r.hset(C.ACTIVITY_PIDS, activity, str(pid))
        self.r.publish(
            C.CH_ACTIVITY, json.dumps({"activity": activity, "pid": pid})
        )

    def activity_pids(self) -> dict[str, int]:
        out = {}
        for k, v in (self.r.hgetall(C.ACTIVITY_PIDS) or {}).items():
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    # ── 큐 공통 ──

    def _brpop(self, key: str, timeout: int) -> tuple[str, str] | None:
        """블로킹 pop. **소켓 타임아웃을 "빈 큐"로 흡수한다.**

        `connect()` 가 건 `socket_timeout` 이 `BRPOP` 대기 시간보다 짧으면
        redis-py 가 `TimeoutError` 를 던진다. 둘 다 "아직 아무것도 없다"는
        같은 뜻이므로 여기서 하나로 만든다 — 안 그러면 소비 루프가 예외로 죽는다.
        """
        try:
            return self.r.brpop(key, timeout=timeout)
        except redis.exceptions.TimeoutError:
            return None

    # ── 추론 파라미터 큐 (게이트웨이 → wrapper) ──

    def push_params(self, params: dict) -> None:
        self.r.lpush(C.PARAMS_QUEUE, json.dumps(params))

    def pop_params(self, timeout: int = C.DEFAULT_POP_TIMEOUT_S) -> dict | None:
        """블로킹 pop. 타임아웃이면 `None` — 호출부가 종료 플래그를 확인할 틈을 준다."""
        item = self._brpop(C.PARAMS_QUEUE, timeout)
        if item is None:
            return None
        try:
            value = json.loads(item[1])
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def clear_params(self) -> int:
        """지난 세션의 잔여 파라미터를 버린다. **시작·종료 때 반드시 부른다.**"""
        return int(self.r.delete(C.PARAMS_QUEUE))

    # ── 녹화 제어 큐 (게이트웨이 → wrapper) ──

    def push_control(self, command: str) -> None:
        self.r.lpush(C.RECORD_CONTROL_QUEUE, command)

    def pop_control(self, timeout: int = C.DEFAULT_POP_TIMEOUT_S) -> str | None:
        item = self._brpop(C.RECORD_CONTROL_QUEUE, timeout)
        return item[1] if item else None

    def clear_control(self) -> int:
        """세션 격리 — 이전 녹화의 명령이 다음 녹화로 새면 엉뚱한 에피소드가 버려진다."""
        return int(self.r.delete(C.RECORD_CONTROL_QUEUE))

    # ── 녹화 task (게이트웨이 → wrapper) ──

    def set_record_task(self, task: str) -> None:
        """다음 에피소드부터 쓸 task. 큐가 아니라 키 — 최신 값만 의미가 있다."""
        self.r.set(C.RECORD_TASK, task)

    def record_task(self) -> str | None:
        """설정된 task. 없으면 `None` → wrapper 는 CLI 로 받은 값을 그대로 쓴다."""
        return self.r.get(C.RECORD_TASK)

    def clear_record_task(self) -> int:
        """세션 격리 — 지난 녹화의 task 가 다음 녹화 첫 에피소드에 새면 안 된다."""
        return int(self.r.delete(C.RECORD_TASK))

    # ── 녹화 프리뷰 (wrapper → 게이트웨이) ──

    def put_preview(self, name: str, jpeg: bytes) -> None:
        """최신 프레임으로 덮어쓴다. TTL 이 stale 판정을 대신한다."""
        self.rb.set(C.preview_key(name), jpeg, px=C.PREVIEW_TTL_MS)

    def get_preview(self, name: str) -> bytes | None:
        return self.rb.get(C.preview_key(name))

    def preview_names(self) -> list[str]:
        """살아 있는(TTL 안 지난) 프리뷰 카메라 이름."""
        return sorted(
            C.preview_name(k) for k in self.r.scan_iter(match=C.PREVIEW_PATTERN)
        )

    def clear_previews(self) -> int:
        keys = list(self.r.scan_iter(match=C.PREVIEW_PATTERN))
        return int(self.r.delete(*keys)) if keys else 0

    # ── 학습 job 레지스트리 ──

    def put_job(self, job_id: str, record: dict) -> None:
        self.r.hset(C.TRAIN_JOBS, job_id, json.dumps(record))
        self.r.publish(C.CH_TRAIN_JOBS, json.dumps({"job_id": job_id}))

    def get_job(self, job_id: str) -> dict | None:
        raw = self.r.hget(C.TRAIN_JOBS, job_id)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    def list_jobs(self) -> list[dict]:
        out = []
        for raw in (self.r.hgetall(C.TRAIN_JOBS) or {}).values():
            try:
                out.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return out

    def delete_job(self, job_id: str) -> None:
        self.r.hdel(C.TRAIN_JOBS, job_id)
        self.r.delete(C.train_log_key(job_id))
        self.r.hdel(C.TRAIN_LOG_LINES, job_id)
        self.r.publish(C.CH_TRAIN_JOBS, json.dumps({"job_id": job_id, "deleted": True}))

    # ── job 로그 링버퍼 ──

    def append_job_log(self, job_id: str, line: str) -> int:
        """줄을 붙이고 **전체 줄 수**를 돌려준다 — 그 값이 다음 `from` 커서다.

        `LTRIM` 으로 앞을 버리므로 리스트 길이는 상한에서 멈추지만,
        커서는 **버린 것까지 포함한 누적 개수**여야 프론트가 "몇 줄 놓쳤나"를 안다.
        """
        key = C.train_log_key(job_id)
        pipe = self.r.pipeline()
        pipe.rpush(key, line)
        pipe.ltrim(key, -C.TRAIN_LOG_MAX, -1)
        pipe.hincrby(C.TRAIN_LOG_LINES, job_id, 1)
        return int(pipe.execute()[-1])

    def job_logs(self, job_id: str, start: int = 0, limit: int = 500) -> list[str]:
        """링버퍼에서 잘라 읽는다. `start` 는 **버퍼 내 인덱스**다."""
        return self.r.lrange(C.train_log_key(job_id), start, start + limit - 1) or []

    def job_log_stats(self, job_id: str) -> dict:
        """버퍼 길이와 누적 줄 수. 둘이 다르면 앞부분이 잘렸다는 뜻이다."""
        buffered = int(self.r.llen(C.train_log_key(job_id)) or 0)
        total = int(self.r.hget(C.TRAIN_LOG_LINES, job_id) or 0)
        return {"buffered": buffered, "total": total, "dropped": max(0, total - buffered)}

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False
