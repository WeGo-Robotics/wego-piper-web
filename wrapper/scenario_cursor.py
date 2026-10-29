"""녹화 시나리오 커서 — 계획의 어느 시도를 지금 찍는가 (feature/scenario-designer.md §9-3).

start_record.py 가 LeRobot 의 순간마다 부른다:

    begin()          에피소드 진입 (record_loop 에 dataset 이 붙은 호출) → 이 시도의 task
    saved(index)     save_episode() 가 끝난 직후 → 사이드카 로그에 덧붙일 한 줄
    dropped()        ← 재녹화로 버퍼를 비웠다 → 같은 시도를 다시
    set_failed()     [실패로 표시] — 저장은 그대로, 줄에 outcome=failed·decision=pending
    defer()          [다음 배치 미루기] — 다음 시도를 블록 끝으로

LeRobot·버스를 모른다 — 순수 상태라 백엔드 테스트가 그대로 돌린다.

순서 규칙:

- 큐는 계획 순서다. 같은 계획으로 이미 성공 저장된 시도는 처음부터 뺀다(`done`) — 이어 찍기.
- 실패로 표시하고 저장한 시도는 **그 블록 끝**에 다시 들어간다 (§8-2). 블록에 남은 것이 없으면
  바로 다음이다 — 그게 블록 끝이다.
- 미루기도 블록 끝으로. 블록에 그것 하나뿐이면 맨 뒤로 — 제자리에 두면 미룬 게 아니다.
- 판을 새로 늘어놓을지는 여기서 정하지 않는다. `prev`(마지막 성공 시도)만 알려 주고,
  게이트웨이가 계획의 연쇄와 대조한다 — 실패·미루기로 연쇄가 끊긴 자리는 거기서 드러난다.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
from pathlib import Path

PLAN_FILE = "piper_scenario.json"
LOG_FILE = "piper_scenario_log.jsonl"


class Cursor:
    def __init__(self, plan_id: str, episodes: list[dict], done=()) -> None:
        self.plan_id = plan_id
        self._block = {ep["id"]: ep.get("block") for ep in episodes}
        self._task = {ep["id"]: ep.get("task") or "" for ep in episodes}
        done = set(done)
        self.queue: list[str] = [ep["id"] for ep in episodes if ep["id"] not in done]
        self.total = len(episodes)
        self.done = len([i for i in self._block if i in done])
        self.current: str | None = None
        self.failed = False            # 지금 에피소드에 실패 표시
        self.rerecord = False          # ← 를 받았다 — 리셋이 끝나면 같은 시도를 다시
        self.prev: str | None = None   # 이 세션에서 마지막으로 성공 저장된 시도
        self.saved_n = 0
        self.failed_n = 0
        self.deferred_n = 0
        self.last: dict | None = None
        self._lock = threading.Lock()

    # ── LeRobot 순간 ──

    def begin(self) -> dict | None:
        """에피소드 진입. 찍을 시도 `{id, task}`, 계획을 다 찍었으면 None."""
        with self._lock:
            if self.current is not None:      # 진입이 두 번 — 앞의 것은 저장도 버림도 안 됐다
                self.queue.insert(0, self.current)
            self.failed = self.rerecord = False
            self.current = self.queue.pop(0) if self.queue else None
            if self.current is None:
                return None
            return {"id": self.current, "task": self._task[self.current]}

    def saved(self, episode_index: int, now: str | None = None) -> dict | None:
        """저장 직후. 로그에 덧붙일 줄 — 시도 없이 찍힌 에피소드면 None."""
        with self._lock:
            tid, failed = self.current, self.failed
            self.current, self.failed, self.rerecord = None, False, False
            if tid is None:
                return None
            line = {"plan": self.plan_id, "trial": tid, "episode_index": int(episode_index),
                    "outcome": "failed" if failed else "success", "at": now or _now()}
            if failed:
                line["decision"] = "pending"
                self._to_block_end(tid)
                self.failed_n += 1
            else:
                self.done += 1
                self.saved_n += 1
                self.prev = tid
            self.last = line
            return line

    def dropped(self) -> None:
        """← 재녹화 — 버퍼를 비웠다. 같은 시도가 다시 맨 앞."""
        with self._lock:
            if self.current is not None:
                self.queue.insert(0, self.current)
            self.current, self.failed, self.rerecord = None, False, False

    # ── 사람이 누르는 것 ──

    def mark_rerecord(self) -> None:
        with self._lock:
            if self.current is not None:
                self.rerecord = True

    def set_failed(self, flag: bool) -> bool:
        """지금 에피소드에 실패 표시(또는 취소). 찍는 시도가 없으면 False."""
        with self._lock:
            if self.current is None:
                return False
            self.failed = bool(flag)
            return True

    def defer(self) -> str | None:
        """다음 시도를 블록 끝으로. 미룬 시도 id, 미룰 것이 없으면 None."""
        with self._lock:
            if len(self.queue) < 2:
                return None
            tid = self.queue.pop(0)
            if not self._to_block_end(tid, behind_only=True):
                self.queue.append(tid)
            self.deferred_n += 1
            return tid

    # ── 보여 주기 ──

    @property
    def finished(self) -> bool:
        return self.current is None and not self.queue

    def state(self) -> dict:
        with self._lock:
            nxt = self.current if self.rerecord else (self.queue[0] if self.queue else None)
            return {
                "plan_id": self.plan_id, "current": self.current, "current_failed": self.failed,
                "rerecord": self.rerecord, "next": nxt, "prev": self.prev, "queue": list(self.queue),
                "done": self.done, "total": self.total,
                "session": {"saved": self.saved_n, "failed": self.failed_n, "deferred": self.deferred_n},
                "last": self.last, "finished": self.current is None and not self.queue,
            }

    def _to_block_end(self, tid: str, behind_only: bool = False) -> bool:
        """`tid` 를 큐에서 같은 블록의 마지막 뒤에 넣는다. 같은 블록이 없으면
        `behind_only` 일 때 False(넣지 않음), 아니면 맨 앞(그게 블록 끝이다)."""
        block = self._block.get(tid)
        last = max((i for i, q in enumerate(self.queue) if self._block.get(q) == block), default=None)
        if last is None:
            if behind_only:
                return False
            self.queue.insert(0, tid)
            return True
        self.queue.insert(last + 1, tid)
        return True


# ── 사이드카 (데이터셋 meta/) ──

def append_line(root: Path, line: dict) -> None:
    """한 줄 덧붙인다. 저장 직후에 부르므로 끊겨도 앞 줄은 남는다."""
    path = Path(root) / "meta" / LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def write_plan(root: Path, plan: dict) -> bool:
    """계획 사이드카 — 이미 있으면 그대로 둔다 (같은 계획인지는 게이트웨이가 시작 때 확인했다)."""
    path = Path(root) / "meta" / PLAN_FILE
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return True


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")
