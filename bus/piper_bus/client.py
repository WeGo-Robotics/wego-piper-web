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


def connect(decode: bool = True) -> redis.Redis:
    """동기 클라이언트. 데몬은 이걸 쓴다."""
    return redis.Redis.from_url(url(), decode_responses=decode)


class Bus:
    """계약을 아는 얇은 래퍼. 키 이름을 호출부에 흘리지 않는다."""

    def __init__(self, client: redis.Redis | None = None) -> None:
        self.r = client or connect()

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

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False
