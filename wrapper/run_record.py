"""추론 실행 기록 — CSV 옆 같은 이름의 `.json` (lerobot_wrapper / grpc_wrapper 공유).

CSV 는 스텝별 숫자만 든다. 어느 체크포인트를 어떤 인자로 돌렸고 도중에 무엇을 바꿨는지가
없으면, 나중에 CSV 를 열어도 무엇의 결과인지 모른다 — 2026-10-28 세 실행(SmolVLA·ACT-DINOv3·
ACT)을 추론 시간과 task 문구로 거꾸로 짐작해야 했다.

    {"csv": ..., "started": ..., "policy_type": ..., "checkpoint": ..., "args": {...},
     ..., "changes": [{"t": ..., "key": ..., "value": ...}],
     "ended": ..., "steps": ...}

`changes[].t` 는 CSV `timestamp` 열과 같은 시계다 — 그 행에 맞춰 읽는다. 래퍼마다 시계가 달라서
(lerobot_wrapper 는 `time.monotonic`, grpc_wrapper 는 `time.time`) 만들 때 `clock` 으로 받는다.
"""

import datetime as _dt
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 수동 조작은 조건이 아니라 입력이다 — 조그 한 번에 수십 개가 온다
_SKIP = {"manual_action"}


def settings(obj) -> dict:
    """객체의 공개 속성(설정값)만. 내부 상태(`_` 시작)는 뺀다."""
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


class RunRecord:
    def __init__(self, csv_path: str, *, clock=time.monotonic, **info) -> None:
        self.path = Path(csv_path).with_suffix(".json")
        self._clock = clock
        self._lock = threading.Lock()
        self._doc = {"csv": Path(csv_path).name, "started": _now(), **info, "changes": []}
        self._write()

    def update(self, **info) -> None:
        """시작 뒤에야 정해지는 값 (예: 모델 기본값으로 채워지는 actions_per_chunk)."""
        with self._lock:
            self._doc.update(info)
            self._write()

    def change(self, key: str, value) -> None:
        """버스로 들어온 파라미터 하나. 파라미터 리스너 스레드에서 부른다."""
        if key in _SKIP:
            return
        with self._lock:
            self._doc["changes"].append({"t": round(self._clock(), 3), "key": key, "value": value})
            self._write()

    def finish(self, steps: int) -> None:
        with self._lock:
            self._doc.update(ended=_now(), steps=steps)
            self._write()

    def _write(self) -> None:
        # ⚠ 기록 때문에 추론이 서면 안 된다 — 디스크 오류는 경고만 남긴다
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self._doc, ensure_ascii=False, indent=2, default=str) + "\n")
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("실행 기록을 쓰지 못했다 (%s): %s", self.path, e)


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")
